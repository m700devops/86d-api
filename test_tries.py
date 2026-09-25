"""Tries on the Follow-ups tab: every attempt to sell a lead, not just calls.

A bar called once and emailed twice had a TRY of 1, because the column showed
`attempts` — the call-retry ladder's count, which emails deliberately don't
touch (an email must not use up a bar's six tries at being rung). The screen
now counts every touch in `crm_touches` instead, undone ones excluded, and the
details drawer lists each one. Fake cursors stand in for Postgres; the
`database` stub is the same one test_callnow.py uses.
"""
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402


def _row(id, **kw):
    row = {k: None for k in crm.LEAD_COLUMNS}
    row.update(id=id, name="The Barrel House", status="contacted", attempts=1,
               followup_date="2026-09-20", last_outcome="emailed")
    row.update(kw)
    return row


class _Cursor:
    """Answers the queries by their opening words, and remembers the SQL."""

    def __init__(self, answers):
        self.answers = answers
        self.seen = []
        self._last = None

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.seen.append(s)
        for prefix, rows in self.answers:
            if s.startswith(prefix):
                self._last = rows
                return
        raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last or [])


def _db(monkeypatch, cursor):
    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_call_window", lambda *a, **k: None)


def test_tries_counts_every_kind():
    t = crm._tries([("call", 1), ("email", 2)])
    assert t == {"total": 3, "call": 1, "email": 2, "fb": 0}


def test_tries_with_nothing_logged():
    assert crm._tries([]) == {"total": 0, "call": 0, "email": 0, "fb": 0}


def test_follow_ups_try_column_counts_calls_and_emails(monkeypatch):
    cur = _Cursor([
        ("SELECT * FROM crm_leads WHERE followup_date IS NOT NULL", [_row("A")]),
        ("SELECT * FROM crm_leads", []),
        ("SELECT lead_id, kind, COUNT(*)", [
            {"lead_id": "A", "kind": "call", "n": 1},
            {"lead_id": "A", "kind": "email", "n": 2}]),
        ("SELECT COUNT(*) AS n", [{"n": 1}]),
    ])
    _db(monkeypatch, cur)
    out = crm.call_queue()
    lead = out["overdue"][0]
    assert lead["tries"]["total"] == 3          # called once, emailed twice
    assert (lead["tries"]["call"], lead["tries"]["email"]) == (1, 2)
    assert lead["attempts"] == 1                # the call ladder is untouched
    tally = next(q for q in cur.seen if q.startswith("SELECT lead_id, kind"))
    assert "'undone'" in tally                  # an undone touch never counts


def test_a_lead_with_no_touches_shows_zero(monkeypatch):
    cur = _Cursor([
        ("SELECT * FROM crm_leads WHERE followup_date IS NOT NULL", [_row("B")]),
        ("SELECT * FROM crm_leads", []),
        ("SELECT lead_id, kind, COUNT(*)", []),
        ("SELECT COUNT(*) AS n", [{"n": 1}]),
    ])
    _db(monkeypatch, cur)
    assert crm.call_queue()["overdue"][0]["tries"]["total"] == 0


def test_lead_details_list_every_attempt_oldest_first(monkeypatch):
    touches = [
        {"id": "T1", "kind": "call", "outcome": "callback", "at": "2026-09-22T01:14:00+00:00"},
        {"id": "T2", "kind": "email", "outcome": "emailed", "at": "2026-09-23T17:02:00+00:00"},
        {"id": "T3", "kind": "email", "outcome": "emailed", "at": "2026-09-24T15:40:00+00:00"},
    ]
    cur = _Cursor([
        ("SELECT * FROM crm_leads WHERE id", [_row("A")]),
        ("SELECT id, kind, outcome, at FROM crm_touches", touches),
    ])
    _db(monkeypatch, cur)
    lead = crm.get_lead("A")["lead"]
    assert [t["kind"] for t in lead["touches"]] == ["call", "email", "email"]
    # The id is what lets the page open an email attempt's text.
    assert [t["id"] for t in lead["touches"]] == ["T1", "T2", "T3"]
    assert lead["tries"] == {"total": 3, "call": 1, "email": 2, "fb": 0}
    history = next(q for q in cur.seen if q.startswith("SELECT id, kind, outcome, at"))
    assert "'undone'" in history and "ORDER BY at ASC" in history


# ── the CRM tab: WHERE THINGS STAND and REACHED OUT ─────────────────────────
#
# A call and then an email read "Called · yesterday": the page built the line
# from `last_outcome`, which the email overwrote with "emailed". The rows now
# carry the touch log's story instead — the latest touch of any kind, the
# latest CALL kept apart, the latest reply — and REACHED OUT's count.

CALL_THEN_TWO_EMAILS = [
    {"kind": "call", "outcome": "voicemail", "at": "2026-09-22T01:14:00+00:00"},
    {"kind": "email", "outcome": "emailed", "at": "2026-09-23T17:02:00+00:00"},
    {"kind": "email", "outcome": "emailed", "at": "2026-09-24T15:40:00+00:00"},
]


def test_a_call_then_two_emails_is_three_and_keeps_the_call():
    s = crm.touch_story(CALL_THEN_TWO_EMAILS)
    assert s["tries"] == {"total": 3, "call": 1, "email": 2, "fb": 0}
    assert s["last_touch"] == {"kind": "email", "outcome": "emailed",
                               "at": "2026-09-24T15:40:00+00:00"}
    assert s["last_call"] == {"outcome": "voicemail", "at": "2026-09-22T01:14:00+00:00"}
    assert s["last_reply"] is None


def test_order_in_doesnt_matter_and_undone_never_counts():
    rows = list(reversed(CALL_THEN_TWO_EMAILS)) + [
        {"kind": "call", "outcome": "undone", "at": "2026-09-25T02:00:00+00:00"}]
    s = crm.touch_story(rows)
    assert s["tries"]["total"] == 3 and s["last_touch"]["kind"] == "email"
    assert s["last_call"]["outcome"] == "voicemail"


def test_the_newest_reply_is_the_one_kept():
    s = crm.touch_story(CALL_THEN_TWO_EMAILS, [
        {"processed_at": "2026-09-23T20:00:00+00:00", "needs_reply": False},
        {"processed_at": "2026-09-25T03:00:00+00:00", "needs_reply": True, "replied_at": None,
         "opt_out": False}])
    assert s["last_reply"] == {"at": "2026-09-25T03:00:00+00:00", "opt_out": False,
                               "needs_reply": True, "answered": False}


def test_nothing_logged_is_zero_and_empty():
    s = crm.touch_story([])
    assert s["tries"]["total"] == 0 and s["last_touch"] is None and s["last_call"] is None


def test_crm_tab_rows_carry_the_story_and_the_servers_today(monkeypatch):
    cur = _Cursor([
        ("SELECT COUNT(*) AS n FROM crm_leads", [{"n": 2}]),
        ("SELECT * FROM crm_leads WHERE", [_row("A"), _row("B", last_outcome="answered")]),
        ("SELECT lead_id, kind, outcome, at FROM crm_touches",
         [{"lead_id": "A", **t} for t in CALL_THEN_TWO_EMAILS]),
        # One management company's reply is about both venues.
        ("SELECT lead_ids, processed_at", [
            {"lead_ids": "A,B", "processed_at": "2026-09-25T03:00:00+00:00", "opt_out": False,
             "needs_reply": True, "replied_at": None}]),
        ("SELECT status, COUNT(*)", [{"status": "contacted", "n": 2}]),
    ])
    _db(monkeypatch, cur)
    out = crm.list_leads(status="open", limit=100, offset=0)
    a, b = out["leads"]
    assert a["tries"]["total"] == 3 and a["last_touch"]["kind"] == "email"
    assert a["last_call"]["outcome"] == "voicemail"
    assert a["last_reply"]["needs_reply"] and b["last_reply"]["needs_reply"]
    assert b["tries"]["total"] == 0 and b["last_touch"] is None      # no log, no guess
    assert out["today"] == crm._today()
    touches = next(q for q in cur.seen if q.startswith("SELECT lead_id, kind, outcome, at"))
    assert "'undone'" in touches
    inbox = next(q for q in cur.seen if q.startswith("SELECT lead_ids, processed_at"))
    assert "&&" in inbox and "'updated', 'no_change'" in inbox
