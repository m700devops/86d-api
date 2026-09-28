"""What a bar uses today, and "call back around 4pm", kept as data.

"They use our competitor, Margins Edge, they are satisfied" lived only inside a
note line, so "which bars are on MarginEdge?" had no answer. `current_system`
holds it, read by competitors.system_of from what was said. And "call back
around 4pm, ask for Mike" was a date in Follow-ups: now `callback_time` holds
the time on the bar's own clock and the call list puts it on top at four.
"""
import sys
import types
from datetime import datetime

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import pytest  # noqa: E402

import competitors  # noqa: E402
import crm  # noqa: E402
from test_quick_add import _FakeCursor, _lead  # noqa: E402


@pytest.mark.parametrize("said, system", [
    ("they use our competitor, Margins Edge, they are satisfied", "MarginEdge"),
    ("Margin-Edge", "MarginEdge"),
    ("MarginEdge for invoices", "MarginEdge"),
    ("they're on bevspot", "BevSpot"),
    ("R365 does their ordering", "Restaurant365"),
    ("clipboard and a spreadsheet", "Pen and paper"),
    ("an Excel sheet the GM keeps", "Spreadsheet"),
    ("owner counts on paper, orders via BevSpot", "BevSpot"),   # a product beats paper
    ("no system at all, the owner eyeballs it", None),
    ("a bar i like downtown", None),                            # never a guess
    ("the back bar is a mess", None),
    ("", None),
])
def test_what_they_use_is_one_name(said, system):
    assert competitors.system_of(said) == system


def test_only_what_people_said_counts_in_old_notes():
    notes = ("Auto-sourced 2026-09-01 · bar · score 9\nhttps://x.com\n"
             "pours liquor: a spreadsheet of spirits\n"          # bookkeeping: ignored
             "[2026-09-20] call · attempt 1: Spoke to staff · How they do it now: clipboard\n"
             "[2026-09-24] call · attempt 2: Bill says they moved to MarginEdge")
    assert crm.system_from_notes(notes) == "MarginEdge"            # latest said wins
    assert crm.system_from_notes("Auto-sourced · a spreadsheet of beers") is None
    assert crm.system_from_notes("[2026-09-28] Found by the AI: sister of Libbey's\n"
                                 "[2026-09-28] From the call to Libbey's: they use Margins Edge") \
        == "MarginEdge"


def _apply(extracted, raw, lead=None, today="2026-09-28"):
    lead = lead or _lead()
    return crm._apply_call_notes(_FakeCursor(initial_row=lead), lead, extracted, raw, "call",
                                 today, f"{today}T14:00:00Z")


def test_a_logged_call_records_what_they_use():
    updated, applied, _, _ = _apply(
        {"status": "dead", "outcome": "not_interested", "summary": "Bill is happy with it.",
         "objection": "already use Margins Edge"},
        "Talked to Bill the manager, they use our competitor, Margins Edge, they are satisfied.")
    assert updated["current_system"] == applied["current_system"] == "MarginEdge"


def test_a_call_that_says_nothing_about_it_keeps_what_was_known():
    updated, applied, _, _ = _apply({"outcome": "voicemail", "summary": "Left a voicemail."},
                                    "left a voicemail", lead=_lead(current_system="BevSpot"))
    assert updated["current_system"] == "BevSpot" and "current_system" not in applied


# ── call back at four ──────────────────────────────────────────────────────

def _at(monkeypatch, hh, mm, day="2026-09-28"):
    y, mo, d = (int(x) for x in day.split("-"))
    monkeypatch.setattr(crm, "_venue_now", lambda name, off: datetime(y, mo, d, hh, mm))


def test_call_back_at_four_is_today_there_with_the_time(monkeypatch):
    _at(monkeypatch, 10, 30)
    updated, applied, _, _ = _apply(
        {"status": "contacted", "outcome": "gatekeeper", "spoke_to": "Jen", "ask_for": "Mike",
         "best_time": "around 4pm", "callback_time": "16:00", "summary": "Mike out, call at 4."},
        "talked to Jen, manager Mike is not in, call back at around 4pm")
    assert updated["callback_time"] == "16:00" and updated["followup_date"] == "2026-09-28"
    assert updated["contact"] == "Mike"


def test_a_time_already_gone_there_means_tomorrow(monkeypatch):
    _at(monkeypatch, 17, 5)
    updated, _, _, _ = _apply({"outcome": "gatekeeper", "callback_time": "16:00",
                               "summary": "call back at 4"}, "call back at 4pm")
    assert updated["followup_date"] == "2026-09-29"


def test_a_named_day_wins_and_nonsense_is_ignored(monkeypatch):
    _at(monkeypatch, 10, 0)
    updated, _, _, _ = _apply({"outcome": "callback", "followup_date": "2026-10-01",
                               "callback_time": "2:30", "summary": "Thursday at 2:30"},
                              "call Thursday at 2:30")
    assert updated["followup_date"] == "2026-10-01" and updated["callback_time"] == "02:30"
    updated, applied, _, _ = _apply({"outcome": "voicemail", "callback_time": "4pm-ish",
                                     "summary": "vm"}, "left a voicemail",
                                    lead=_lead(callback_time="16:00"))
    assert updated["callback_time"] is None and "callback_time" not in applied   # cleared


@pytest.mark.parametrize("now, due", [
    ((15, 44), False), ((15, 45), True), ((16, 0), True), ((17, 30), True), ((17, 31), False)])
def test_a_callback_is_due_from_a_quarter_before_to_ninety_minutes_after(now, due):
    assert crm.callback_due("2026-09-28", "16:00", datetime(2026, 9, 28, *now)) is due
    assert crm.callback_due("2026-09-29", "16:00", datetime(2026, 9, 28, *now)) is False


def test_the_call_list_puts_a_due_callback_on_top(monkeypatch):
    from test_callnow import _lead as now_lead, _run
    mike = now_lead("The Hideaway", status="contacted", last_touch_at="2026-09-28T13:00",
                    followup_date="2026-09-28", callback_time="16:00", contact="Mike (manager)",
                    tz_name="America/New_York", opening_hours="The Hideaway")
    fresh = now_lead("Fresh Bar", opening_hours="Fresh Bar")
    monkeypatch.setattr(crm, "_venue_now", lambda name, off: datetime(2026, 9, 28, 16, 5))
    d = _run([mike, fresh], ["good", "good"])
    assert [l["name"] for l in d["ready"]] == ["The Hideaway", "Fresh Bar"]
    assert d["ready"][0]["callback"] and "4:00pm" in d["ready"][0]["call_window"]["hint"]
    assert d["callbacks_count"] == 1 and d["headline"].startswith("Call The Hideaway back now")
