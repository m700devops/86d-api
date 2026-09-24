"""The Follow-ups AI bar: plain English in, checked CRM changes out.

assist.py is pure, so its gate is tested directly: what the model proposes
only gets written if the operator actually said it. The route is tested with
a fake database and a fake model, `_claude_json` against a fake httpx.post so
the exact request is visible, and undo for the one way an AI edit's undo
could go wrong: marking somebody's real call as undone. `database` is stubbed
the same way test_callnow.py stubs it.
"""
import sys
import types
from contextlib import contextmanager
from datetime import date

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import assist  # noqa: E402
import crm  # noqa: E402
from fastapi import HTTPException  # noqa: E402

TODAY = date(2026, 9, 24)   # a Thursday


def _lead(**kw):
    row = {k: None for k in crm.LEAD_COLUMNS}
    row.update(id="L-barrel", name="The Barrel House", loc="Denver, CO", status="warm",
               contact=None, phone="720-555-0100", email=None, followup_date="2026-09-22",
               last_touch_at="2026-09-22T01:00:00Z", manager_name=None,
               notes="[2026-09-22] call · attempt 1: Spoke with Laura, the GM.")
    row.update(kw)
    return row


def _change(**kw):
    c = {k: None for k in ("name", "loc", "status", "contact", "phone", "email",
                           "followup_date", "note", "logged")}
    c.update(lead="L1", clear_followup=False)
    c.update(kw)
    return c


# ── the gate ──────────────────────────────────────────────────────────────

def test_the_calendar_turns_friday_into_a_lookup():
    table = assist.dates_table(TODAY)
    assert "2026-09-24 Thursday (today)" in table
    assert "2026-09-25 Friday (tomorrow)" in table
    assert len(table.splitlines()) == 15


def test_an_email_the_operator_never_typed_is_refused():
    clean, problems = assist.clean_change(
        _change(email="laura@barrelhouse.com"), _lead(), "barrel house, laura wants a demo", TODAY)
    assert "email" not in clean
    assert problems and "isn't in what you typed" in problems[0]


def test_an_email_they_typed_is_kept():
    clean, _ = assist.clean_change(
        _change(email="Laura@BarrelHouse.com"), _lead(),
        "barrel house: her email is laura@barrelhouse.com", TODAY)
    assert clean["email"] == "Laura@BarrelHouse.com"


def test_a_phone_is_matched_as_digits_whatever_the_formatting():
    clean, problems = assist.clean_change(
        _change(phone="720-242-9667"), _lead(), "her cell is (720) 242 9667", TODAY)
    assert clean["phone"] == "720-242-9667" and not problems
    clean, problems = assist.clean_change(
        _change(phone="720-242-0000"), _lead(), "her cell changed", TODAY)
    assert "phone" not in clean and problems


def test_a_contact_may_come_from_the_leads_own_notes_but_never_from_nowhere():
    clean, _ = assist.clean_change(_change(contact="Laura"), _lead(),
                                   "make the GM the contact", TODAY)
    assert clean["contact"] == "Laura"          # "Spoke with Laura, the GM." is in its notes
    clean, problems = assist.clean_change(_change(contact="Mike"), _lead(),
                                          "make the owner the contact", TODAY)
    assert "contact" not in clean and problems


def test_follow_up_dates():
    clean, _ = assist.clean_change(_change(followup_date="2026-09-25"), _lead(), "friday", TODAY)
    assert clean["followup_date"] == "2026-09-25"
    clean, problems = assist.clean_change(_change(followup_date="2026-09-20"), _lead(), "x", TODAY)
    assert "followup_date" not in clean and "past" in problems[0]
    clean, _ = assist.clean_change(_change(clear_followup=True), _lead(), "they said no", TODAY)
    assert clean["followup_date"] is None       # None = clear it


def test_no_ops_are_dropped_quietly():
    clean, problems = assist.clean_change(_change(status="warm"), _lead(), "still warm", TODAY)
    assert clean == {} and problems == []


def test_a_logged_calls_words_are_verbatim_or_the_whole_message():
    text = "Called barrel house, left a voicemail. Olde Town said no."
    clean, _ = assist.clean_change(_change(logged={
        "kind": "call", "outcome": "voicemail", "summary": "Left a voicemail.",
        "their_words": "Called barrel house, left a voicemail."}), _lead(), text, TODAY)
    assert clean["logged"]["their_words"] == "Called barrel house, left a voicemail."
    clean, _ = assist.clean_change(_change(logged={
        "kind": "call", "outcome": "voicemail", "summary": "x",
        "their_words": "They left a message for the bar"}), _lead(), text, TODAY)
    assert clean["logged"]["their_words"] == text    # a paraphrase is never saved as theirs


def test_the_snapshot_puts_follow_ups_first_and_marks_the_open_row():
    leads = [
        {"id": "untouched", "name": "Quiet Pub", "status": "new", "notes": "Auto-sourced ..."},
        {"id": "due", "name": "The Barrel House", "status": "warm",
         "followup_date": "2026-09-20", "last_touch_at": "2026-09-19T00:00:00Z",
         "notes": "[2026-09-19] call: Laura wants pricing"},
    ]
    text, back = assist.snapshot(leads, {"due": {"total": 3, "call": 1, "email": 2}},
                                 "2026-09-24", focus_id="due")
    rows = text.splitlines()[1:]
    assert rows[0].startswith("L1 | The Barrel House") and "overdue" in rows[0]
    assert "3 (1 call, 2 email)" in rows[0] and rows[0].endswith("<- OPEN ON SCREEN")
    assert "Auto-sourced" not in rows[1]          # a never-called lead's note is bookkeeping
    assert back == {"L1": "due", "L2": "untouched"}


# ── the route ─────────────────────────────────────────────────────────────

class _Cursor:
    def __init__(self, book, row):
        self.book, self.row = book, row
        self.seen = []
        self._rows = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.seen.append((s, list(params or [])))
        if s.startswith("SELECT id, name, loc, status"):
            self._rows = self.book
        elif s.startswith("SELECT lead_id, kind, COUNT(*)"):
            self._rows = []
        elif s.startswith("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE"):
            self._rows = [self.row] if params[0] == self.row["id"] else []
        elif not (s.startswith("INSERT INTO crm_lead_undo") or s.startswith("UPDATE crm_leads")):
            raise AssertionError(f"unexpected SQL: {s[:80]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _wire(monkeypatch, reply):
    row = _lead()
    cur = _Cursor([row], row)

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cur, commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_today", lambda: "2026-09-24")
    monkeypatch.setattr(crm, "_claude_json", lambda system, user, schema: reply)
    return cur


def test_an_edit_is_written_with_its_own_undo_and_a_history_line(monkeypatch):
    cur = _wire(monkeypatch, {"reply": "Moved The Barrel House to Friday.", "question": None,
                              "changes": [_change(followup_date="2026-09-25",
                                                  contact="Laura"),
                                          _change(lead="L99", status="dead")]})
    out = crm.assist_update(crm.AssistRequest(text="barrel house: call laura back friday"), True)
    assert out["applied"][0]["changed"] == ["contact → Laura", "follow-up → 2026-09-25"]
    assert out["skipped"] == [{"lead": None, "why": "couldn't match 'L99' to a lead"}]
    undo = next(p for s, p in cur.seen if s.startswith("INSERT INTO crm_lead_undo"))
    assert undo[2] == "edit (AI bar)" and undo[4] == 0     # an edit spends no counters
    update = next((s, p) for s, p in cur.seen if s.startswith("UPDATE crm_leads"))
    assert "contact = %s" in update[0] and "followup_date = %s" in update[0]
    assert "last_touch_at" not in update[0]                # an edit is not a touch
    assert "[2026-09-24] updated: contact → Laura · follow-up → 2026-09-25" in update[1]


def test_a_call_that_happened_goes_through_the_log_call_path(monkeypatch):
    _wire(monkeypatch, {"reply": "Logged it.", "question": None, "changes": [_change(
        logged={"kind": "call", "outcome": "voicemail", "summary": "Left a voicemail.",
                "their_words": "left laura a voicemail"})]})
    seen = {}

    def fake_apply(cursor, lead, extracted, raw_text, kind, today, now):
        seen.update(extracted=extracted, raw_text=raw_text, kind=kind)
        return lead, {}, "U-call", None

    monkeypatch.setattr(crm, "_apply_call_notes", fake_apply)
    out = crm.assist_update(crm.AssistRequest(text="barrel house — left laura a voicemail"), True)
    assert seen["kind"] == "call" and seen["raw_text"] == "left laura a voicemail"
    assert seen["extracted"]["outcome"] == "voicemail"
    assert out["applied"][0]["undo_id"] == "U-call"


def test_a_question_comes_back_and_nothing_changes(monkeypatch):
    cur = _wire(monkeypatch, {"reply": "", "question": "Which Barrel House — Denver or Austin?",
                              "changes": []})
    out = crm.assist_update(crm.AssistRequest(text="barrel house said yes"), True)
    assert out["question"].startswith("Which Barrel House")
    assert out["applied"] == []
    assert not any(s.startswith("UPDATE") for s, _ in cur.seen)


# ── the model call ──────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        return self._body


def _post_recorder(monkeypatch, *responses):
    import httpx
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"headers": headers, "json": json})
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_the_request_uses_structured_output_and_no_prefill_or_temperature(monkeypatch):
    calls = _post_recorder(monkeypatch, _Resp(200, {"stop_reason": "end_turn", "content": [
        {"type": "thinking", "thinking": ""},
        {"type": "text", "text": '{"reply": "ok", "question": null, "changes": []}'}]}))
    out = crm._claude_json("sys", "msg", assist.SCHEMA, model="claude-opus-5")
    assert out == {"reply": "ok", "question": None, "changes": []}
    body = calls[0]["json"]
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": assist.SCHEMA}
    assert "temperature" not in body                      # current models reject it
    assert [m["role"] for m in body["messages"]] == ["user"]   # and a prefill
    assert body["fallbacks"] == "default"
    assert calls[0]["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"


def test_other_models_get_no_fallback_parameter(monkeypatch):
    calls = _post_recorder(monkeypatch, _Resp(200, {"stop_reason": "end_turn", "content": [
        {"type": "text", "text": '{"reply": "", "question": null, "changes": []}'}]}))
    crm._claude_json("sys", "msg", assist.SCHEMA, model="claude-sonnet-5")
    assert "fallbacks" not in calls[0]["json"] and "anthropic-beta" not in calls[0]["headers"]


def test_a_400_is_retried_once_as_a_plain_request(monkeypatch):
    calls = _post_recorder(
        monkeypatch, _Resp(400, {"error": "schema"}),
        _Resp(200, {"stop_reason": "end_turn", "content": [
            {"type": "text", "text": 'Here: {"reply": "ok", "question": null, "changes": []}'}]}))
    assert crm._claude_json("sys", "msg", assist.SCHEMA, model="claude-opus-5")["reply"] == "ok"
    retry = calls[1]["json"]
    assert "output_config" not in retry and "fallbacks" not in retry
    assert "JSON schema" in retry["system"]


def test_a_refusal_is_an_error_not_an_empty_answer(monkeypatch):
    _post_recorder(monkeypatch, _Resp(200, {"stop_reason": "refusal", "content": []}))
    with pytest.raises(HTTPException) as e:
        crm._claude_json("sys", "msg", assist.SCHEMA, model="claude-opus-5")
    assert e.value.detail["error"] == "ai_declined"


# ── undo ────────────────────────────────────────────────────────────────────

class _UndoCursor:
    def __init__(self, undo):
        self.undo, self.seen, self._last = undo, [], None

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.seen.append(s)
        if s.startswith("SELECT * FROM crm_lead_undo"):
            self._last = self.undo
        elif s.startswith("UPDATE crm_leads SET"):
            self._last = {"id": self.undo["lead_id"]}
        else:
            self._last = None

    def fetchone(self):
        return self._last


def test_undoing_an_ai_edit_never_marks_a_real_call_undone(monkeypatch):
    import json
    cur = _UndoCursor({"id": "U1", "lead_id": "L-barrel", "action": "edit (AI bar)",
                       "snapshot": json.dumps({"contact": None, "followup_date": "2026-09-22"}),
                       "counters_spent": 0, "restored_at": None, "touch_id": None})

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cur, commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_lead_row", lambda row: row)
    crm.undo_touch("U1", True)
    assert not any(s.startswith("UPDATE crm_touches") for s in cur.seen)
    assert any(s.startswith("UPDATE crm_leads SET contact = %s, followup_date = %s")
               for s in cur.seen)
