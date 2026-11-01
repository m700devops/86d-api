"""Contact Form: bars reached only through the contact form on their own
website. Nobody was spoken to, so they get their own CRM view, apart from
Open, until a real call or email is logged — and the AI files them there.
"""
import sys
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import assist  # noqa: E402
import crm  # noqa: E402
from test_quick_add import _FakeCursor, _lead  # noqa: E402


def test_contact_form_is_its_own_view_and_open_leaves_it_out():
    assert "form" in crm.LEAD_VIEWS
    assert "kind = 'form'" in crm.LEAD_VIEWS["form"]
    assert "status <> 'dead'" in crm.LEAD_VIEWS["form"]
    # Open excludes the form-only leads; a real touch (kind <> 'form') puts
    # one back in Open.
    assert f"NOT {crm._FORM_ONLY}" in crm.LEAD_VIEWS["open"]
    assert "kind <> 'form'" in crm._FORM_ONLY
    # Undone touches never count either way.
    assert crm._FORM_ONLY.count("IS DISTINCT FROM 'undone'") == 2


def test_tries_count_contact_forms():
    assert crm._tries([("call", 1), ("form", 2)]) == {
        "total": 3, "call": 1, "email": 0, "fb": 0, "form": 2}


def test_logging_a_contact_form_is_not_a_call():
    cur = _FakeCursor(_lead())
    # The model may still say "answered" — a form never is.
    extracted = {"status": "contacted", "outcome": "answered",
                 "summary": "Sent a message through their contact form."}
    updated, applied, undo_id, _ = crm._apply_call_notes(
        cur, _lead(), extracted, "sent their contact form", "form",
        "2026-11-01", "2026-11-01T12:00:00Z")
    assert updated["status"] == "contacted"
    assert updated["last_outcome"] == "form_sent"
    assert updated["attempts"] == 0          # the call ladder is untouched
    assert "call_date" not in applied
    assert "followup_date" not in applied    # no cadence for a form
    assert "outcome" not in applied
    assert undo_id
    # No daily quota for forms: only the lifetime ticker is spent.
    counters_sql = [s for s, _ in cur.executed if s.startswith("UPDATE crm_counters")][0]
    assert "daily_" not in counters_sql.split("touch_ticker_remaining")[0]


def test_a_contact_form_never_downgrades_a_warm_lead():
    cur = _FakeCursor(_lead(status="warm"))
    updated, _, _, _ = crm._apply_call_notes(
        cur, _lead(status="warm"), {"status": "contacted"}, "contact form sent",
        "form", "2026-11-01", "2026-11-01T12:00:00Z")
    assert updated["status"] == "warm"


def test_the_ai_can_log_a_form_and_never_gives_it_an_outcome():
    from test_assist import TODAY, _change
    from test_assist import _lead as _book_lead
    assert "form" in assist.KINDS
    assert "form" in str(assist.SCHEMA)
    text = "barrel house: sent them a message through their contact form"
    clean, _ = assist.clean_change(
        _change(logged={"kind": "form", "outcome": "answered",
                        "summary": "Contact form sent", "their_words": text}),
        _book_lead(), text, TODAY)
    assert clean["logged"]["kind"] == "form"
    assert clean["logged"]["outcome"] is None
    assert "contact-form message" in " ".join(
        assist.describe({"logged": {"kind": "form", "outcome": None}}))
    assert 'kind "form"' in assist.BAR_RULES


def test_new_bars_from_a_contact_form_are_filed_as_forms():
    yes = ["Sent Joe's Bar a message through their contact form",
           "filled out the web form for The Rusty Nail",
           "used the contact-form on their site", "left a note via the form on their website"]
    no = ["Called Joe's Bar, left a voicemail", "emailed the owner",
          "the manager said to fill out a form for vendors?"]
    assert all(assist.mentions_contact_form(t) for t in yes)
    assert not any(assist.mentions_contact_form(t) for t in no)
