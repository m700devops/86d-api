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


# ── deleting a note off "While you were away" ───────────────────────────────

def _inbox_db(monkeypatch, rows):
    """crm_inbox as a dict. The UPDATE applies the route's own CASE — stamp
    the first delete time, or clear it — and every statement is kept."""
    seen = []

    class Cur:
        def __init__(self):
            self.out = []

        def execute(self, sql, params=()):
            seen.append(sql)
            if sql.lstrip().startswith("UPDATE crm_inbox"):
                dismiss, when, mid = params
                row = rows.get(mid)
                if row is not None:
                    row["dismissed_at"] = (row["dismissed_at"] or when) if dismiss else None
                self.out = [{"message_id": mid, "dismissed_at": row["dismissed_at"]}] if row else []
            elif "MAX(processed_at)" in sql:
                self.out = [{"last": max(r["processed_at"] for r in rows.values())}]
            else:
                self.out = sorted(rows.values(), key=lambda r: r["processed_at"], reverse=True)

        def fetchone(self):
            return self.out[0] if self.out else None

        def fetchall(self):
            return self.out

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass

    @contextmanager
    def db():
        yield Conn()

    monkeypatch.setattr(crm, "get_db", db)
    return seen


def _note(mid, at, **extra):
    now = crm.datetime.now(crm.timezone.utc)
    return {"message_id": mid, "from_addr": "jed@fbrmgmt.com", "from_name": "Jed",
            "subject": "Re: hi", "received_at": None, "status": "updated",
            "result": json.dumps({"reply": "Jed is the new contact",
                                  "applied": [{"lead_id": "L1", "name": "Olde Town",
                                               "changed": ["contact"], "undo_id": "u1"}]}),
            "processed_at": (now - crm.timedelta(hours=at)).isoformat(),
            "lead_ids": "L1", "opt_out": False, "needs_reply": False, "draft": None,
            "replied_at": None, "dismissed_at": None, **extra}


def test_a_deleted_note_leaves_the_list_and_can_be_put_back(monkeypatch):
    rows = {"<a@x>": _note("<a@x>", 1), "<b@x>": _note("<b@x>", 2)}
    seen = _inbox_db(monkeypatch, rows)
    out = crm.inbox_dismiss(crm.InboxNote(message_id="<a@x>"), True)
    assert out["dismissed_at"]
    feed = crm.inbox_feed(72, True)
    assert [i["message_id"] for i in feed["items"]] == ["<b@x>"]
    assert [i["message_id"] for i in feed["deleted"]] == ["<a@x>"]
    assert feed["deleted"][0]["applied"][0]["undo_id"] == "u1"   # the note, whole
    # Deleting only hides the note. The row stays: the reader treats a
    # message it has no row for as new mail, so a real DELETE would bring
    # the email back and apply its changes twice.
    assert not any(q.lstrip().upper().startswith("DELETE") for q in seen)
    crm.inbox_restore(crm.InboxNote(message_id="<a@x>"), True)
    feed = crm.inbox_feed(72, True)
    assert [i["message_id"] for i in feed["items"]] == ["<a@x>", "<b@x>"]
    assert feed["deleted"] == []


def test_deleting_twice_keeps_the_first_time_and_the_newest_deletion_is_first(monkeypatch):
    rows = {"<a@x>": _note("<a@x>", 1), "<b@x>": _note("<b@x>", 2)}
    _inbox_db(monkeypatch, rows)
    first = crm.inbox_dismiss(crm.InboxNote(message_id="<b@x>"), True)["dismissed_at"]
    rows["<b@x>"]["dismissed_at"] = "2026-09-25T01:00:00+00:00"
    assert crm.inbox_dismiss(crm.InboxNote(message_id="<b@x>"), True)["dismissed_at"] \
        == "2026-09-25T01:00:00+00:00"
    assert first
    crm.inbox_dismiss(crm.InboxNote(message_id="<a@x>"), True)
    feed = crm.inbox_feed(72, True)
    assert feed["items"] == []
    assert [i["message_id"] for i in feed["deleted"]] == ["<a@x>", "<b@x>"]


def test_deleting_a_note_that_isnt_there_is_a_404(monkeypatch):
    _inbox_db(monkeypatch, {"<a@x>": _note("<a@x>", 1)})
    with pytest.raises(HTTPException) as e:
        crm.inbox_dismiss(crm.InboxNote(message_id="<gone@x>"), True)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException):
        crm.inbox_restore(crm.InboxNote(message_id="<gone@x>"), True)


def test_the_delete_routes_are_wired():
    routes = {r.path: r.endpoint for r in crm.crm_router.routes if hasattr(r, "endpoint")}
    assert routes["/v1/crm/inbox/dismiss"] is crm.inbox_dismiss
    assert routes["/v1/crm/inbox/restore"] is crm.inbox_restore
