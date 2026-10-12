"""A breakfast or lunch place is not a bar (leadgen.daytime_only).

The Daily Feast PDX (2026-10-12): a downtown breakfast diner open 7am-2pm,
whose drinks page lists a Paloma and a Greyhound — two spirits, so it passed
the liquor rule, went on the call list, and cost the owner half an hour on an
email. The map had its hours all along. Every hours string and page excerpt
here is real unless it says otherwise. Pure, plus the fake cursor from
test_owner_rules.py; the crawl is stubbed.
"""
import json
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import activity  # noqa: E402
import leadgen  # noqa: E402
from callwindow import latest_close  # noqa: E402
from leadgen import daytime_only, site_latest_close  # noqa: E402
from test_owner_rules import _Cursor, _c, _l  # noqa: E402

FEAST_MAP = "Mo-Fr 07:00-14:00; Sa,Su 07:00-15:00"      # OSM node 6405580641
FEAST_HOME = ("<h2>Hours</h2><p>Mon, Tue, Wed, Thur, Fri</p><p>7:00 AM - 2:00 PM</p>"
              "<p>Sun, Sat</p><p>7:00 AM - 3:00 PM</p><p>Brunch, anytime you need it</p>"
              "<p>The Daily Feast PDX · (503)-206-7838</p>"
              '<a href="/portland-downtown-the-daily-feast-pdx-drink-menu">Drink Menu</a>')
FEAST_DRINKS = ("<p>Mimosa — fresh-squeezed grapefruit or orange juice, Sofia champagne</p>"
                "<p>Paloma — tequila, fresh squeezed grapefruit juice, lime</p>"
                "<p>Greyhound — vodka and grapefruit juice</p><p>Irish Coffee</p>")


# ── the map's hours ─────────────────────────────────────────────────────────

def test_the_daily_feast_is_daytime_by_its_map_hours():
    why = daytime_only(FEAST_MAP)
    assert why.startswith("daytime only — the map lists it shut by 3pm every day")


def test_breakfast_and_lunch_places_from_the_map():
    for hours in ("Mo-Su 07:00-15:00",                                  # Henry Higgins Bagels
                  "Mo-Fr 08:00-13:00; Sa,Su 08:00-14:00",               # Slappy Cakes
                  "We-Su 08:00-13:30; Mo,Tu off",                       # Balong Panaderia
                  "Mo-Fr 07:00-17:00"):                                 # Daily Cafe: 5pm counts
        assert daytime_only(hours), hours


def test_bars_and_evening_places_never_are():
    for hours in ("Su-Th 11:00-24:00; Fr,Sa 11:00-02:00",               # Eastburn (pub)
                  "Mo-Su 08:00-22:00",                                  # Bloodbuzz: coffee_shop
                  "Mo-Tu 08:00-15:00; We-Th,Su 08:00-24:00; Fr-Sa 08:00-01:00",   # TRUCE
                  "Fr 16:00-21:00, Sa-Su 08:00-14:00",                  # Pine State, Friday night
                  "Mo-Fr 11:00-14:00,17:00-22:00",                      # lunch and dinner
                  "Mo-Su 07:00-17:01"):
        assert daytime_only(hours) is None, hours


def test_hours_it_cant_fully_read_decide_nothing():
    # Colleen's Kitchen (Austin) serves dinner: "17:00+" is an open end.
    assert latest_close("Mo-Fr 11:00-16:00, Sa,Su 10:00-14:30, Mo-Su 16:00-17:00+") is None
    # One rule it can't read could be the evening (made up).
    assert latest_close("Mo-Fr 07:00-14:00; Fr 5pm-10pm") is None
    for hours in (None, "", "closed", "24/7", "Mo-Su sunset-02:00", "Mo-Fr 07:00-14:00; Sa late"):
        assert daytime_only(hours) is None, hours


# ── their own site ──────────────────────────────────────────────────────────

def _text(html):
    return leadgen._page_text(html)


def test_the_daily_feast_is_daytime_by_its_own_site():
    assert site_latest_close(_text(FEAST_HOME)) == 15 * 60
    assert daytime_only(site_text=_text(FEAST_HOME)).startswith(
        "daytime only — their site lists it shut by 3pm every day")


def test_any_sign_of_the_evening_on_the_page_wins():
    # Made up, each the shape a real bar's site takes.
    for page in ("<p>Hours</p><p>Mon-Fri 4pm - 2am</p>",
                 "<p>Hours: daily 11am - close</p>",
                 "<p>Hours: daily 11am-3pm</p><p>Happy hour 3-7pm</p>",
                 "<p>Hours: 11am-3pm</p><p>Join us for dinner</p>",
                 "<p>Hours: Mon-Thu 11:00 AM - 3:00 PM</p><p>Fri-Sat 5 to 10 pm</p>",
                 "<p>Hours</p><p>Tue-Sun 9-2am</p>"):
        assert site_latest_close(_text(page)) is None, page


def test_hours_for_one_part_of_the_day_say_nothing_about_closing():
    # A bar's site advertising brunch; the bar hours sit in an image.
    assert site_latest_close(_text("<p>Hours: Brunch Sat & Sun 10am-2pm</p>")) is None
    assert site_latest_close(_text("<p>Happy Hours 2-4pm</p>")) is None
    assert site_latest_close(_text("<p>Kitchen hours 11am-3pm</p>")) is None


def test_no_hours_label_or_an_ambiguous_range_decides_nothing():
    assert site_latest_close(_text("<p>Breakfast 7am-2pm</p>")) is None    # no "hours"
    assert site_latest_close(_text("<p>Hours: 10-2</p>")) is None          # am or pm?
    assert site_latest_close(_text("<p>Call (503)-206-7838 · open 2019-2026</p>")) is None
    assert site_latest_close(_text("<p>Hours: parties of 8-14 by reservation</p>")) is None


def test_how_ranges_are_read():
    for page, end in (("Hours: 11-2pm", 14 * 60), ("Hours: 7a-3p", 15 * 60),
                      ("Hours: 07:00-14:00", 14 * 60), ("Hours: 8:30 a.m. to 1:30 p.m.", 810),
                      ("Hours: 6am until 12pm", 12 * 60)):
        assert site_latest_close(page.lower()) == end, page


# ── where it's applied ──────────────────────────────────────────────────────

def test_enrichment_rejects_it_from_the_map_without_a_single_request(monkeypatch):
    monkeypatch.setattr(leadgen, "_http", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("crawled a breakfast diner")))
    out = leadgen.enrich_candidate({
        "id": "C", "name": "The Daily Feast", "website": "https://www.thedailyfeastpdx.com/",
        "raw_tags": json.dumps({"amenity": "restaurant", "opening_hours": FEAST_MAP}),
        "opening_hours": FEAST_MAP, "city": "Portland", "phone": "5032067838",
        "amenity": "restaurant"})
    assert out["status"] == "rejected" and out["reject_reason"].startswith("daytime only")


def _feast_site(monkeypatch):
    pages = {"https://www.thedailyfeastpdx.com": FEAST_HOME,
             "https://www.thedailyfeastpdx.com/portland-downtown-the-daily-feast-pdx-drink-menu":
             FEAST_DRINKS}
    monkeypatch.setattr(leadgen, "_http", lambda url, **k: (pages[url.rstrip("/")], 200)
                        if url.rstrip("/") in pages else ("", 404))


def test_with_no_map_hours_its_own_site_rejects_it_though_it_pours(monkeypatch):
    _feast_site(monkeypatch)
    assert leadgen.liquor_verdict(_text(FEAST_DRINKS), {}, "The Daily Feast",
                                  "restaurant")["status"] == "spirits"
    out = leadgen.enrich_candidate({
        "id": "C", "name": "The Daily Feast", "website": "https://www.thedailyfeastpdx.com/",
        "raw_tags": json.dumps({"amenity": "restaurant"}), "city": "Portland",
        "phone": "5032067838", "amenity": "restaurant", "local_codes": {"503"}})
    assert out["status"] == "rejected"
    assert out["reject_reason"].startswith("daytime only — their site")


def test_the_fit_check_blocks_it_both_ways(monkeypatch):
    _feast_site(monkeypatch)
    row = {"name": "The Daily Feast", "website": "https://www.thedailyfeastpdx.com/",
           "raw_tags": json.dumps({"amenity": "restaurant"}), "amenity": "restaurant",
           "city": "Portland"}
    assert leadgen.check_fit(row)["note"].startswith("daytime only — their site")
    out = leadgen.check_fit({**row, "opening_hours": FEAST_MAP})
    assert out["status"] == "blocked" and out["note"].startswith("daytime only — the map")


def test_boot_takes_daytime_places_off_the_list_and_out_of_the_bank():
    feast = {"opening_hours": FEAST_MAP}
    cur = _Cursor([{**_c("feast", {}, lead="L1", city="Portland"), **feast},
                   {**_c("griddle", {"opening_hours": "Mo-Su 09:00-15:00"}, status="qualified",
                         city="Portland")},
                   {**_c("eastburn", {}, lead="L2", city="Portland"),
                    "opening_hours": "Su-Th 11:00-24:00; Fr,Sa 11:00-02:00"},
                   {**_c("called", {}, lead="L3", city="Portland"), **feast},
                   {**_c("queued", {}, lead="L4", city="Portland"), **feast}],
                  [_l("L1"), _l("L2"), _l("L3", touched="2026-10-12T18:00:00Z"),
                   _l("L4", queued="2026-10-13")])
    assert leadgen._reconcile_owner_rules(cur) == (1, 1)
    assert "L1" not in cur.leads and cur.cands["feast"]["reject_reason"].startswith("daytime only")
    assert cur.cands["griddle"]["status"] == "rejected"
    assert "L2" in cur.leads and cur.cands["eastburn"]["status"] == "promoted"
    assert cur.leads["L3"]["fit_status"] is None           # someone emailed it: theirs now
    assert cur.leads["L4"]["fit_status"] == "blocked"      # kept, never offered


# ── the one pass over what was crawled before the rule ──────────────────────

class _HoursCursor:
    def __init__(self, rows):
        self.rows, self.sql, self.rowcount, self.writes = rows, "", 1, []

    def execute(self, sql, params=()):
        self.sql = " ".join(sql.split())
        if self.sql.startswith(("UPDATE", "DELETE")):
            self.writes.append((self.sql, params))

    def fetchone(self):
        return {"applied_at": "2026-10-12T20:00:00+00:00"}

    def fetchall(self):
        return self.rows


def _hours_db(monkeypatch, rows):
    cur = _HoursCursor(rows)

    class Conn:
        def cursor(self):
            return cur

        def commit(self):
            pass

    @contextmanager
    def db():
        yield Conn()

    monkeypatch.setattr(leadgen, "get_db", db)
    return cur


def test_the_hours_pass_reads_each_site_once(monkeypatch):
    monkeypatch.setattr(activity, "_last_scan", None)
    cur = _hours_db(monkeypatch, [
        {"id": "feast", "name": "The Daily Feast", "website": "https://feast.example",
         "status": "promoted", "lead_id": "L1"},
        {"id": "bar", "name": "Eastburn", "website": "https://bar.example",
         "status": "qualified", "lead_id": None},
        {"id": "down", "name": "Gone", "website": "https://down.example",
         "status": "qualified", "lead_id": None}])
    sites = {"https://feast.example": FEAST_HOME,
             "https://bar.example": "<p>Hours: Sun-Thu 11am-12am, Fri-Sat 11am-2am</p>"}
    monkeypatch.setattr(leadgen, "_fetch_site", lambda w: (sites.get(w, ""), w, 200 if w in sites else 0))
    assert leadgen.hours_check_step(budget_s=5) == 3
    deletes = [p for s, p in cur.writes if s.startswith("DELETE FROM crm_leads")]
    rejects = [p for s, p in cur.writes if "SET status = 'rejected'" in s]
    stamped = [p[1] for s, p in cur.writes if "hours_checked_at" in s]
    assert deletes == [("L1",)]
    assert len(rejects) == 1 and rejects[0][0].startswith("daytime only")
    assert rejects[0][2:] == ("promoted", "feast")
    assert sorted(stamped) == ["bar", "down", "feast"]       # each read once, even a dead site


def test_the_hours_pass_waits_out_a_count(monkeypatch):
    monkeypatch.setattr(activity, "_last_scan", None)
    activity.scan_seen()                                      # someone is scanning now
    cur = _hours_db(monkeypatch, [{"id": "feast", "name": "x", "website": "https://feast.example",
                                   "status": "qualified", "lead_id": None}])
    monkeypatch.setattr(leadgen, "_fetch_site", lambda w: (_ for _ in ()).throw(
        AssertionError("crawled during a count")))
    assert leadgen.hours_check_step(budget_s=0.3) == 0
    assert not [s for s, _ in cur.writes if "hours_checked_at" in s]   # left for the next batch
