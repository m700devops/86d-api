"""The AI bar can find venues it was asked for — and nothing is saved unchecked.

"Libbey's Coastal Kitchen … find the sister restaurants and add them" got
"tell me their names" back: the AI bar only saw the book. Now it asks for
research (assist.BAR_SCHEMA's `research`); crm._run_research makes one
web-search call, then checks every venue named against its own website — the
site must be theirs, the phone must be on it, and a real page must connect it
to the first venue — before adding it. Checked by hand on the live sites:
Knoxie's Table (Chesapeake Bay Beach Club) passes with its number and a page
naming both; with no such page, or for an unrelated bar, it's refused.
"""
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import assist  # noqa: E402
import crm  # noqa: E402
import leadgen  # noqa: E402
import research  # noqa: E402

ANSWER = """I searched for the group.
{"found": [
 {"name": "Knoxie's Table", "city": "Stevensville", "state": "md", "website":
  "https://www.baybeachclub.com/dining/knoxies-table", "phone": "(410) 604-5900",
  "relation": "same owners (Chesapeake Bay Beach Club)", "source_url": "https://twodaystay.com/x"},
 {"name": "Knoxie's Table", "city": "Stevensville", "state": "MD"},
 {"name": "", "city": "Nowhere"},
 {"name": "No Town Bar", "city": ""},
 {"name": "Sketchy", "city": "Annapolis", "state": "Maryland", "website": "javascript:alert(1)",
  "source_url": "ftp://x"}
], "note": "Found one sister restaurant."}"""


def _words(t):
    return [w for w in leadgen._name_words(t)
            if w not in leadgen._GENERIC_NAME_WORDS and len(w) > 2]


def test_the_answer_is_parsed_and_nothing_malformed_survives():
    found, note = research.parse_found(ANSWER)
    assert [v["name"] for v in found] == ["Knoxie's Table", "Sketchy"]
    assert found[0]["state"] == "MD" and found[0]["phone"] == "(410) 604-5900"
    assert found[1]["state"] == "" and found[1]["website"] == "" and found[1]["source_url"] == ""
    assert note == "Found one sister restaurant."
    assert research.parse_found("no json here") == ([], "")
    assert research.parse_found('{"found": "nope"}') == ([], "")


def test_a_page_connects_two_venues_only_by_their_distinctive_words():
    page = "Kent Island dining: Libbey's Coastal Kitchen and Knoxie's Table, both at the Beach Club"
    assert research.related_on_page(page, "Libbey's Coastal Kitchen", _words)
    assert research.related_on_page(page, "Knoxie's Table", _words)
    assert not research.related_on_page("Harris Crab House", "Libbey's Coastal Kitchen", _words)
    assert not research.related_on_page(page, "The Kitchen", _words)   # nothing distinctive


def test_the_note_says_where_it_came_from_and_whose_call_it_was():
    note = research.lead_note("2026-09-28", "Libbey's Coastal Kitchen", "same owners",
                              "https://twodaystay.com/x", "they use Margin Edge, they are satisfied")
    assert "Found by the AI: same owners Libbey's Coastal Kitchen (source: https://twodaystay.com/x)" in note
    assert "From the call to Libbey's Coastal Kitchen: they use Margin Edge" in note


# ── checking a found venue on the web (network faked) ──────────────────────

KNOXIE = {"name": "Knoxie's Table", "city": "Stevensville", "state": "MD",
          "website": "https://www.baybeachclub.com/dining/knoxies-table", "phone": "(410) 604-5900",
          "relation": "same owners", "source_url": "https://twodaystay.com/x"}
PAGES = {
    "https://www.baybeachclub.com/dining/knoxies-table":
        "<p>Knoxie's Table at the Chesapeake Bay Beach Club. Call 410-604-5900 or 443-222-0401</p>",
    "https://twodaystay.com/x": "<p>Eat at Libbey's Coastal Kitchen, then Knoxie's Table.</p>",
    "https://harris.example/": "<p>Harris Crab House. Call 410-827-9500</p>",
}


def _web(monkeypatch, pages=PAGES):
    monkeypatch.setattr(leadgen, "_is_public_http_url", lambda u: True)
    monkeypatch.setattr(leadgen, "_fetch_site", lambda u: (pages.get(u, ""), u, 200 if u in pages else 404))
    monkeypatch.setattr(leadgen, "_contact_urls", lambda site, home: [])
    monkeypatch.setattr(leadgen, "find_venue_website", lambda name, loc=None: None)
    monkeypatch.setattr(leadgen, "find_email_on_site", lambda site, **k: (None, None))


def test_a_sister_with_its_number_on_its_site_and_a_page_naming_both_passes(monkeypatch):
    _web(monkeypatch)
    got = crm._check_found(dict(KNOXIE), "Libbey's Coastal Kitchen", "<p>Libbey's</p>")
    assert got["ok"] and got["phone"] == "4106045900" and got["phone_status"] == "confirmed"
    assert got["loc"] == "Stevensville, MD" and got["source"] == "https://twodaystay.com/x"


def test_no_page_connecting_them_is_refused(monkeypatch):
    _web(monkeypatch)
    got = crm._check_found({**KNOXIE, "source_url": ""}, "Libbey's Coastal Kitchen", "")
    assert not got["ok"] and "connected to Libbey's" in got["why"]


def test_the_first_restaurants_own_site_naming_it_is_enough(monkeypatch):
    _web(monkeypatch)
    got = crm._check_found({**KNOXIE, "source_url": ""}, "Libbey's Coastal Kitchen",
                           "<p>Our sister restaurant, Knoxie's Table</p>")
    assert got["ok"]


def test_a_number_its_site_doesnt_show_is_refused(monkeypatch):
    _web(monkeypatch)
    got = crm._check_found({**KNOXIE, "phone": "(410) 555-0199"}, "Libbey's Coastal Kitchen", "")
    assert not got["ok"] and "phone" in got["why"]
    # No number from the search and two on a group page: not guessed.
    got = crm._check_found({**KNOXIE, "phone": ""}, "Libbey's Coastal Kitchen", "")
    assert not got["ok"]


def test_a_site_that_isnt_theirs_is_refused(monkeypatch):
    _web(monkeypatch)
    got = crm._check_found({**KNOXIE, "website": "https://harris.example/"},
                           "Libbey's Coastal Kitchen", "")
    assert not got["ok"] and "website" in got["why"]


# ── the AI bar hands research over, and never asks for the names ───────────

def _bar(monkeypatch, out, research_result):
    rows = []

    class Cur:
        def execute(self, *a): pass
        def fetchall(self): return rows
        def fetchone(self): return None

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_touch_counts", lambda cur, ids: {})
    monkeypatch.setattr(crm, "_claude_json", lambda *a, **k: dict(out))
    monkeypatch.setattr(crm, "_apply_proposed", lambda *a, **k: ([], []))
    monkeypatch.setattr(crm, "_quick_add", lambda text: {
        "lead": {"id": "N1", "name": "Libbey's Coastal Kitchen", "last_outcome": "not_interested",
                 "contact": "Bill (manager)"}, "undo_id": "U1", "applied": {}})
    asked = []
    monkeypatch.setattr(crm, "_run_research", lambda item, text: asked.append(item) or research_result)
    return asked


MESSAGE = ("Libbey's Coastal Kitchen, Stevensville MD. Talked to Bill the manager, they use our "
           "competitor, Margins Edge, they are satisfied. they have sister restaurants that use "
           "the same technology. add this to the crm, and find the sister restaurants and add them")
OUT = {"reply": "Adding Libbey's and looking up its sister restaurants.",
       "question": "What are the names of Libbey's sister restaurants?",
       "changes": [], "new_leads": [{"text": MESSAGE}],
       "research": [{"about": "Libbey's Coastal Kitchen", "loc": "Stevensville, MD",
                     "find": "sister restaurants",
                     "carry": "they use our competitor, Margins Edge, they are satisfied"}]}


def test_the_bar_adds_the_venue_then_finds_its_sisters(monkeypatch):
    asked = _bar(monkeypatch, OUT, {"added": [{"lead_id": "K1", "name": "Knoxie's Table",
                                               "changed": ["added"], "undo_id": None}],
                                    "skipped": [], "note": ""})
    got = crm.assist_update(crm.AssistRequest(text=MESSAGE))
    assert asked and asked[0]["about"] == "Libbey's Coastal Kitchen"
    assert [a["name"] for a in got["applied"]] == ["Libbey's Coastal Kitchen", "Knoxie's Table"]
    assert "Found and added for Libbey's Coastal Kitchen: Knoxie's Table." in got["reply"]
    assert got["question"] is None          # it doesn't ask for names it was told to find


def test_a_search_that_finds_nothing_says_so(monkeypatch):
    _bar(monkeypatch, OUT, {"added": [], "skipped": [], "note": "No group found."})
    got = crm.assist_update(crm.AssistRequest(text=MESSAGE))
    assert "found none. No group found." in got["reply"]


def test_the_bar_is_told_to_research_not_to_ask():
    assert "research" in assist.BAR_SCHEMA["required"]
    assert "Never ask the salesperson for the names" in assist.BAR_SYSTEM
    # The inbox reader shares SCHEMA and must never research from strangers' mail.
    assert "research" not in assist.SCHEMA["properties"]
