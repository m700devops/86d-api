"""Call notes (and quick-add) read the calendar and remember who to ask for.

The notes reader used to get the notes and nothing else — no today, no
calendar — and a rule reading '"Monday" is 3 unless told otherwise', true only
on a Friday. It now gets today and the next two weeks spelled out, plus the
lead's recent history, and returns the actual date. And `contact` — shown
everywhere as "ask for X" — used to be whoever picked up: a bartender who
said "the owner is Brent" became the person to ask for next time.

The write path is exercised against test_quick_add's fake cursor, which
enforces one param per %s and applies each UPDATE onto a row dict.
"""
import sys
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402
from test_quick_add import _FakeCursor, _lead  # noqa: E402

TODAY = "2026-09-25"   # a Friday


def _apply(extracted, raw="raw notes"):
    cur = _FakeCursor()
    return crm._apply_call_notes(cur, _lead(), extracted, raw, "call", TODAY,
                                 "2026-09-25T12:00:00Z")


# ── what the model is given ─────────────────────────────────────────────────

def test_the_notes_reader_gets_today_the_calendar_and_the_lead(monkeypatch):
    sent = {}
    monkeypatch.setattr(crm, "_ask_claude", lambda system, user, **k: sent.update(
        system=system, user=user) or {})
    lead = {"name": "Workhorse Bar", "loc": "Austin, TX", "contact": "Lesley",
            "last_outcome": "gatekeeper",
            "notes": "Auto-sourced\n[2026-09-24] call · attempt 1: Spoke with Lesley. Owner is Brent."}
    crm._debrief_extract("got Brent this time, call him back Tuesday", lead, TODAY)
    user = sent["user"]
    assert "TODAY: 2026-09-25 (Friday)" in user
    assert "2026-09-29 Tuesday" in user                   # a lookup, not arithmetic
    assert "Asking for: Lesley" in user and "Owner is Brent" in user
    assert user.rstrip().endswith("call him back Tuesday")
    assert '"Monday" is 3' not in sent["system"]           # the Friday-only rule is gone
    assert '"ask_for"' in sent["system"] and '"followup_date"' in sent["system"]


def test_quick_add_gets_the_calendar_too(monkeypatch):
    sent = {}
    monkeypatch.setattr(crm, "_ask_claude", lambda system, user, **k: sent.update(user=user) or {})
    crm._quick_add_extract("Olde Town Tavern, spoke to Taylor, call Monday", TODAY)
    assert "TODAY: 2026-09-25 (Friday)" in sent["user"] and "2026-09-28 Monday" in sent["user"]


# ── what gets written ───────────────────────────────────────────────────────

def test_the_date_off_the_calendar_is_the_follow_up():
    updated, applied, _, _ = _apply({"status": "warm", "outcome": "callback",
                                     "followup_date": "2026-09-29", "summary": "Brent said Tuesday."})
    assert applied["followup_date"] == updated["followup_date"] == "2026-09-29"
    assert "followup_set_by" not in applied


def test_a_date_in_the_past_or_nonsense_is_dropped_for_the_ladder():
    for bad in ("2026-09-01", "next tuesday", "2031-01-01"):
        _, applied, _, _ = _apply({"outcome": "voicemail", "followup_date": bad,
                                   "summary": "left a voicemail"})
        assert applied["followup_set_by"].startswith("cadence"), bad


def test_the_decision_maker_is_who_we_ask_for_next_time():
    updated, applied, _, _ = _apply({
        "status": "contacted", "outcome": "gatekeeper",
        "spoke_to": "Lesley (bartender)", "ask_for": "Brent (owner)",
        "summary": "Lesley says Brent owns it and is in Tuesdays.",
        "best_time": "Tuesdays after 2pm", "current_setup": "Brent counts on a clipboard",
        "objection": "Brent does all the ordering himself", "next_step": "Call Brent Tuesday"})
    assert updated["contact"] == "Brent (owner)"
    note = updated["notes"].splitlines()[-1]
    for part in ("Spoke to: Lesley (bartender)", "Best time: Tuesdays after 2pm",
                 "How they do it now: Brent counts on a clipboard",
                 "Objection: Brent does all the ordering himself", "Next: Call Brent Tuesday"):
        assert part in note, part
    assert applied["details"]["Best time"] == "Tuesdays after 2pm"


def test_spoke_to_is_not_repeated_when_it_is_who_we_ask_for():
    updated, _, _, _ = _apply({"outcome": "answered", "spoke_to": "Brent (owner)",
                               "ask_for": "Brent (owner)", "summary": "Talked to Brent."})
    assert updated["contact"] == "Brent (owner)"
    assert "Spoke to:" not in updated["notes"].splitlines()[-1]


def test_the_older_contact_key_still_works():
    # What the AI bar passes when it logs a call.
    updated, _, _, _ = _apply({"outcome": "answered", "contact": "Sarah", "summary": "ok"})
    assert updated["contact"] == "Sarah"
