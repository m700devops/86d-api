"""The plain-English stages, "log any call", the assistant's snapshot, the
practice-call transcript, and the App Store report parsing. All pure or run
against the fake cursor from test_quick_add — no database, no network, no model.
"""
import base64
import sys
import types
from datetime import datetime, timezone

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import appstore  # noqa: E402
import crm  # noqa: E402
from test_quick_add import _Conn, _FakeCursor, _lead, _no_lookup  # noqa: E402


# ---------------------------------------------------------------- stages

def test_a_voicemail_reads_voicemail_left_not_contacted():
    assert crm.friendly_stage("contacted", "voicemail", True) == "voicemail"
    assert crm.STAGE_LABELS["voicemail"] == "Voicemail left"


def test_stage_words_cover_every_status():
    assert crm.friendly_stage("new", None, False) == "todo"
    assert crm.friendly_stage("contacted", "answered", True) == "talked"
    assert crm.friendly_stage("contacted", "gatekeeper", True) == "staff"
    assert crm.friendly_stage("warm", "answered", True) == "callback"
    assert crm.friendly_stage("won", "answered", True) == "signed"
    assert crm.friendly_stage("dead", "not_interested", True) == "no"
    # Retired by the cadence after five voicemails is not "they said no".
    assert crm.friendly_stage("dead", "voicemail", True) == "gaveup"
    assert crm.friendly_stage("contacted", "emailed", True) == "emailed"


def test_lead_rows_carry_the_label():
    row = crm._lead_row(_lead(status="contacted", last_outcome="voicemail",
                              last_touch_at="2026-09-21T10:00:00+00:00"))
    assert row["stage_label"] == "Voicemail left"


# ---------------------------------------------------------------- log any call

def test_keyword_fallback_reads_the_common_outcomes():
    assert crm.guess_outcome("left a voicemail") == "voicemail"
    assert crm.guess_outcome("VM") == "voicemail"
    assert crm.guess_outcome("no answer") == "voicemail"
    assert crm.guess_outcome("not interested, hung up") == "not_interested"
    assert crm.guess_outcome("manager wasn't in, talked to the bartender") == "gatekeeper"
    assert crm.guess_outcome("Dave wants a call back Tuesday") == "callback"
    assert crm.guess_outcome("talked to Dave for ten minutes about his count") == "answered"


class _SelectCursor(_FakeCursor):
    """_FakeCursor plus the SELECTs log-call makes: every lookup finds
    `self.found` (or nothing)."""

    def __init__(self, found=None):
        super().__init__(found)
        self.found = found

    def execute(self, sql, params=None):
        if sql.strip().startswith("SELECT"):
            self.executed.append((sql.strip(), list(params or [])))
            self._last = dict(self.row) if self.found else None
            return
        super().execute(sql, params)

    def fetchall(self):
        return [dict(self.row)] if self.found else []


def test_log_call_on_the_lead_on_screen_keeps_the_voicemail(monkeypatch):
    cur = _SelectCursor(_lead())
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    monkeypatch.setattr(crm, "_ask_claude", lambda *a, **k: {
        "status": "contacted", "outcome": "voicemail", "summary": "Left a voicemail."})
    r = crm.log_call(crm.LogCall(text="vm", lead_id="L1"), True)
    assert r["matched"] == "existing"
    assert r["lead"]["last_outcome"] == "voicemail"
    assert r["lead"]["stage_label"] == "Voicemail left"
    assert r["lead"]["followup_date"]  # the cadence booked the next try


def test_log_call_without_ai_still_logs(monkeypatch):
    cur = _SelectCursor(_lead())
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))

    def down(*a, **k):
        raise crm.HTTPException(status_code=503, detail={"error": "ai_unavailable"})
    monkeypatch.setattr(crm, "_ask_claude", down)
    r = crm.log_call(crm.LogCall(text="no answer, left a message", lead_id="L1"), True)
    assert r["lead"]["last_outcome"] == "voicemail"


def test_log_call_finds_an_existing_lead_by_phone(monkeypatch):
    cur = _SelectCursor(_lead(phone="+1-615-742-9095"))
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    monkeypatch.setattr(crm, "_ask_claude", lambda *a, **k: {
        "name": "Murphys", "phone": "(615) 742-9095", "outcome": "answered",
        "status": "contacted", "next_step": "Email Dave", "summary": "Talked to Dave."})
    r = crm.log_call(crm.LogCall(text="murphys 615 742 9095 talked to dave"), True)
    assert r["matched"] == "existing" and r["match_reason"] == "same phone number"
    assert "Next step: Email Dave" in r["lead"]["notes"]


def test_log_call_makes_a_new_lead_when_nothing_matches(monkeypatch):
    cur = _SelectCursor(None)
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    _no_lookup(monkeypatch)
    monkeypatch.setattr(crm, "_ask_claude", lambda *a, **k: {
        "name": "The Rusty Nail", "loc": "Portland, OR", "outcome": "callback",
        "status": "warm", "followup_in_days": 2, "summary": "Wants a demo."})
    r = crm.log_call(crm.LogCall(text="The Rusty Nail, Portland — wants a demo"), True)
    assert r["matched"] == "new"
    assert r["lead"]["name"] == "The Rusty Nail"
    assert r["lead"]["stage_label"] == "Call back"


# ---------------------------------------------------------------- assistant

def test_assistant_snapshot_is_in_the_operators_days():
    now = datetime(2026, 9, 22, 3, 0, tzinfo=crm._operator_tz())
    touches = [{"at": "2026-09-21T01:00:00+00:00", "kind": "call", "outcome": "voicemail",
                "attempt": 1, "id": "L1", "name": "Murphy's Pub", "loc": "Nashville, TN",
                "status": "contacted", "last_outcome": "voicemail",
                "followup_date": "2026-09-23", "notes": "[2026-09-21] call: left vm"}]
    followups = [{"id": "L2", "name": "Rusty Nail", "loc": "Portland, OR", "status": "warm",
                  "last_outcome": "callback", "followup_date": "2026-09-21", "phone": "503",
                  "notes": None}]
    text, ids = crm.format_assistant_context(now, touches, followups, [], {"active": 2},
                                             1, [])
    # 01:00 UTC on the 21st is 9am on the 21st in Manila: "yesterday" from the 22nd.
    assert "yesterday 9:00am · call · voicemail left" in text
    assert "OVERDUE" in text
    assert ids == {"L1", "L2"}


def test_practice_transcript_alternates_and_starts_with_the_ring():
    T = crm.PracticeTurn
    msgs = crm.practice_messages([T(role="them", text="Murphy's."), T(role="me", text="Hi"),
                                  T(role="me", text="it's Stephan")])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[-1]["content"] == "Hi\nit's Stephan"


def test_coach_price_line_never_invents_a_price(monkeypatch):
    monkeypatch.setattr(crm, "COMPANY_PRICE", "")
    price = next(o for o in crm.coach_scripts()["objections"] if o["id"] == "price")
    assert "$" not in price["say"]


# ---------------------------------------------------------------- app store

SALES = ("Provider\tProduct Type Identifier\tUnits\tDeveloper Proceeds\tCountry Code\t"
         "Currency of Proceeds\tDevice\n"
         "APPLE\t1F\t5\t0\tUS\tUSD\tiPhone\n"
         "APPLE\t7F\t9\t0\tUS\tUSD\tiPhone\n"
         "APPLE\t3F\t2\t0\tCA\tCAD\tiPad\n"
         "APPLE\t1F\t1\t0\tCA\tCAD\tiPad\n")


def test_sales_report_splits_downloads_updates_redownloads():
    n = appstore.parse_sales_report(SALES)
    assert (n["downloads"], n["updates"], n["redownloads"]) == (6, 9, 2)
    assert n["countries"] == {"US": 5, "CA": 1}


def test_totals_compute_the_download_to_account_rate():
    t = appstore.totals({"d": appstore.parse_sales_report(SALES)}, {"2026-09-20": 3},
                        [{"rating": 5}, {"rating": 4}])
    assert t["signup_rate_pct"] == 50
    assert t["recent_rating"] == 4.5


def test_private_key_survives_every_way_it_gets_pasted():
    pem = "-----BEGIN PRIVATE KEY-----\nAAAA\nBBBB\n-----END PRIVATE KEY-----\n"
    for pasted in (pem, pem.replace("\n", "\\n"), base64.b64encode(pem.encode()).decode(),
                   " ".join(pem.split("\n"))):
        out = appstore.normalize_private_key(pasted)
        assert out.startswith("-----BEGIN PRIVATE KEY-----\n")
        assert out.rstrip().endswith("-----END PRIVATE KEY-----")
        assert "AAAA" in out and "BBBB" in out


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
