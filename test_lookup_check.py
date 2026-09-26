"""Quick-add's website lookup must find THE bar, not a bar.

The real case, 2026-09-25: NE Moose Bar & Grill (356 Monroe St NE,
Minneapolis) was added from the AI bar with "Website:
https://africangrilllakewood.com/ · Email found on: …" — another restaurant's
site and email. find_venue_website() took the first Nominatim hit that had a
website, without checking it was the same venue or even the same town. Now a
hit must be the same venue in the same city (and state), the site must name
the bar, and leads that already got a wrong email are cleaned once.
"""
import json
import sys
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import leadgen  # noqa: E402

MOOSE = "NE Moose Bar & Grill"
MPLS = "Minneapolis, MN"


def _hit(name, city, state_iso, website=None):
    tags = {"website": website} if website else {}
    return {"name": name, "extratags": tags,
            "address": {"city": city, "ISO3166-2-lvl4": state_iso}}


AFRICAN = _hit("African Grill & Bar", "Lakewood", "US-CO", "https://africangrilllakewood.com/")
REAL = _hit(MOOSE, "Minneapolis", "US-MN")          # on the map, no website tag


def test_another_restaurant_is_never_the_venue():
    assert not leadgen.map_result_is_venue(AFRICAN, MOOSE, MPLS)
    assert leadgen.map_result_is_venue(REAL, MOOSE, MPLS)


def test_the_same_name_in_another_town_or_state_is_not_them():
    assert not leadgen.map_result_is_venue(_hit(MOOSE, "Duluth", "US-MN"), MOOSE, MPLS)
    assert not leadgen.map_result_is_venue(_hit(MOOSE, "Minneapolis", "US-KS"), MOOSE, MPLS)
    assert leadgen.map_result_is_venue(_hit("St. Paul Tavern", "Saint Paul", "US-MN"),
                                       "St. Paul Tavern", "St. Paul, Minnesota")


def test_the_lookup_skips_a_wrong_hit_with_a_website(monkeypatch):
    calls = []

    def http(url, **kw):
        calls.append(url)
        return json.dumps([AFRICAN, REAL]), 200

    monkeypatch.setattr(leadgen, "_http", http)
    assert leadgen.find_venue_website(MOOSE, MPLS) is None      # the real one has none
    assert "addressdetails=1" in calls[0]


def test_no_town_means_no_lookup(monkeypatch):
    monkeypatch.setattr(leadgen, "_http", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("looked up a bar with no town")))
    assert leadgen.find_venue_website(MOOSE, None) is None


def test_a_site_must_name_the_bar():
    assert not leadgen.site_names_venue("<title>African Grill & Bar</title><p>Jollof rice</p>",
                                        MOOSE)
    assert leadgen.site_names_venue("<title>NE Moose</title><p>Tavern pizza</p>", MOOSE)
    # Every distinctive word, not just one: "Olde Town" isn't "Town Hall".
    assert not leadgen.site_names_venue("<p>Town Hall Brewery</p>", "Olde Town Tavern")


def test_quick_add_drops_a_looked_up_site_that_isnt_them(monkeypatch):
    import crm
    from test_quick_add import _Conn, _FakeCursor

    cur = _FakeCursor()
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    monkeypatch.setattr(leadgen, "find_venue_website", lambda name, loc=None:
                        "https://africangrilllakewood.com/")
    monkeypatch.setattr(leadgen, "site_is_venue", lambda site, name: False)
    monkeypatch.setattr(leadgen, "find_email_on_site", lambda site, **kw: (_ for _ in ()).throw(
        AssertionError("read a site that isn't the bar")))
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text, *a: {
        "name": MOOSE, "loc": MPLS, "outcome": "gatekeeper", "status": "contacted",
        "contact": "Larry", "summary": "Brenda picked up; Larry is back at 3pm."})
    out = crm.quick_add_lead(crm.QuickAdd(text="NE Moose Bar & Grill ... spoke to Brenda"), True)
    assert not out["lead"]["email"]
    assert "africangrill" not in (out["lead"]["notes"] or "")


# ── the one-time clean-up ───────────────────────────────────────────────────

NOTES = ("Website: https://africangrilllakewood.com/\nEmail found on: "
         "https://africangrilllakewood.com/\n[2026-09-25] call · attempt 1: Brenda "
         "picked up — Your notes: NE Moose Bar & Grill ... spoke to Brenda")


def test_only_looked_up_sites_are_suspects():
    typed = {"id": "T", "name": "Olde Town", "email": "o@oldetown.example",
             "notes": "Website: https://oldetown.example\nEmail found on: https://oldetown.example"
                      " — Your notes: Olde Town, website oldetown.example"}
    looked = {"id": "M", "name": MOOSE, "email": "info@africangrilllakewood.com", "notes": NOTES}
    no_email = {"id": "N", "name": "X", "email": None, "notes": NOTES}
    # A looked-up website with no email on it counts too: the wrong-number
    # button and the prep sheet read it.
    assert [r["id"] for r, _, _ in leadgen.lookup_suspects([typed, looked, no_email])] == ["M", "N"]
    flagged = dict(looked, notes=NOTES + "\n[2026-09-26] Website https://africangrilllakewood.com/"
                                         " is not theirs — the automatic website lookup …")
    assert leadgen.lookup_suspects([flagged]) == []


class _Cursor:
    def __init__(self, leads, marked=False):
        self.leads, self.marked, self.writes, self._rows = leads, marked, [], []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self._rows = []
        if s.startswith("SELECT 1 FROM crm_leadgen_oneshots"):
            self._rows = [{"x": 1}] if self.marked else []
        elif s.startswith("SELECT id, name, email, notes FROM crm_leads"):
            self._rows = self.leads
        elif s.startswith("INSERT INTO crm_leadgen_oneshots"):
            self._rows, self.marked = ([] if self.marked else [{"name": params[0]}]), True
        elif s.startswith("UPDATE"):
            self.writes.append((s, params))
        else:
            raise AssertionError(s[:80])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _db(monkeypatch, cur):
    class Conn:
        def cursor(self): return cur
        def commit(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(leadgen, "get_db", lambda: Conn())


def test_the_clean_up_takes_the_wrong_email_off_once(monkeypatch):
    lead = {"id": "M", "name": MOOSE, "email": "info@africangrilllakewood.com", "notes": NOTES}
    cur = _Cursor([lead])
    _db(monkeypatch, cur)
    monkeypatch.setattr(leadgen, "_http", lambda url, **k: ("<title>African Grill</title>", 200))
    assert leadgen.recheck_looked_up_sites() == 1
    lead_update, mail_stop = cur.writes
    assert "SET email = NULL" in lead_update[0] and lead_update[1][-1] == lead["email"]
    assert "is not theirs" in lead_update[1][1]
    assert "Removed info@africangrilllakewood.com" in lead_update[1][1]
    assert "crm_scheduled_emails" in mail_stop[0]
    assert leadgen.recheck_looked_up_sites() == 0        # marker: never again


def test_the_clean_up_leaves_a_site_that_names_the_bar(monkeypatch):
    lead = {"id": "M", "name": MOOSE, "email": "larry@nemoose.example",
            "notes": NOTES.replace("africangrilllakewood.com", "nemoose.example")}
    cur = _Cursor([lead])
    _db(monkeypatch, cur)
    monkeypatch.setattr(leadgen, "_http", lambda url, **k: ("<h1>NE Moose Bar</h1>", 200))
    assert leadgen.recheck_looked_up_sites() == 0 and cur.writes == []
