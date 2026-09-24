"""Opening an email attempt shows the email that was sent.

Only the subject used to survive a send, as a line in the notes; the body was
gone the moment it left. It's now kept per attempt in crm_sent_emails, and
older attempts are recovered from whatever still holds them. Fake cursors as
in test_tries.py.
"""
import sys
import types
from contextlib import contextmanager

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Cursor:
    def __init__(self, answers):
        self.answers, self.seen, self._last = answers, [], None

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.seen.append((s, params))
        for prefix, rows in self.answers:
            if s.startswith(prefix):
                self._last = rows
                return
        self._last = []

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last or [])


def _db(monkeypatch, cursor):
    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(crm, "get_db", db)


TOUCH = {"id": "T1", "lead_id": "L1", "kind": "email", "at": "2026-09-24T19:51:00+00:00"}
NOTES = ("Auto-sourced 2026-09-15 · bar · score 15\n"
         "[2026-09-25] call · attempt 1: Spoke with Lesley at the bar.\n"
         "[2026-09-25] email to workhorsebar@gmail.com: inventory counting app lesley mentioned")


def test_a_kept_email_comes_back_whole(monkeypatch):
    cur = _Cursor([
        ("SELECT * FROM crm_touches", [TOUCH]),
        ("SELECT * FROM crm_sent_emails", [{
            "to_addr": "workhorsebar@gmail.com", "subject": "Quick one",
            "body": "Hi Lesley,\n\nThanks for the chat.", "sent_at": TOUCH["at"]}]),
    ])
    _db(monkeypatch, cur)
    m = crm.sent_email("L1", "T1", True)
    assert m == {"to": "workhorsebar@gmail.com", "subject": "Quick one",
                 "body": "Hi Lesley,\n\nThanks for the chat.",
                 "sent_at": TOUCH["at"], "complete": True}


def test_a_scheduled_send_is_recovered_from_the_queue(monkeypatch):
    cur = _Cursor([
        ("SELECT * FROM crm_touches", [TOUCH]),
        ("SELECT * FROM crm_sent_emails", []),
        ("SELECT to_addr, subject, body, sent_at FROM crm_scheduled_emails", [
            {"to_addr": "a@b.com", "subject": "Old", "body": "Not this one",
             "sent_at": "2026-09-20T19:51:00+00:00"},
            {"to_addr": "a@b.com", "subject": "Held for 2pm", "body": "Hi there",
             "sent_at": "2026-09-24T19:52:30Z"}]),
    ])
    _db(monkeypatch, cur)
    m = crm.sent_email("L1", "T1", True)
    assert (m["subject"], m["body"], m["complete"]) == ("Held for 2pm", "Hi there", True)


def test_an_old_send_now_shows_its_subject_and_says_the_text_is_gone(monkeypatch):
    cur = _Cursor([
        ("SELECT * FROM crm_touches", [TOUCH]),
        ("SELECT * FROM crm_sent_emails", []),
        ("SELECT to_addr, subject, body, sent_at FROM crm_scheduled_emails", []),
        ("SELECT notes FROM crm_leads", [{"notes": NOTES}]),
    ])
    _db(monkeypatch, cur)
    m = crm.sent_email("L1", "T1", True)
    # The note is dated in the CRM's day (Manila, already the 25th); the touch
    # in UTC (the 24th). A day apart is still the same send.
    assert m["to"] == "workhorsebar@gmail.com"
    assert m["subject"] == "inventory counting app lesley mentioned"
    assert m["body"] is None and m["complete"] is False


def test_a_call_is_not_an_email(monkeypatch):
    _db(monkeypatch, _Cursor([("SELECT * FROM crm_touches", [{**TOUCH, "kind": "call"}])]))
    with pytest.raises(HTTPException) as e:
        crm.sent_email("L1", "T1", True)
    assert e.value.status_code == 404


def test_another_leads_touch_is_not_found(monkeypatch):
    cur = _Cursor([("SELECT * FROM crm_touches", [])])
    _db(monkeypatch, cur)
    with pytest.raises(HTTPException):
        crm.sent_email("OTHER", "T1", True)
    sql, params = cur.seen[0]
    assert "lead_id = %s" in sql and params == ("T1", "OTHER")


def test_sending_keeps_the_body_against_its_attempt(monkeypatch):
    lead = {k: None for k in crm.LEAD_COLUMNS}
    lead.update(id="L1", name="Workhorse Bar", status="contacted", attempts=1,
                email="workhorsebar@gmail.com")
    cur = _Cursor([("SELECT * FROM crm_leads WHERE id", [lead])])
    monkeypatch.setattr(crm, "_snapshot", lambda *a, **k: "U1")
    monkeypatch.setattr(crm, "_record_touch", lambda *a, **k: "T9")
    monkeypatch.setattr(crm, "_attach_touch", lambda *a, **k: None)
    monkeypatch.setattr(crm, "_load_counters_locked", lambda *a, **k: None)
    crm._record_email_sent(cur, "L1", "workhorsebar@gmail.com", " Quick one ",
                           "2026-09-25", "2026-09-24T19:51:00+00:00", body="Hi Lesley")
    inserts = [p for s, p in cur.seen if s.startswith("INSERT INTO crm_sent_emails")]
    assert inserts == [("T9", "L1", "workhorsebar@gmail.com", "Quick one", "Hi Lesley",
                        "2026-09-24T19:51:00+00:00")]


def test_note_lines_far_from_the_send_are_not_matched():
    assert crm._email_from_notes(NOTES, "2026-09-10T00:00:00+00:00") is None
    assert crm._email_from_notes(None, TOUCH["at"]) is None
    assert crm._email_from_notes(NOTES, "not a date") is None
