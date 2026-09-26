"""The CRM's crawling stays out of the scanner's way: it runs at a dead hour for
bars (main._in_crawl_window) and waits out any bottle count in progress
(activity.py). No database, no network, no real clock.
"""
import inspect
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import activity  # noqa: E402
import leadgen  # noqa: E402
import main  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(activity, "_clock", c)
    monkeypatch.setattr(activity, "_last_scan", None)
    monkeypatch.setattr(activity, "_paused_since", None)
    return c


# ─── taking turns ────────────────────────────────────────────────────────────

def test_a_scan_makes_the_scanner_busy_until_it_goes_quiet(clock):
    assert not activity.scanning()
    activity.scan_seen()
    clock.now += activity.QUIET_SECONDS - 1
    assert activity.scanning()
    clock.now += 1
    assert not activity.scanning()


def test_quiet_means_no_wait(clock):
    slept = []
    assert activity.wait_for_quiet(sleep=slept.append) is True and slept == []


def test_a_crawl_waits_out_a_count(clock, capsys):
    activity.scan_seen()
    scans_left = [3]

    def sleep(seconds):
        clock.sleep(seconds)
        if scans_left[0]:               # the bartender is still scanning
            scans_left[0] -= 1
            activity.scan_seen()
    assert activity.wait_for_quiet(sleep=sleep) is True
    assert clock.now >= 1000 + 3 * activity.POLL_SECONDS + activity.QUIET_SECONDS
    out = capsys.readouterr().out
    assert out.count("CRAWL_PAUSED") == 1 and out.count("CRAWL_RESUMED") == 1


def test_a_crawl_with_a_deadline_gives_up_instead(clock):
    activity.scan_seen()
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        assert len(naps) < 50, "still waiting past its deadline"   # fail, don't hang
        clock.sleep(seconds)
    assert activity.wait_for_quiet(until=clock.now + 25, sleep=sleep) is False
    assert clock.now == 1025                         # not a second past its budget


# ─── the dead hour ───────────────────────────────────────────────────────────

LA = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize("hour, inside", [(4, False), (5, True), (7, True), (8, False),
                                          (18, False), (23, False)])
def test_the_crawl_window(hour, inside):
    assert main._in_crawl_window(datetime(2026, 9, 25, hour, 30, tzinfo=LA)) is inside


@pytest.mark.parametrize("day", [datetime(2026, 1, 15), datetime(2026, 7, 15)])   # winter and summer
def test_5am_los_angeles_is_8am_new_york_and_evening_in_manila(day):
    start = day.replace(hour=main.LEADGEN_CRAWL_HOUR, tzinfo=main._crawl_tz())
    assert start.astimezone(ZoneInfo("America/New_York")).hour == 8
    assert start.astimezone(ZoneInfo("Asia/Manila")).hour in (20, 21)


@pytest.mark.parametrize("hour, inside", [(22, False), (23, True), (0, True), (1, True), (2, False)])
def test_a_window_set_across_midnight(monkeypatch, hour, inside):
    monkeypatch.setattr(main, "LEADGEN_CRAWL_HOUR", 23)
    day = 26 if hour == 23 or hour == 22 else 27
    assert main._in_crawl_window(datetime(2026, 9, day, hour, 30, tzinfo=LA)) is inside


def test_once_per_window_counts_from_when_it_opened(monkeypatch):
    """Not from local midnight: a window crossing it would run twice."""
    monkeypatch.setattr(main, "LEADGEN_CRAWL_HOUR", 23)
    assert main._crawl_window_start(datetime(2026, 9, 27, 1, 40, tzinfo=LA)) == \
        datetime(2026, 9, 26, 23, 0, tzinfo=LA)
    monkeypatch.setattr(main, "LEADGEN_CRAWL_HOUR", 5)
    assert main._crawl_window_start(datetime(2026, 9, 27, 7, 59, tzinfo=LA)) == \
        datetime(2026, 9, 27, 5, 0, tzinfo=LA)
    src = inspect.getsource(main._leadgen_should_run_now)
    assert "window_start.astimezone(timezone.utc)" in src and "local_midnight" not in src


def test_the_run_no_longer_follows_the_counters_timezone():
    src = inspect.getsource(main._leadgen_should_run_now)
    assert "_reset_tz" not in src and "_crawl_tz()" in src and "_crawl_window_start" in src


# ─── every background crawl waits; what someone clicked doesn't ──────────────

def test_each_site_the_daily_run_reads_waits_first(monkeypatch):
    order = []
    monkeypatch.setattr(activity, "wait_for_quiet", lambda **kw: order.append("wait") or True)
    monkeypatch.setattr(leadgen, "enrich_candidate", lambda cand: order.append("crawl") or {"ok": 1})
    assert leadgen._enrich_safe({"name": "x"}) == {"ok": 1}
    assert order == ["wait", "crawl"]


def test_harvests_and_rechecks_wait_first():
    run = inspect.getsource(leadgen.run_daily)
    assert run.index("activity.wait_for_quiet()") < run.index("harvest_city(city)")
    recheck = inspect.getsource(leadgen.recheck_restaurant_leads)
    assert recheck.index("activity.wait_for_quiet()") < recheck.index("_http(row[\"website\"]")


def test_on_demand_lookups_never_wait():
    for fn in (leadgen.find_email_on_site, leadgen.find_venue_website):
        assert "wait_for_quiet" not in inspect.getsource(fn)


def _verify(monkeypatch, crawled):
    rows = [{"lead_id": f"L{i}", "id": f"C{i}", "city": "Denver",
             "website": f"https://bar{i}.example", "phone": "303-893-0552"} for i in range(4)]

    class Cur:
        rowcount = 1

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return rows if "FROM crm_leads l" in self.sql else []

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

    @contextmanager
    def db():
        yield Conn()

    monkeypatch.setattr(leadgen, "get_db", db)
    monkeypatch.setattr(leadgen, "find_site_phones",
                        lambda website, map_phone=None, budget=3: crawled.append(website) or (["3038930552"], True))
    monkeypatch.setattr(leadgen, "_local_codes_by_city", lambda c, cities: {})
    return leadgen.verify_phones(lead_limit=4, bank_limit=0, budget_s=0.3)


def test_phone_checks_skip_their_batch_during_a_count(monkeypatch):
    monkeypatch.setattr(activity, "_last_scan", None)
    activity.scan_seen()                             # real clock: someone is scanning now
    crawled = []
    out = _verify(monkeypatch, crawled)
    assert crawled == [] and out["leads_checked"] == 0   # left for the next batch


def test_phone_checks_run_when_its_quiet(monkeypatch):
    monkeypatch.setattr(activity, "_last_scan", None)
    crawled = []
    _verify(monkeypatch, crawled)
    assert len(crawled) == 4


# ─── what counts as scanning ─────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(activity, "_last_scan", None)
    main.app.dependency_overrides[main.get_current_user] = lambda: "u1"
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(main.get_current_user, None)


def test_opening_the_scan_screen_counts(client, monkeypatch):
    async def warmed():
        return {}
    monkeypatch.setattr(main, "_warm_providers", warmed)
    assert client.post("/v1/scans/warm").status_code == 200
    assert activity.scanning()


def test_a_scan_counts_even_one_that_is_refused(client, monkeypatch):
    monkeypatch.setattr(main, "_scan_context", lambda user: (None, None))   # not subscribed
    assert client.post("/v1/scans/analyze", json={"image": "aGVsbG8="}).status_code == 402
    assert activity.scanning()
