"""Ask AI: questions about the CRM, answered from a snapshot of it.

The model never writes SQL and never sees anything but CRM rows. These tests
check what it's handed (dates in the operator's own time, short aliases, the
latest note) and that what comes back is mapped onto real leads only — an
alias the model made up must not turn into a row on screen.
"""
import sys
import types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402

MANILA = ZoneInfo("Asia/Manila")


def _lead(id, name, **kw):
    row = dict(id=id, name=name, loc="Nashville, TN", status="contacted",
               last_outcome="voicemail", attempts=1,
               last_touch_at="2026-09-23T06:00:00Z", followup_date=None,
               contact=None, email=None, phone="615-742-9095",
               notes="[2026-09-22] call · attempt 1: first\n[2026-09-23] call · attempt 2: left a voicemail")
    row.update(kw)
    return row


def test_snapshot_uses_operator_local_time_and_short_aliases():
    leads = [_lead("abc-long-id-1", "Pig & the Sprout")]
    touches = [dict(lead_id="abc-long-id-1", kind="call", outcome="voicemail",
                    attempt=2, at="2026-09-23T06:00:00Z")]
    now = datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc)
    text, back = crm._ask_snapshot(leads, touches, now, MANILA)

    assert back == {"L1": "abc-long-id-1"}
    assert "abc-long-id-1" not in text          # aliases, not long ids
    assert "TODAY: 2026-09-24 Thursday" in text  # 3am UTC is 11am Thursday in Manila
    # 06:00 UTC on the 23rd is 2:00pm Wednesday in Manila
    assert "2026-09-23 Wed 2:00pm | L1 | call | voicemail | 2" in text
    # Only the latest note, not the whole history
    assert "left a voicemail" in text and "first" not in text


def test_snapshot_touch_for_unknown_lead_does_not_crash():
    text, _ = crm._ask_snapshot([], [dict(lead_id="gone", kind="call", outcome="answered",
                                          attempt=1, at="2026-09-23T06:00:00Z")],
                                datetime.now(timezone.utc), MANILA)
    assert "| ? | call |" in text


class _Cursor:
    def __init__(self, leads, touches):
        self.results = [leads, touches]
        self.sql = []
    def execute(self, sql, params=None):
        self.sql.append(sql)
    def fetchall(self):
        return self.results.pop(0)


class _Conn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self.cur
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_ask_maps_aliases_back_and_drops_invented_ones(monkeypatch):
    leads = [_lead("id-1", "Pig & the Sprout"), _lead("id-2", "Murphy's Pub")]
    cur = _Cursor(leads, [])
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    seen = {}

    def fake_claude(system, user, **kw):
        seen["user"] = user
        return {"answer": "One voicemail yesterday.", "leads": ["L2", "L99", "L2"]}
    monkeypatch.setattr(crm, "_ask_claude", fake_claude)

    out = crm.ask_crm(crm.AskCRM(question="who got a voicemail yesterday?"))
    assert out["answer"] == "One voicemail yesterday."
    assert [l["name"] for l in out["leads"]] == ["Murphy's Pub"]   # L99 invented, L2 deduped
    assert out["leads"][0]["phone_dial"] == "615-742-9095"
    assert "QUESTION: who got a voicemail yesterday?" in seen["user"]
    # Undone dials are excluded, and nothing here writes.
    assert "'undone'" in cur.sql[1]
    assert not any(s.strip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for s in cur.sql)


def test_ask_with_no_answer_from_model_says_so(monkeypatch):
    cur = _Cursor([], [])
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    monkeypatch.setattr(crm, "_ask_claude", lambda *a, **k: {})
    out = crm.ask_crm(crm.AskCRM(question="anything?"))
    assert out["answer"] and out["leads"] == []


# ── /leads views: the CRM tab vs Yet to Contact ─────────────────────────────

class _LeadsCursor:
    """Records list_leads' SQL; answers counts with 0 and the page with []."""
    def __init__(self): self.sql = []; self._last = ""
    def execute(self, sql, params=None): self.sql.append(sql); self._last = sql
    def fetchone(self): return {"n": 0}
    def fetchall(self): return []


def _list(monkeypatch, status):
    cur = _LeadsCursor()
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    out = crm.list_leads(status=status, q=None, limit=100, offset=0)
    return cur, out


def test_open_view_is_worked_and_not_dead(monkeypatch):
    cur, out = _list(monkeypatch, "open")
    page = cur.sql[0]
    assert "status <> 'dead'" in page and "NOT (status = 'new' AND last_touch_at IS NULL)" in page
    assert {"open", "untouched", "worked", "all"} <= set(out["counts"])


def test_untouched_view_is_never_contacted(monkeypatch):
    cur, _ = _list(monkeypatch, "untouched")
    assert "(status = 'new' AND last_touch_at IS NULL)" in cur.sql[0]
    assert "NOT (status" not in cur.sql[0].split("WHERE", 1)[1].split("AND (LOWER")[0]


def test_unknown_view_is_rejected(monkeypatch):
    import pytest
    with pytest.raises(crm.HTTPException):
        _list(monkeypatch, "bogus")
