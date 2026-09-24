"""Replies from bars that need something back: an opt-out, or a question.

An opt-out ("please stop emailing me") puts the address on the do-not-email
list for good — every send path checks it, and Undo doesn't lift it — and
marks the lead dead. A question or real interest gets a reply drafted
overnight for the owner to review and send in the morning; it is never sent by
itself, and when it is sent it threads under their email. Fixtures from
test_inbox.py.
"""
import json
import sys
import types
from contextlib import contextmanager

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402
import inbox  # noqa: E402
import mailer  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from test_inbox import RAW, _wire  # noqa: E402

QUESTION = RAW.replace(
    b"Hello, Thank you for reaching out but the person you are trying to connect with is no "
    b"longer with the company.", b"Hi Stephan, how much is it per month, and does it work "
    b"with two locations?")
STOP = RAW.replace(b"Hello, Thank you for reaching out", b"Please take us off your list")


def _cols(row):
    p = row["params"]
    return {"body_text": p[9], "opt_out": p[10], "needs_reply": p[11],
            "draft": json.loads(p[12]) if p[12] else None}


# ── the pass ────────────────────────────────────────────────────────────────

def test_a_question_gets_a_reply_drafted_and_nothing_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda *a, **k: sent.append(a))
    store = _wire(monkeypatch, [QUESTION], lambda m, ids: {
        "reply": "They asked the price.", "applied": [], "skipped": [],
        "opt_out": False, "needs_reply": True})
    monkeypatch.setattr(crm, "_reply_draft_for", lambda mail, lead_id: {
        "subject": "Re: following up", "body": "Hi — $29.99 a month...",
        "to": mail["from_addr"], "lead_id": lead_id})
    crm.process_inbox()
    cols = _cols(store["recorded"][0])
    assert cols["needs_reply"] is True and cols["opt_out"] is False
    assert cols["draft"]["lead_id"] == "CAT" and cols["draft"]["to"] == "bpeterson@fbrmgmt.com"
    assert "how much is it per month" in cols["body_text"]
    assert sent == []                                   # never sent by itself


def test_an_opt_out_is_recorded_and_writes_through_its_own_path(monkeypatch):
    store = _wire(monkeypatch, [STOP], lambda m, ids: {
        "reply": "They asked to be taken off the list.", "applied": [], "skipped": [],
        "opt_out": True, "needs_reply": False})
    recorded = {}
    monkeypatch.setattr(crm, "_record_opt_out", lambda mail, ids: recorded.update(ids=ids) or [
        {"lead_id": "CAT", "name": "Mean Eyed Cat", "changed": ["asked not to be emailed"],
         "undo_id": "U9"}])
    drafts = []
    monkeypatch.setattr(crm, "_reply_draft_for", lambda *a: drafts.append(a))
    out = crm.process_inbox()
    assert out["updated"] == 1 and recorded["ids"] == ["CAT", "LAVACA"]
    assert _cols(store["recorded"][0])["opt_out"] is True
    assert drafts == []                                  # nobody drafts a reply to a "stop"


def test_the_words_alone_make_an_opt_out_whatever_the_model_said(monkeypatch):
    captured = {}

    class Cur:
        def execute(self, sql, params=()):
            self._rows = [{"id": "CAT", "name": "Mean Eyed Cat", "status": "contacted",
                           "email": "bpeterson@fbrmgmt.com", "notes": ""}]

        def fetchall(self):
            return self._rows

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_today", lambda: "2026-09-25")
    monkeypatch.setattr(crm, "_touch_counts", lambda cursor, ids: {})
    monkeypatch.setattr(crm, "_claude_json", lambda *a, **k: {
        "reply": "", "question": None, "opt_out": False, "needs_reply": True,
        "changes": [{"lead": "L1", "status": "warm"}]})
    monkeypatch.setattr(crm, "_apply_proposed", lambda proposed, *a, **k:
                        captured.update(proposed=proposed) or ([], []))
    out = crm._read_reply(inbox.parse(STOP), ["CAT"])
    assert out["opt_out"] is True and out["needs_reply"] is False
    assert captured["proposed"] == []                    # the model's "warm" is not applied


def test_recording_an_opt_out_suppresses_the_address_and_kills_the_lead(monkeypatch):
    writes = []

    class Cur:
        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            writes.append((s, params))
            self._row = {"id": "CAT", "name": "Mean Eyed Cat"} if s.startswith("SELECT") else None

        def fetchone(self):
            return self._row

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_snapshot", lambda cursor, lead, action: "U1")
    applied = crm._record_opt_out(inbox.parse(STOP), ["CAT"])
    sup = next(p for s, p in writes if s.startswith("INSERT INTO crm_suppressions"))
    assert sup[1] == "bpeterson@fbrmgmt.com" and "asked not to be emailed" in sup[2]
    upd = next(s for s, p in writes if s.startswith("UPDATE crm_leads"))
    assert "status = 'dead'" in upd and "followup_date = NULL" in upd
    assert applied[0]["undo_id"] == "U1" and "on the do-not-email list" in applied[0]["changed"]


# ── sending ─────────────────────────────────────────────────────────────────

def _send_db(monkeypatch, suppressed=None, writes=None):
    lead = {"id": "CAT", "name": "Mean Eyed Cat", "email": "bpeterson@fbrmgmt.com",
            "status": "contacted", "email_date": None, "attempts": 1}

    class Cur:
        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            if writes is not None:
                writes.append((s, params))
            if s.startswith("SELECT reason FROM crm_suppressions"):
                self._row = {"reason": suppressed} if suppressed else None
            elif s.startswith("SELECT * FROM crm_leads") or s.startswith("SELECT * FROM crm_counters"):
                self._row = lead
            else:
                self._row = None

        def fetchone(self):
            return self._row

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)


def test_an_opted_out_address_is_never_sent_to(monkeypatch):
    _send_db(monkeypatch, suppressed="asked not to be emailed (2026-09-25, replying to us)")
    monkeypatch.setattr(mailer, "valid_address", lambda a: True)
    monkeypatch.setattr(mailer, "send", lambda *a, **k: pytest.fail("sent to an opt-out"))
    for send_at in (None, "2026-09-26T18:00:00Z"):         # now, or queued
        with pytest.raises(HTTPException) as e:
            crm.send_lead_email("CAT", crm.OutgoingEmail(subject="hi", body="hi",
                                                         send_at=send_at), True)
        assert e.value.status_code == 409 and e.value.detail["error"] == "opted_out"


def test_a_reply_threads_and_is_marked_answered(monkeypatch):
    writes = []
    _send_db(monkeypatch, writes=writes)
    got = {}
    monkeypatch.setattr(mailer, "valid_address", lambda a: True)
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, **k: got.update(k) or {
        "message_id": "<new@my86d.com>", "to": to, "saved_to": "Sent"})
    monkeypatch.setattr(crm, "_record_email_sent", lambda *a, **k: "U1")
    monkeypatch.setattr(crm, "_remember_sent", lambda *a, **k: None)
    monkeypatch.setattr(crm, "_lead_row", lambda row: dict(row))
    monkeypatch.setattr(crm, "_counters_row", lambda row: dict(row))
    crm.send_lead_email("CAT", crm.OutgoingEmail(subject="Re: following up", body="Hi",
                                                 in_reply_to="<reply-1@fbrmgmt.com>"), True)
    assert got["in_reply_to"] == "<reply-1@fbrmgmt.com>"
    assert any(s.startswith("UPDATE crm_inbox SET replied_at") and p[1] == "<reply-1@fbrmgmt.com>"
               for s, p in writes)


def test_the_threading_headers_go_on_the_message(monkeypatch):
    captured = {}

    class Smtp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, m): captured["m"] = m

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", Smtp)
    monkeypatch.setattr(mailer, "PORT", 465)
    monkeypatch.setattr(mailer, "USER", "Stephan@my86d.com")
    monkeypatch.setattr(mailer, "PASSWORD", "x")
    monkeypatch.setattr(mailer, "file_copy", lambda m: ("Sent", None))
    mailer.send("jed@fbrmgmt.com", "Re: hi", "Hello", in_reply_to="<reply-1@fbrmgmt.com>")
    assert captured["m"]["In-Reply-To"] == "<reply-1@fbrmgmt.com>"
    assert captured["m"]["References"] == "<reply-1@fbrmgmt.com>"
    mailer.send("jed@fbrmgmt.com", "hi", "Hello", in_reply_to="not a message id\r\nBcc: x")
    assert captured["m"]["In-Reply-To"] is None           # header injection can't ride in


# ── the reply draft ─────────────────────────────────────────────────────────

def test_a_reply_is_drafted_from_their_own_words(monkeypatch):
    ask = crm._reply_ask({"from_name": "Jed", "from_addr": "jed@fbrmgmt.com",
                          "subject": "following up", "body_text": "How much per month?"})
    assert 'Subject: "Re: following up"' in ask and "How much per month?" in ask
    assert "never guess" in ask
    again = crm._reply_ask({"subject": "Re: following up", "body_text": "x"})
    assert 'Subject: "Re: following up"' in again and "Re: Re:" not in again
