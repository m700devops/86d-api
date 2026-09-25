"""The School's game film of real calls, at the route level: what the route
reads and returns. The prompt and the validator are test_coach.py's job.
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


def test_a_game_needs_a_known_owner(monkeypatch):
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


def test_the_film_route_is_wired_and_rehearsal_is_gone():
    paths = {r.path: r.endpoint for r in crm.crm_router.routes if hasattr(r, "endpoint")}
    assert paths["/v1/crm/coach/film"] is crm.coach_film
    # "Rehearse a real call" was removed at the owner's request.
    assert not any("rehearse" in p for p in paths)
    assert "lead_id" not in crm.TurnRequest.model_fields
    assert "lead_id" not in crm.ReviewRequest.model_fields
