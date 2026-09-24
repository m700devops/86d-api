"""The email drafter's master sheet, and every CRM AI on one strong model.

The drafts were bland: the rules capped them at four sentences with no list,
the model knew no price, trial or phone number, and it ran on Haiku. pitch.py
now carries the owner's facts, his own example email and a style guide, and
every AI in the CRM runs on CRM_AI_MODEL (Opus 5) at CRM_AI_EFFORT (medium).
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
import pitch  # noqa: E402


# ── the master sheet ────────────────────────────────────────────────────────

def test_the_sheet_carries_the_owners_facts():
    sheet = pitch.master_sheet()
    for fact in ("Stephan", "910-335-2760", "$29.99/month", "First month free",
                 "No credit card", pitch.APP_URL, "iOS only", "restaurant's name",
                 "bar manager's name"):
        assert fact in sheet, fact
    assert pitch.APP_URL == "https://apps.apple.com/us/app/86d-bar-inventory/id6798359825"


def test_the_sheet_never_claims_an_order_number():
    # The owner's sample said "a unique order number"; the distributor email
    # has none ("Order from {bar} — {date}"). A bar that checks stops trusting.
    assert "order number" not in pitch.master_sheet().lower()
    assert "order number" not in pitch.EXAMPLE_EMAIL.lower()


def test_the_prompt_has_the_sheet_the_example_and_the_rules():
    p = pitch.system_prompt(pitch.lead_context({"name": "Rioja", "loc": "Denver, CO"}))
    assert "MASTER SHEET" in p and "Cut bar inventory to 15 minutes" in p
    assert "NEVER state a product fact" in p and "who owns 86'd" in p
    assert "Open with THEM" in p                        # the human part
    assert "App Store link in every email" in p


def test_what_we_know_is_only_whats_on_file():
    ctx = pitch.lead_context(
        {"name": "Workhorse Bar", "loc": "Austin, TX", "contact": "Lesley",
         "opener": "a big tap list"},
        [{"text": "open since 2014", "source": "their website"}],
        ["busy Sundays"], "[2026-09-25] call: spoke with Lesley")
    assert "Lesley (the person we spoke to)" in ctx
    assert "open since 2014 (from their website)" in ctx
    assert "busy Sundays" in ctx and "spoke with Lesley" in ctx
    thin = pitch.lead_context({"name": "Nowhere Bar"})
    assert "Nothing else is known" in thin


# ── the drafter uses it, on the strong model ────────────────────────────────

def _lead(**kw):
    row = {k: None for k in crm.LEAD_COLUMNS}
    row.update(id="L1", name="Rioja", loc="Denver, CO", status="new",
               notes="[2026-09-24] call · attempt 1: spoke with Alex, the GM",
               venue_facts=None, call_brief='["tapas and a long wine list"]')
    row.update(kw)
    return row


@pytest.fixture
def drafted(monkeypatch):
    sent = {}

    class _Cur:
        def execute(self, *a): pass
        def fetchone(self): return _lead()

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: _Cur())

    def fake(system, ask, schema, model=None, max_tokens=0, timeout=0):
        sent.update(system=system, ask=ask, schema=schema, max_tokens=max_tokens)
        return {"subject": "Rioja's Sunday count", "body": "Hi Alex, ..."}

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_claude_json", fake)
    return sent


def test_a_first_email_reads_the_log_and_the_prep_sheet(drafted):
    out = crm.draft_lead_email("L1", crm.DraftRequest(brief="first email, mention the free month"))
    assert out["subject"] == "Rioja's Sunday count"
    assert "spoke with Alex" in drafted["system"]          # the log, for a first email too
    assert "tapas and a long wine list" in drafted["system"]
    assert drafted["schema"] == pitch.SCHEMA
    assert drafted["max_tokens"] >= crm.AI_MIN_TOKENS


def test_a_follow_up_carries_the_log_once(drafted):
    crm.draft_lead_email("L1", crm.DraftRequest(followup=True))
    assert "spoke with Alex" in drafted["ask"]
    assert "spoke with Alex" not in drafted["system"]


# ── every AI on one model and effort ────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def _record(monkeypatch, *responses):
    import httpx
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"json": json, "timeout": timeout})
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_the_default_is_opus_5_at_medium():
    assert crm.AI_MODEL == "claude-opus-5" and crm.AI_EFFORT == "medium"
    assert crm.ASSIST_MODEL == crm.AI_MODEL == crm.DEBRIEF_MODEL


def test_the_old_helper_speaks_to_current_models(monkeypatch):
    calls = _record(monkeypatch, _Resp(200, {"stop_reason": "end_turn", "content": [
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": 'Sure: {"points": ["a"]}'}]}))
    assert crm._ask_claude("sys", "msg", max_tokens=350, temperature=1) == {"points": ["a"]}
    body = calls[0]["json"]
    assert body["model"] == "claude-opus-5"
    assert body["output_config"] == {"effort": "medium"}
    assert "temperature" not in body                                # rejected by Opus 5
    assert [m["role"] for m in body["messages"]] == ["user"]         # and so is a prefill
    assert body["max_tokens"] >= crm.AI_MIN_TOKENS                   # room to think
    assert calls[0]["timeout"] >= crm.AI_MIN_TIMEOUT


def test_a_400_on_effort_is_retried_without_it(monkeypatch):
    calls = _record(monkeypatch, _Resp(400, {"error": "effort"}),
                    _Resp(200, {"stop_reason": "end_turn", "content": [
                        {"type": "text", "text": '{"ok": true}'}]}))
    assert crm._ask_claude("sys", "msg") == {"ok": True}
    assert "output_config" not in calls[1]["json"]


def test_the_structured_helper_sends_effort_too(monkeypatch):
    calls = _record(monkeypatch, _Resp(200, {"stop_reason": "end_turn", "content": [
        {"type": "text", "text": '{"subject": "s", "body": "b"}'}]}))
    crm._claude_json("sys", "msg", pitch.SCHEMA)
    oc = calls[0]["json"]["output_config"]
    assert oc["effort"] == "medium" and oc["format"]["schema"] == pitch.SCHEMA
