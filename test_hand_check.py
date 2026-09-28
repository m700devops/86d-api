"""Bars added by hand (or found by the AI) get a timezone, hours and a phone check.

A hand-added bar arrived with no timezone — so no calling window, so never
"ready" on the call list — and nothing checked its number. Checked by hand on
the live sites: Libbey's Coastal Kitchen gets America/New_York and its map hours,
and the number logged for it, (410) 643-4400, is flagged against the
410-604-0999 its own website lists. The typed number is never replaced.
"""
import inspect
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import activity  # noqa: E402
import leadgen  # noqa: E402

HOME = "<p>Libbey's Coastal Kitchen · 357 Pier 1 Rd · <a href='tel:4106040999'>410-604-0999</a></p>"
ROW = {"name": "Libbey's Coastal Kitchen", "loc": "Stevensville, MD", "phone": "(410) 643-4400",
       "notes": "Website: https://libbeys.example/", "tz_offset_hours": None, "tz_name": None,
       "opening_hours": None}


def _net(monkeypatch, home=HOME, hit=None):
    hit = hit if hit is not None else {"website": None, "lat": 38.98, "lon": -76.33,
                                       "opening_hours": "Mo-Su 11:00-21:00", "phone": None}
    monkeypatch.setattr(leadgen, "lookup_venue", lambda name, loc=None: hit)
    monkeypatch.setattr(leadgen, "_is_public_http_url", lambda u: True)
    monkeypatch.setattr(leadgen, "_fetch_site", lambda u: (home, u, 200 if home else 404))
    monkeypatch.setattr(leadgen, "_contact_urls", lambda site, home: [])
    monkeypatch.setattr(leadgen, "site_is_venue", lambda site, name: True)


def test_timezone_and_hours_come_from_the_map(monkeypatch):
    _net(monkeypatch)
    got = leadgen.check_hand_added(ROW)
    assert got["tz_offset_hours"] == -5 and got["tz_name"] == "America/New_York"
    assert got["opening_hours"] == "Mo-Su 11:00-21:00"


def test_a_number_their_site_doesnt_list_is_flagged_not_replaced(monkeypatch):
    _net(monkeypatch)
    got = leadgen.check_hand_added(ROW)
    assert got["phone_status"] == "mismatch" and "phone" not in got
    assert got["phone_note"] == "Their website lists 410-604-0999, not the 410-643-4400 logged here"
    assert got["note"].startswith("Phone check:")


def test_a_number_on_their_site_is_confirmed(monkeypatch):
    _net(monkeypatch)
    got = leadgen.check_hand_added({**ROW, "phone": "410-604-0999"})
    assert got["phone_status"] == "confirmed" and "note" not in got


def test_nothing_known_is_overwritten_and_no_town_means_no_lookup(monkeypatch):
    _net(monkeypatch)
    got = leadgen.check_hand_added({**ROW, "tz_name": "America/Chicago", "tz_offset_hours": -6,
                                    "opening_hours": "Mo-Fr 16:00-02:00"})
    assert "tz_name" not in got and "opening_hours" not in got
    looked = []
    monkeypatch.setattr(leadgen, "lookup_venue", lambda n, l=None: looked.append(n))
    leadgen.check_hand_added({**ROW, "loc": ""})
    assert looked == []


def test_a_website_from_the_map_must_name_the_bar(monkeypatch):
    _net(monkeypatch, hit={"website": "https://other.example/", "lat": None, "lon": None,
                           "opening_hours": None, "phone": None})
    monkeypatch.setattr(leadgen, "site_is_venue", lambda site, name: False)
    got = leadgen.check_hand_added({**ROW, "notes": ""})
    assert "phone_status" not in got and "website" not in got
    assert got["tz_name"] == "America/New_York"         # MD by state, no coordinates needed


def test_saving_fills_blanks_only_and_always_stamps(monkeypatch):
    seen = []

    class Cur:
        def execute(self, sql, params=None):
            seen.append((sql, params))

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(leadgen, "get_db", db)
    leadgen.save_hand_check("L1", {"tz_name": "America/New_York", "phone_status": "mismatch",
                                   "phone_note": "x", "note": "Phone check: x"})
    sql, params = seen[0]
    assert "hand_checked_at = %s" in sql and "tz_name = COALESCE(tz_name, %s)" in sql
    assert sql.count("%s") == len(params) and params[-1] == "L1"
    leadgen.save_hand_check("L2", {})
    assert "hand_checked_at = %s" in seen[1][0]           # stamped even when nothing found


def test_the_background_batch_waits_for_a_quiet_scanner(monkeypatch):
    src = inspect.getsource(leadgen.hand_check_step)
    assert src.index("activity.wait_for_quiet(") < src.index("check_and_save_hand_added(")
    rows = [{"id": "L1"}, {"id": "L2"}]

    class Cur:
        def execute(self, sql, params=None): pass
        def fetchall(self): return rows

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(leadgen, "get_db", db)
    checked = []
    monkeypatch.setattr(leadgen, "check_and_save_hand_added", lambda i: checked.append(i))
    monkeypatch.setattr(activity, "wait_for_quiet", lambda **k: False)   # a count is on
    assert leadgen.hand_check_step() == 0 and checked == []
    monkeypatch.setattr(activity, "wait_for_quiet", lambda **k: True)
    assert leadgen.hand_check_step() == 2 and checked == ["L1", "L2"]
