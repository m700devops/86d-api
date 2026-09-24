"""The prep sheet: an opener, who to ask for, talking points and what to watch
for — from the bar's facts, its own history and what we've learned.

It used to be written from website facts only (so a bar with none got no
points at all, and a callback got nothing from the call before), and cached
forever (so it never learned what the last call said). Now it's cached
against a fingerprint of everything it was written from, and `quick=1`
returns what's on file without waiting on a model.
"""
import json
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402


def _row(**kw):
    row = {"id": "L1", "name": "Workhorse Bar", "loc": "Austin, TX", "contact": "Brent (owner)",
           "last_outcome": "gatekeeper", "manager_name": None, "manager_role": None,
           "opener": None, "venue_facts": None, "call_brief": None, "cand_website": None,
           "cand_amenity": "bar", "opening_hours": None,
           "notes": "[2026-09-24] call · attempt 1: Lesley says Brent owns it · Spoke to: Lesley"}
    row.update(kw)
    return row


def _wire(monkeypatch, row, model_out=None):
    state = {"row": dict(row), "calls": 0, "writes": []}

    class Cur:
        def execute(self, sql, params=()):
            if sql.lstrip().startswith("UPDATE crm_leads SET call_brief"):
                state["writes"].append(params)
                state["row"]["call_brief"] = params[0]

        def fetchone(self):
            return dict(state["row"])

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    def model(system, user, schema, **kw):
        state["calls"] += 1
        state["system"], state["user"] = system, user
        if isinstance(model_out, Exception):
            raise model_out
        return model_out or {"opener": "Hey, is Brent around? Lesley said he handles orders.",
                             "ask_for": "Brent, the owner, per Lesley",
                             "points": ["Brent does the ordering himself."],
                             "watch_for": "\"I do it on a clipboard, it's fine\" — ask how long it takes."}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_claude_json", model)
    monkeypatch.setattr(crm, "_knowledge", lambda *a, **k: "")
    monkeypatch.setattr(crm, "_venue_profile", lambda row: {"kind": "bar"})
    return state


def test_quick_never_waits_on_the_model(monkeypatch):
    st = _wire(monkeypatch, _row())
    d = crm.lead_brief("L1", quick=True, _=True)
    assert d["pending"] is True and st["calls"] == 0 and d["points"] == []


def test_the_brief_uses_the_history_and_is_kept(monkeypatch):
    st = _wire(monkeypatch, _row())
    d = crm.lead_brief("L1", _=True)
    assert d["opener"].startswith("Hey, is Brent around")
    assert d["ask_for"] == "Brent, the owner, per Lesley"
    assert d["watch_for"] and d["points"] == ["Brent does the ordering himself."]
    assert "Lesley says Brent owns it" in st["user"]              # the call before
    assert "MASTER SHEET" in st["system"] and "$29.99" in st["system"]
    stored = json.loads(st["writes"][0][0])
    assert stored["v"] == crm.BRIEF_VERSION and stored["fp"]
    # Same inputs: served from the cache, no second model call.
    again = crm.lead_brief("L1", _=True)
    assert again["cached"] and st["calls"] == 1
    # A logged call changes the inputs, so the next open rewrites it.
    st["row"]["notes"] += "\n[2026-09-26] call · attempt 2: Brent said call back Tuesday"
    crm.lead_brief("L1", _=True)
    assert st["calls"] == 2


def test_an_old_list_brief_is_treated_as_stale(monkeypatch):
    st = _wire(monkeypatch, _row(call_brief='["an old point"]'))
    assert crm._brief_of(_row(call_brief='["an old point"]')) == {"points": ["an old point"]}
    crm.lead_brief("L1", _=True)
    assert st["calls"] == 1


def test_a_name_the_record_doesnt_carry_is_dropped(monkeypatch):
    _wire(monkeypatch, _row(), model_out={"opener": None, "ask_for": "Maria, the GM",
                                          "points": [], "watch_for": None})
    assert crm.lead_brief("L1", _=True)["ask_for"] is None


def test_a_model_outage_still_shows_the_facts(monkeypatch):
    st = _wire(monkeypatch, _row(), model_out=crm.HTTPException(status_code=503, detail={}))
    d = crm.lead_brief("L1", _=True)
    assert d["points"] == [] and d["opener"] is None and st["writes"] == []
