"""The Follow-ups tab's Email button: a follow-up drafted from what was LOGGED.

`draft-email` with `followup: true` hands the model the lead's own notes
(call summaries, the operator's verbatim words) instead of a typed brief.
These check what it's handed, and that the bookkeeping instructions ride
along, without calling a model: `_ask_claude` is replaced by a recorder.
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

NOTES = ("[2026-09-20] call · attempt 1: Spoke with Laura, the GM. Interested, "
         "wants pricing. — Your notes: Laura just paid $800 at the vet for her cat\n"
         "Email found on: https://barrelhouse.example/contact")


def _lead(**kw):
    row = {k: None for k in crm.LEAD_COLUMNS}
    row.update(id="L1", name="The Barrel House", loc="Denver, CO", status="warm",
               contact="Laura", last_outcome="callback", call_date="2026-09-20",
               email_date=None, email="laura@barrelhouse.example", notes=NOTES)
    row.update(kw)
    return row


def test_followup_ask_carries_the_log_and_the_contact():
    ask = crm._followup_ask(_lead())
    assert "FOLLOW-UP" in ask
    assert "Contact: Laura" in ask
    assert "they asked for a callback (2026-09-20)" in ask
    assert "paid $800 at the vet" in ask         # the operator's own words
    assert "Ignore bookkeeping lines" in ask     # "Email found on" isn't content


def test_followup_ask_keeps_the_newest_part_of_a_long_log():
    old = "[2026-01-01] ancient history " * 400
    ask = crm._followup_ask(_lead(notes=old + "\n[2026-09-20] the latest call"))
    assert "the latest call" in ask
    assert len(ask) < crm.FOLLOWUP_NOTES_CHARS + 2000


def test_followup_ask_with_nothing_logged():
    ask = crm._followup_ask(_lead(notes=None, last_outcome=None, contact=None))
    assert "(nothing logged)" in ask
    assert "Contact:" not in ask


def test_followup_ask_appends_an_extra_instruction():
    assert crm._followup_ask(_lead(), "mention the free trial").endswith(
        "Also: mention the free trial")


class _Cursor:
    def __init__(self, row): self.row = row
    def execute(self, *a): pass
    def fetchone(self): return self.row


@pytest.fixture
def drafted(monkeypatch):
    sent = {}

    @contextmanager
    def db():
        conn = types.SimpleNamespace(cursor=lambda: _Cursor(_lead()))
        yield conn

    def fake_claude(system, ask, max_tokens=0):
        sent.update(system=system, ask=ask)
        return {"subject": "following up", "body": "Hi Laura, ..."}

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_ask_claude", fake_claude)
    return sent


def test_followup_draft_needs_no_brief(drafted):
    out = crm.draft_lead_email("L1", crm.DraftRequest(followup=True))
    assert out == {"subject": "following up", "body": "Hi Laura, ..."}
    assert "paid $800 at the vet" in drafted["ask"]
    assert "NEVER invent a fact" in drafted["system"]   # same facts-only rules


def test_a_redraft_edits_the_draft_on_screen(drafted):
    crm.draft_lead_email("L1", crm.DraftRequest(
        followup=True, brief="shorter", subject="following up", body="Hi Laura, ..."))
    assert drafted["ask"].startswith("Here is the current draft.")
    assert "shorter" in drafted["ask"]


def test_the_drafter_is_given_the_app_store_link(drafted):
    # It defaulted to empty, and "include the link to the app" got the website
    # only: the drafter may not use a link it wasn't handed.
    assert crm.COMPANY_APP_URL.startswith("https://apps.apple.com/")
    crm.draft_lead_email("L1", crm.DraftRequest(brief="include the app store link"))
    assert f"App Store listing: {crm.COMPANY_APP_URL}" in drafted["system"]


def test_a_plain_draft_still_needs_a_brief(drafted):
    with pytest.raises(HTTPException) as e:
        crm.draft_lead_email("L1", crm.DraftRequest(brief="   "))
    assert e.value.status_code == 422


def test_the_drafter_knows_the_sender_owns_86d_and_made_the_calls(drafted):
    # A draft opened "Lesley told me about 86'd" — Lesley was the bartender the
    # owner spoke to, who pointed him at Brent. The model hadn't been told the
    # log is the sender's, or that the sender owns the product.
    crm.draft_lead_email("L1", crm.DraftRequest(followup=True))
    assert "who owns 86'd" in drafted["system"]
    assert "Whose log this is: the SENDER's" in drafted["ask"]
    assert "never say or imply they did" in drafted["ask"]
