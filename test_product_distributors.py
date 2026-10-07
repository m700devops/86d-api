"""A bottle's distributor, set once, comes back on every later count.

`GET /locations/{id}/product-distributors` is how the app reads the saved
assignments back. Its response model used to nest DistributorResponse /
ProductResponse, which require fields the route never selects (user_id,
category, timestamps): with ZERO assignments it answered 200, and from the first
one saved it answered 500 — so the app read nothing back and asked the bartender
for the same distributor on every count. The route is run through FastAPI's real
response validation here, with the database faked. The scan-side half (a bottle
with only a distributor saved is still "this bar's bottle") was run on a real
Postgres 16; see CLAUDE.md.
"""
import contextlib
import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

ROW = {"id": "a1", "location_id": "L1", "product_id": "p1", "distributor_id": "d1",
       "created_at": "2026-10-01T00:00:00+00:00", "distributor_name": "Southern",
       "distributor_email": None, "product_name": "Blue Bolt", "product_brand": "Gatorade",
       "product_product_type": None, "product_size": None}


def _client(monkeypatch, rows):
    class Cur:
        def execute(self, sql, params=()):
            self.sql = sql

        def fetchone(self):
            return {"id": "L1"}

        def fetchall(self):
            return rows

    class Conn:
        def cursor(self):
            return Cur()

    @contextlib.contextmanager
    def fake_db():
        yield Conn()

    monkeypatch.setattr(main, "get_db", fake_db)
    main.app.dependency_overrides[main.get_current_user] = lambda: "u1"
    return TestClient(main.app, raise_server_exceptions=False)


def test_saved_assignments_read_back(monkeypatch):
    try:
        r = _client(monkeypatch, [ROW]).get("/v1/locations/L1/product-distributors")
    finally:
        main.app.dependency_overrides.pop(main.get_current_user, None)
    assert r.status_code == 200
    (a,) = r.json()["assignments"]
    # Exactly what the app's ProductBookContext reads.
    assert a["product_id"] == "p1" and a["distributor_id"] == "d1"
    assert a["distributor"] == {"id": "d1", "name": "Southern", "email": None}
    assert a["product"]["name"] == "Blue Bolt" and a["product"]["brand"] == "Gatorade"


def test_no_assignments(monkeypatch):
    try:
        r = _client(monkeypatch, []).get("/v1/locations/L1/product-distributors")
    finally:
        main.app.dependency_overrides.pop(main.get_current_user, None)
    assert r.status_code == 200 and r.json() == {"assignments": []}


def test_bar_book_includes_bottles_with_only_a_distributor():
    """Step 0 of the matcher must see a bottle the bar gave a distributor and
    nothing else, or a scan can land on another copy and lose it."""
    import inspect
    src = inspect.getsource(main._find_product)
    step0 = src[src.index("Step 0"):src.index("Step A")]
    assert "location_product_distributors" in step0
