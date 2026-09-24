"""The School's two links to the real job, at the route level: rehearsing the
call to a real lead, and the game film of real calls. The prompts and the
referee are test_coach.py's job; these check what the routes read and return.
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

LEAD = {"id": "L1", "name": "Barrel House", "loc": "Denver, CO", "contact": "Laura",
        "last_outcome": "gatekeeper", "venue_facts": None, "cand_amenity": "bar",
        "notes": "[2026-09-20] call — Spoke to: Jake · Objection: no time"}


def _db(monkeypatch, one=None, many=()):
    seen = []

    class Cur:
        def execute(self, sql, params=None):
            seen.append((" ".join(sql.split()), params))

        def fetchone(self):
            return one

        def fetchall(self):
            return list(many)

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_brain_row", lambda: {"playbook": None})
    return seen


def test_rehearse_shows_what_is_real_before_the_phone_rings(monkeypatch):
    _db(monkeypatch, one=dict(LEAD))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    out = crm.coach_rehearse("L1", True)
    assert out["bar"] == "Barrel House" and out["name"] == "Laura" and out["ai"] is True
    assert out["opening"].startswith("Barrel House, Jake speaking")
    assert "Objection: no time" in out["real"]


def test_rehearsing_a_lead_that_is_gone_is_a_404(monkeypatch):
    _db(monkeypatch, one=None)
    with pytest.raises(crm.HTTPException) as e:
        crm.coach_rehearse("gone", True)
    assert e.value.status_code == 404


def test_a_rehearsal_turn_plays_the_real_bar(monkeypatch):
    _db(monkeypatch, one=dict(LEAD))
    asked = {}

    def fake(system, user, **kw):
        asked.update(system=system, user=user)
        return {"reply": "Laura's in the back, who's calling?", "trust_delta": 5}

    monkeypatch.setattr(crm, "_ask_claude", fake)
    out = crm.coach_turn(crm.TurnRequest(boss="lead", lead_id="L1", said="Hi, is Laura in?",
                                         patience=60, trust=10, challenge="storm"), True)
    assert "Barrel House" in asked["system"] and "REAL FACTS" in asked["system"]
    assert "TODAY'S TWIST" not in asked["system"]          # rotation rules don't apply to a real bar
    assert out["reply"].startswith("Laura's in the back") and out["trust"] == 15


def test_the_rehearsal_review_returns_the_cheat_sheet(monkeypatch):
    _db(monkeypatch, one=dict(LEAD))
    monkeypatch.setattr(crm, "_ask_claude", lambda s, u, **kw: {
        "opener": 7, "discovery": 6, "objections": 8, "ask": 5, "turning_point": "t", "redo": "r",
        "cheat_sheet": ["Is Laura in? It's about the Sunday count.", "Try it on your next count.", "x"],
        "avoid": "Pitching Jake."})
    out = crm.coach_review(crm.ReviewRequest(boss="lead", lead_id="L1",
                                             transcript=[{"role": "rep", "text": "hi"}],
                                             result="won"), True)
    assert out["cheat_sheet"] == ["Is Laura in? It's about the Sunday count.", "Try it on your next count."]
    assert out["avoid"] == "Pitching Jake." and "Objection: no time" in out["real"]


def test_an_ordinary_game_still_needs_a_known_owner(monkeypatch):
    with pytest.raises(crm.HTTPException) as e:
        crm.coach_turn(crm.TurnRequest(boss="nobody", said="hi", patience=50, trust=10), True)
    assert e.value.status_code == 422


def test_film_with_nothing_real_says_so_without_a_model_call(monkeypatch):
    _db(monkeypatch, many=[])
    monkeypatch.setattr(crm, "_ask_claude", lambda *a, **k: pytest.fail("no model call"))
    out = crm.coach_film(True)
    assert out["calls"] == 0 and "voicemails and missed calls don't count" in out["note"]


def test_film_reads_real_conversations_only(monkeypatch):
    rows = [{"lead_id": "L1", "at": "2026-09-23T20:00:00+00:00", "outcome": "not_interested",
             "name": "Barrel House", "notes": "[2026-09-23] call — Your notes: has a spreadsheet"}]
    seen = _db(monkeypatch, many=rows)
    asked = {}

    def fake(system, user, **kw):
        asked["user"] = user
        return {"working": None, "costing": {"text": "Pitching too early.", "from": "Barrel House"},
                "drills": [{"from": "Barrel House", "who": "Laura", "line": "We have a spreadsheet.",
                            "better": "Who updates it when a price changes?"}]}

    monkeypatch.setattr(crm, "_ask_claude", fake)
    out = crm.coach_film(True)
    sql = seen[0][0]
    assert "outcome IN ('answered', 'callback', 'not_interested', 'gatekeeper')" in sql
    assert "has a spreadsheet" in asked["user"]
    assert out["calls"] == 1 and out["drills"][0]["line"] == "We have a spreadsheet."


def test_the_new_school_routes_are_wired():
    paths = {r.path: r.endpoint for r in crm.crm_router.routes if hasattr(r, "endpoint")}
    assert paths["/v1/crm/coach/rehearse/{lead_id}"] is crm.coach_rehearse
    assert paths["/v1/crm/coach/film"] is crm.coach_film
