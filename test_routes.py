"""Every CRM route calls the function it was written for.

A helper inserted between `@crm_router.post("/leads/quick-add")` and
`quick_add_lead` became the route: FastAPI registered `_find_existing_lead`,
which takes a cursor, a name and a list of phones, so every "Add a lead" from
the page failed with a 422. The unit tests called `quick_add_lead()` directly
and never noticed. These check the routing table itself.
"""
import sys
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402


def _routes():
    return [r for r in crm.crm_router.routes if hasattr(r, "endpoint")]


def test_no_private_helper_is_a_route():
    wrong = [(r.path, r.endpoint.__name__) for r in _routes()
             if r.endpoint.__name__.startswith("_")]
    assert not wrong, wrong


def test_quick_add_is_routed_to_quick_add():
    route = next(r for r in _routes() if r.path.endswith("/leads/quick-add"))
    assert route.endpoint is crm.quick_add_lead
    assert [p.name for p in route.dependant.body_params] == ["data"]
    assert not route.dependant.query_params


def test_no_route_asks_for_a_database_cursor():
    # A cursor is never something a browser can send.
    bad = [r.path for r in _routes()
           if any(p.name == "cursor" for p in r.dependant.query_params)]
    assert not bad, bad
