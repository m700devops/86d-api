"""Apple Analytics: parsing Apple's report files, the import loop, the tiles.

No network and no database: the App Store Connect client is driven through a
fake HTTP object that answers the same paths Apple does, and storage is two
callbacks (crm's own storage runs against a recording cursor). What matters
here is that Apple's varying report columns are read without guessing, files
are applied oldest first with each date replaced whole (Apple's rule: the
latest file wins), the tiles count what App Store Connect counts, and a tile
with no data says so instead of showing a wrong number.
"""
import gzip
import os
import sys
import types
from contextlib import contextmanager
from datetime import date

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import apple  # noqa: E402
import crm  # noqa: E402


# ── parsing ─────────────────────────────────────────────────────────────────

ENGAGEMENT = (
    "Date\tApp Name\tApp Apple Identifier\tEvent\tPage Type\tSource Type\tDevice\tTerritory\tCounts\tUnique Counts\n"
    "2026-09-20\t86'd\t123\tImpression\tProduct page\tApp Store search\tiPhone\tUS\t100\t80\n"
    "2026-09-20\t86'd\t123\tImpression\tProduct page\tApp Store browse\tiPad\tUS\t20\t15\n"
    "2026-09-20\t86'd\t123\tPage view\tProduct page\tApp Store search\tiPhone\tUS\t30\t25\n"
    "2026-09-21\t86'd\t123\tImpression\tProduct page\tApp Store search\tiPhone\tUS\t1,050\t900\n"
)


def test_parse_reads_gzipped_tsv():
    rows = apple.parse_tsv(gzip.compress(ENGAGEMENT.encode()))
    assert len(rows) == 4 and rows[0]["Event"] == "Impression"


def test_aggregate_splits_by_event_and_sums_every_count_column():
    totals = apple.aggregate(apple.parse_tsv(ENGAGEMENT.encode()))
    assert totals[("2026-09-20", "Impression", "Counts")] == 120      # summed across devices
    assert totals[("2026-09-20", "Impression", "Unique Counts")] == 95
    assert totals[("2026-09-20", "Page view · Product page", "Counts")] == 30
    assert totals[("2026-09-21", "Impression", "Counts")] == 1050     # "1,050" read as a number
    # The app's own Apple ID is a number but not a metric.
    assert not any(k[2] == "App Apple Identifier" for k in totals)


def test_aggregate_without_a_split_column_totals_everything():
    tsv = b"Date\tApp Name\tSessions\tTotal Session Duration\n2026-09-20\tx\t5\t300\n2026-09-20\tx\t2\t60\n"
    totals = apple.aggregate(apple.parse_tsv(tsv))
    assert totals[("2026-09-20", "Total", "Sessions")] == 7
    assert totals[("2026-09-20", "Total", "Total Session Duration")] == 360


def test_aggregate_with_no_date_column_gives_nothing_rather_than_guessing():
    assert apple.aggregate([{"Counts": "5"}]) == {}


def test_normalize_key_rebuilds_a_mangled_paste():
    pem = apple.normalize_key("-----BEGIN PRIVATE KEY-----\\nABCD EFGH\\n-----END PRIVATE KEY-----")
    assert pem == "-----BEGIN PRIVATE KEY-----\nABCDEFGH\n-----END PRIVATE KEY-----\n"


# ── the import loop, against a fake Apple ───────────────────────────────────

class _Resp:
    def __init__(self, status, body=None, content=b""):
        self.status_code, self._body = status, body
        self.content = content if body is None else b"x"
        self.text = ""
    def json(self):
        return self._body


class _FakeApple:
    """Answers the App Store Connect paths sync() walks."""
    def __init__(self):
        self.downloads = 0
        tsv = gzip.compress(ENGAGEMENT.encode())
        self.routes = {
            "/v1/analyticsReportRequests/R1/reports": {"data": [
                {"id": "rep-std", "attributes": {"name": "App Store Discovery and Engagement Standard"}},
                {"id": "rep-det", "attributes": {"name": "App Store Discovery and Engagement Detailed"}},
            ]},
            "/v1/analyticsReports/rep-std/instances": {"data": [
                {"id": "inst-new", "attributes": {"processingDate": "2026-09-21"}},
                {"id": "inst-old", "attributes": {"processingDate": "2026-09-20"}},
                {"id": "inst-ancient", "attributes": {"processingDate": "2025-01-01"}},
            ]},
            "/v1/analyticsReportInstances/inst-new/segments": {"data": [
                {"id": "s1", "attributes": {"url": "https://signed.example/s1"}}]},
        }
        self.files = {"https://signed.example/s1": tsv}

    def get(self, url, params=None, headers=None):
        if url in self.files:
            assert headers is None      # pre-signed: no bearer token
            self.downloads += 1
            return _Resp(200, content=self.files[url])
        path = url.replace(apple.ASC_BASE, "")
        grain = (params or {}).get("filter[granularity]")
        if (path, grain) in self.routes:
            return _Resp(200, self.routes[(path, grain)])
        if path in self.routes:
            return _Resp(200, self.routes[path])
        return _Resp(404, {"errors": [{"detail": "not found"}]})

    def post(self, url, json=None, headers=None):
        return _Resp(201, {"data": {"id": "R-new"}})


def _client(fake):
    asc = apple.ASC("KEY", "ISSUER", "unused", http=fake)
    asc._auth = lambda: {"Authorization": "Bearer t"}
    return asc


def test_sync_imports_only_new_standard_instances():
    fake = _FakeApple()
    saved = []
    result = apple.sync(_client(fake), "R1",
                        already=lambda i: i == "inst-old",
                        save=lambda *a: saved.append(a),
                        today=date(2026, 9, 24))
    # Detailed report skipped (would double every total), the already-imported
    # day skipped, the too-old day skipped: exactly one instance imported.
    assert [s[0] for s in saved] == ["inst-new"]
    assert saved[0][1] == "App Store Discovery and Engagement Standard"
    assert saved[0][3][("2026-09-20", "Impression", "Counts")] == 120
    assert saved[0][4] == "DAILY"
    assert fake.downloads == 1
    assert result == {"reports": 1, "imported": 1, "partial": False}


def test_ensure_report_request_reuses_an_existing_ongoing_one():
    fake = _FakeApple()
    fake.routes["/v1/apps/APP/analyticsReportRequests"] = {"data": [
        {"id": "R-existing", "attributes": {"accessType": "ONGOING"}}]}
    assert apple.ensure_report_request(_client(fake), "APP", None) == "R-existing"


def test_ensure_report_request_creates_one_when_none_exists():
    fake = _FakeApple()
    fake.routes["/v1/apps/APP/analyticsReportRequests"] = {"data": []}
    assert apple.ensure_report_request(_client(fake), "APP", None) == "R-new"


def test_resolve_app_needs_a_choice_when_the_account_has_several():
    fake = _FakeApple()
    fake.routes["/v1/apps"] = {"data": [
        {"id": "1", "attributes": {"name": "86'd", "bundleId": "com.x.86d"}},
        {"id": "2", "attributes": {"name": "Other", "bundleId": "com.x.other"}}]}
    try:
        apple.resolve_app(_client(fake), None)
        assert False, "should have asked which app"
    except apple.AppleError as e:
        assert "2 apps" in str(e)


def test_a_rejected_key_says_what_to_check():
    fake = _FakeApple()
    fake.get = lambda *a, **k: _Resp(401, {"errors": [{"detail": "bad"}]})
    try:
        _client(fake).get("/v1/apps")
        assert False
    except apple.AppleError as e:
        assert "Issuer ID" in str(e)


# ── the tiles ───────────────────────────────────────────────────────────────

def _rows():
    rows = []
    for i in range(60):
        day = date.fromordinal(date(2026, 9, 21).toordinal() - i).isoformat()
        current = i < 30
        rows += [
            {"report": "App Store Discovery and Engagement Standard", "day": day,
             "dim": "Impression", "metric": "Counts", "value": 10.0 if current else 5.0},
            {"report": "App Store Discovery and Engagement Standard", "day": day,
             "dim": "Impression", "metric": "Unique Counts", "value": 5.0},
            {"report": "App Downloads Standard", "day": day,
             "dim": "First-time download", "metric": "Counts", "value": 1.0},
            {"report": "App Crashes Standard", "day": day,
             "dim": "Total", "metric": "Crashes", "value": 2.0 if current else 1.0},
        ]
    return rows


def test_summary_tiles_compare_against_the_previous_window():
    s = apple.summarize(_rows(), 30)
    k = {x["key"]: x for x in s["kpis"]}
    assert s["through"] == "2026-09-21"
    assert k["impressions"]["value"] == 300 and k["impressions"]["previous"] == 150
    assert k["impressions"]["change_pct"] == 100.0
    assert len(k["impressions"]["daily"]) == 30
    assert k["downloads"]["value"] == 30
    # Downloads per UNIQUE-device impression, App Store Connect's definition:
    # 30 downloads / (5 unique a day x 30 days).
    assert round(k["conversion"]["value"], 1) == 20.0
    # No weekly file here: the daily figure, flagged as leaving out quiet days.
    assert k["crashes"]["value"] == 60 and k["crashes"]["up_is_good"] is False
    assert k["crashes"]["partial"] is True
    assert k["proceeds"]["value"] is None                        # not reported -> "—", never 0


def test_summary_keeps_every_report_row_for_the_tables():
    s = apple.summarize(_rows(), 7)
    names = [t["report"] for t in s["reports"]]
    assert "App Crashes Standard" in names
    eng = next(t for t in s["reports"] if t["report"].startswith("App Store Discovery"))
    assert {(r["dim"], r["metric"]) for r in eng["rows"]} == {
        ("Impression", "Counts"), ("Impression", "Unique Counts")}


def test_summary_of_nothing_is_empty_not_an_error():
    assert apple.summarize([], 30)["kpis"] == []


# ── which reports, in which order, at which grain ───────────────────────────

def _rep(rid, name, category):
    return {"id": rid, "attributes": {"name": name, "category": category}}


def test_the_reports_the_tab_needs_including_crashes():
    reps = [
        _rep("1", "App Store Discovery and Engagement Standard", "APP_STORE_ENGAGEMENT"),
        _rep("2", "App Store Discovery and Engagement Detailed", "APP_STORE_ENGAGEMENT"),
        _rep("3", "App Crashes", "APP_USAGE"),           # one version, no "Standard"
        _rep("4", "App Sessions Standard", "APP_USAGE"),
        _rep("5", "ARKit Face Tracking", "FRAMEWORK_USAGE"),
        _rep("6", "App Launch Performance", "PERFORMANCE"),
        _rep("7", "App Store Downloads Standard", "APP_STORE_COMMERCE"),
    ]
    assert [r["id"] for r in apple.wanted_reports(reps)] == ["1", "3", "4", "7"]
    # No categories from Apple: the Standard ones, as before.
    bare = [{"id": r["id"], "attributes": {"name": r["attributes"]["name"]}} for r in reps]
    assert [r["id"] for r in apple.wanted_reports(bare)] == ["1", "4", "7"]


def test_usage_reports_import_weekly_files_too_and_every_file_oldest_first():
    fake = _FakeApple()
    tsv = gzip.compress(b"Date\tDevice\tSessions\n2026-09-20\tiPhone\t7\n")
    fake.routes["/v1/analyticsReportRequests/R1/reports"] = {"data": [
        _rep("rep-eng", "App Store Discovery and Engagement Standard", "APP_STORE_ENGAGEMENT"),
        _rep("rep-ses", "App Sessions Standard", "APP_USAGE")]}
    # Apple lists newest first here; the newer file must be saved LAST.
    fake.routes[("/v1/analyticsReports/rep-eng/instances", "DAILY")] = {"data": [
        {"id": "e-22", "attributes": {"processingDate": "2026-09-22"}},
        {"id": "e-21", "attributes": {"processingDate": "2026-09-21"}}]}
    fake.routes[("/v1/analyticsReports/rep-ses/instances", "DAILY")] = {"data": [
        {"id": "s-22", "attributes": {"processingDate": "2026-09-22"}}]}
    fake.routes[("/v1/analyticsReports/rep-ses/instances", "WEEKLY")] = {"data": [
        {"id": "w-18", "attributes": {"processingDate": "2026-09-18"}}]}
    for inst in ("e-22", "e-21", "s-22", "w-18"):
        fake.routes[f"/v1/analyticsReportInstances/{inst}/segments"] = {"data": [
            {"id": f"seg-{inst}", "attributes": {"url": f"https://signed.example/{inst}"}}]}
        fake.files[f"https://signed.example/{inst}"] = tsv
    saved = []
    apple.sync(_client(fake), "R1", already=lambda i: False,
               save=lambda *a: saved.append((a[0], a[4])), today=date(2026, 9, 24))
    assert saved == [("e-21", "DAILY"), ("e-22", "DAILY"), ("s-22", "DAILY"), ("w-18", "WEEKLY")]


def test_a_stopped_report_request_is_replaced():
    fake = _FakeApple()
    fake.routes["/v1/analyticsReportRequests/R-old"] = {"data": {
        "id": "R-old", "attributes": {"stoppedDueToInactivity": True}}}
    fake.routes["/v1/apps/APP/analyticsReportRequests"] = {"data": [
        {"id": "R-old", "attributes": {"accessType": "ONGOING", "stoppedDueToInactivity": True}}]}
    assert apple.ensure_report_request(_client(fake), "APP", "R-old") == "R-new"
    fake.routes["/v1/analyticsReportRequests/R-live"] = {"data": {
        "id": "R-live", "attributes": {"stoppedDueToInactivity": False}}}
    assert apple.ensure_report_request(_client(fake), "APP", "R-live") == "R-live"


def test_page_views_keep_the_page_they_were_on():
    tsv = (b"Date\tEvent\tPage Type\tCounts\tUnique Counts\n"
           b"2026-09-20\tPage view\tProduct page\t30\t25\n"
           b"2026-09-20\tPage view\tApp version history\t4\t4\n"
           b"2026-09-20\tImpression\tNo page\t100\t80\n")
    t = apple.aggregate(apple.parse_tsv(tsv))
    assert t[("2026-09-20", "Page view · Product page", "Counts")] == 30
    assert t[("2026-09-20", "Page view · App version history", "Counts")] == 4
    assert t[("2026-09-20", "Impression", "Counts")] == 100      # impressions aren't split


# ── the tiles count what App Store Connect counts ──────────────────────────

ENG = "App Store Discovery and Engagement Standard"


def _eng(day, dim, counts, uniq):
    return [{"report": ENG, "day": day, "dim": dim, "metric": "Counts", "value": float(counts)},
            {"report": ENG, "day": day, "dim": dim, "metric": "Unique Counts", "value": float(uniq)}]


def test_impressions_page_views_downloads_and_conversion_by_app_store_connects_definitions():
    day = "2026-09-20"
    rows = (_eng(day, "Impression", 100, 80) + _eng(day, "Page view · Product page", 30, 20)
            + _eng(day, "Page view · Store sheet", 2, 2)
            + _eng(day, "Page view · App version history", 9, 9)
            + [{"report": "App Store Downloads Standard", "day": day, "dim": d,
                "metric": "Counts", "value": v}
               for d, v in (("First-time Download", 8.0), ("Redownload", 2.0),
                            ("Auto-update", 50.0), ("Manual update", 4.0), ("Restore", 3.0))])
    k = {x["key"]: x for x in apple.summarize(rows, 7)["kpis"]}
    assert k["impressions"]["value"] == 132       # "includes product page views"
    assert k["page_views"]["value"] == 32         # the product page, not version history
    assert k["downloads"]["value"] == 10          # never updates or restores
    # Total downloads / unique-device impressions (80 + 20 + 2).
    assert round(k["conversion"]["value"], 2) == round(10 / 102 * 100, 2)


def _wk(week, value):
    return {"report": "App Sessions Standard", "week": week, "dim": "iPhone",
            "metric": "Sessions", "value": value}


MONDAYS = ["2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24",
           "2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21"]


def test_usage_tiles_come_from_full_weeks_and_a_missing_week_counts_as_nothing():
    weekly = [_wk(w, 10.0 + i) for i, w in enumerate(MONDAYS) if w != "2026-09-07"]
    daily = [{"report": "App Sessions Standard", "day": "2026-09-26", "dim": "iPhone",
              "metric": "Sessions", "value": 99.0}] + _eng("2026-09-26", "Impression", 5, 5)
    s = apple.summarize(daily, 30, weekly=weekly)
    sess = {x["key"]: x for x in s["kpis"]}["sessions"]
    assert sess["period"] == "weeks" and sess["weeks"] == 4
    assert (sess["from"], sess["to"]) == ("2026-08-31", "2026-09-27")
    # 08-31 (14) + 09-07 (no file: fewer than five users that week) + 09-14 (16) + 09-21 (17)
    assert sess["value"] == 47 and sess["previous"] == 10 + 11 + 12 + 13
    assert sess["change_pct"] == round((47 - 46) / 46 * 100, 1)
    assert [d["day"] for d in sess["daily"]] == MONDAYS[4:]
    assert "partial" not in sess                  # the daily 99 is not mixed in
    assert s["weeks_through"] == "2026-09-27"
    one = {x["key"]: x for x in apple.summarize(daily, 7, weekly=weekly)["kpis"]}["sessions"]
    assert one["weeks"] == 1 and one["value"] == 17


def test_nothing_is_compared_against_days_before_the_data_starts():
    rows = []
    for i in range(12):
        rows += _eng(date.fromordinal(date(2026, 9, 21).toordinal() - i).isoformat(),
                     "Impression", 10, 5)
    s = apple.summarize(rows, 30, since="2026-09-10")
    k = {x["key"]: x for x in s["kpis"]}["impressions"]
    assert k["value"] == 120 and k["previous"] is None and k["change_pct"] is None
    assert k["days_covered"] == 12 and len(k["daily"]) == 12
    assert s["since"] == "2026-09-10" and s["through"] == "2026-09-21"
    assert all(r["previous"] is None for t in s["reports"] for r in t["rows"])
    # Weekly: fewer full weeks than the window asks for — no comparison either.
    few = apple.summarize([], 30, weekly=[_wk(w, 5.0) for w in MONDAYS[-3:]])
    sess = {x["key"]: x for x in few["kpis"]}["sessions"]
    assert sess["value"] == 15 and sess["previous"] is None and sess["weeks_covered"] == 3


# ── storage in crm: a date is replaced whole, by the parser in use ─────────

class _RecCursor:
    def __init__(self, row=None):
        self.sql, self.row = [], row

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


def _db(monkeypatch, cur):
    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cur, commit=lambda: None)
    monkeypatch.setattr(crm, "get_db", db)


def test_a_file_replaces_every_date_it_carries(monkeypatch):
    cur = _RecCursor()
    _db(monkeypatch, cur)
    crm._apple_save_instance("inst-9", ENG, "2026-09-22", {
        ("2026-09-20", "Impression", "Counts"): 120.0,
        ("2026-09-21", "Impression", "Counts"): 80.0})
    first, *rest = cur.sql
    assert first[0] == "DELETE FROM crm_apple_metrics WHERE report = %s AND day = ANY(%s)"
    assert first[1] == (ENG, ["2026-09-20", "2026-09-21"])
    inserts = [p for s, p in rest if s.startswith("INSERT INTO crm_apple_metrics")]
    assert sorted(inserts) == [(ENG, "2026-09-20", "Impression", "Counts", 120.0),
                               (ENG, "2026-09-21", "Impression", "Counts", 80.0)]
    mark = next(p for s, p in rest if s.startswith("INSERT INTO crm_apple_instances"))
    assert mark[0] == "inst-9" and mark[-1] == crm.APPLE_PARSER


def test_a_weekly_file_goes_to_its_own_table(monkeypatch):
    cur = _RecCursor()
    _db(monkeypatch, cur)
    crm._apple_save_instance("w-1", "App Sessions Standard", "2026-09-25",
                             {("2026-09-15", "iPhone", "Sessions"): 40.0}, "WEEKLY")
    assert cur.sql[0][0] == "DELETE FROM crm_apple_weekly WHERE report = %s AND week = ANY(%s)"
    assert any(s.startswith("INSERT INTO crm_apple_weekly (report, week, dim, metric, value)")
               for s, _ in cur.sql)
    assert not any("crm_apple_metrics" in s for s, _ in cur.sql)


def test_a_file_read_by_an_older_parser_is_read_again(monkeypatch):
    cur = _RecCursor(row=None)
    _db(monkeypatch, cur)
    assert crm._apple_already("inst-1") is False
    assert cur.sql[0][0].endswith("WHERE id = %s AND parser >= %s")
    assert cur.sql[0][1] == ("inst-1", crm.APPLE_PARSER)


def test_the_background_import_runs_only_when_connected_and_due(monkeypatch):
    from datetime import datetime, timedelta, timezone
    started = []
    monkeypatch.setattr(crm, "_apple_start_sync", lambda: started.append(1) or True)
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    for cfg, expect in (({"connected": False, "last_sync_at": old}, False),
                        ({"connected": True, "last_sync_at": fresh}, False),
                        ({"connected": True, "last_sync_at": old}, True),
                        ({"connected": True, "last_sync_at": None}, True)):
        monkeypatch.setattr(crm, "_apple_config", lambda c=cfg: c)
        assert crm.apple_sync_if_due() is expect
    assert len(started) == 2


def test_main_runs_the_background_import():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")).read()
    assert "asyncio.create_task(_apple_sync_loop())" in src
    assert "apple_sync_if_due" in src


def test_installs_count_only_the_installations_and_deletions_report():
    rows = [{"report": "App Store Installations and Deletions Standard", "day": "2026-09-20",
             "dim": "Install", "metric": "Counts", "value": 4.0},
            {"report": "App Store Installations and Deletions Standard", "day": "2026-09-20",
             "dim": "Delete", "metric": "Counts", "value": 1.0},
            {"report": "Platform App Installs", "day": "2026-09-20",
             "dim": "Install", "metric": "Counts", "value": 9.0}]
    k = {x["key"]: x for x in apple.summarize(rows, 7)["kpis"]}
    assert k["installs"]["value"] == 4 and k["deletions"]["value"] == 1
