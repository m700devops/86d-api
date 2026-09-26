"""Barcode lookups: one printed code, however the phone reads it, finds its product
— including after the product it was registered on was merged into another.

`helpers.barcode_variants` is pure. `_find_by_barcode` is checked with its SQL
faked per query; the merge and registration paths were run against a real
Postgres (see CLAUDE.md). No network.
"""
import contextlib
import os
import sys

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import main  # noqa: E402
from helpers import _upce_to_upca, barcode_variants  # noqa: E402


def finds(stored, scanned):
    """What the database lookup does: the stored string must be one of the
    scanned code's forms. One-sided on purpose — a test that intersected both
    sides once hid a UPC-E that could only be found from one direction."""
    return stored in barcode_variants(scanned)


def same(a, b):
    return finds(a, b) and finds(b, a)


@pytest.mark.parametrize("a, b", [
    ("012345678905", "0012345678905"),        # UPC-A, and iOS's EAN-13 read of it
    ("012345678905", "00012345678905"),       # GTIN-14 padding
    ("04963406", "049000006346"),             # a Coca-Cola can's UPC-E and its UPC-A
    ("04963406", "0049000006346"),            # ...and iOS's EAN-13 read of that UPC-A
    ("5000267024004", "05000267024004"),      # a real EAN-13 (Johnnie Walker) padded
])
def test_one_code_in_its_many_forms(a, b):
    assert same(a, b)          # found whichever way round it was stored and read


def test_a_code_typed_with_its_spaces():
    from helpers import clean_barcode
    assert clean_barcode(" 0 12345-67890 5 ") == "012345678905"     # stored as digits
    assert finds("012345678905", "0 12345 67890 5")                    # and found typed
    assert clean_barcode("ABC-123") == "ABC-123" and clean_barcode("  ") is None


@pytest.mark.parametrize("a, b", [
    ("012345678905", "012345678912"),         # different products
    ("5000267024004", "5000267024011"),
    ("ABC-123", "123"),                       # a Code 128 tag is only ever itself
])
def test_different_codes_stay_different(a, b):
    assert not finds(a, b) and not finds(b, a)


@pytest.mark.parametrize("upce, upca", [
    ("04963406", "049000006346"),
    ("06543217", "065100004327"),     # last digit 5-9
    ("01234531", "012300000451"),     # last digit 3
    ("01234542", "012340000052"),     # last digit 4
    ("01234507", "012000003457"),     # last digit 0-2
])
def test_upce_expansion(upce, upca):
    assert _upce_to_upca(upce) == upca


def test_not_barcodes():
    assert barcode_variants("") == [] and barcode_variants(None) == []
    assert barcode_variants("ABC-123") == ["ABC-123"]
    assert barcode_variants("12345") == ["12345"]            # too short to be a GTIN
    assert _upce_to_upca("24963406") is None                  # UPC-E starts with 0 or 1


# ─── the lookup ──────────────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, live=(), merged=()):
        self.live, self.merged, self.queries, self.rows = list(live), list(merged), [], []

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.queries.append((sql, params))
        wanted = set(params[0])
        if "deleted_at IS NULL ORDER BY" in sql:
            self.rows = [r for r in self.live if r["upc"] in wanted]
        elif "JOIN product_aliases" in sql:
            self.rows = [r["now"] for r in self.merged if r["upc"] in wanted]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_a_can_registered_by_its_upce_is_found_by_its_upca():
    cur = FakeCursor(live=[{"id": "coke", "upc": "04963406"}])
    assert main._find_by_barcode(cur, "049000006346")["id"] == "coke"
    assert main._find_by_barcode(cur, "0049000006346")["id"] == "coke"


def test_every_upce_round_trips():
    import random
    from helpers import _upca_to_upces
    rng = random.Random(1)
    for _ in range(5000):
        upce = rng.choice("01") + "".join(rng.choice("0123456789") for _ in range(7))
        assert upce in _upca_to_upces(_upce_to_upca(upce))


def test_only_real_compressions_are_offered():
    """The other direction: a UPC-A with no UPC-E form has none, and every one
    offered writes back out as the same code — an unchecked guess would make
    012345678905 answer to four unrelated 8-digit codes."""
    import random
    from helpers import _upca_to_upces
    assert _upca_to_upces("012345678905") == set()
    rng = random.Random(2)
    for _ in range(5000):
        upca = rng.choice("01") + "".join(rng.choice("0123456789") for _ in range(11))
        assert all(_upce_to_upca(e) == upca for e in _upca_to_upces(upca))


def test_found_in_whatever_form_it_was_stored():
    cur = FakeCursor(live=[{"id": "p1", "upc": "012345678905"}])
    assert main._find_by_barcode(cur, "0012345678905")["id"] == "p1"     # scanned on an iPhone
    assert len(cur.queries) == 1                                         # no second query needed


def test_a_merged_away_products_code_finds_the_product_it_became():
    cur = FakeCursor(merged=[{"upc": "0012345678905", "now": {"id": "keeper"}}])
    assert main._find_by_barcode(cur, "012345678905")["id"] == "keeper"


def test_a_live_product_wins_over_a_merged_one():
    cur = FakeCursor(live=[{"id": "live", "upc": "012345678905"}],
                     merged=[{"upc": "012345678905", "now": {"id": "keeper"}}])
    assert main._find_by_barcode(cur, "012345678905")["id"] == "live"


def test_nothing_known_is_none():
    assert main._find_by_barcode(FakeCursor(), "012345678905") is None
    assert main._find_by_barcode(FakeCursor(), "") is None


def test_the_route_answers_from_the_lookup(monkeypatch):
    from fastapi.testclient import TestClient
    cur = FakeCursor(live=[{"id": "p1", "upc": "049000006346", "verified": 1, "name": "Coca-Cola"}])

    class Conn:
        def cursor(self):
            return cur
    monkeypatch.setattr(main, "get_db", lambda: contextlib.nullcontext(Conn()))
    client = TestClient(main.app)
    r = client.get("/v1/products/barcode/04963406")                  # the can's UPC-E
    assert r.status_code == 200 and r.json()["product"]["id"] == "p1"
    assert r.json()["product"]["verified"] is True
    miss = client.get("/v1/products/barcode/012345678912")
    assert miss.status_code == 404
    # Flat for the CRM page and RegisterScreen, and under "detail", where most of
    # the app looks (the 409's existing_product, invalid_password, ...).
    assert miss.json()["error"] == miss.json()["detail"]["error"] == "product_not_found"


# ─── registering and merging ─────────────────────────────────────────────────

class ScriptCursor:
    """Answers each query from the first rule whose words it contains, and keeps
    every statement so a test can see what was written."""
    def __init__(self, rules):
        self.rules, self.sql, self.rows = rules, [], []

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.sql.append((sql, params))
        self.rows = next((rows for words, rows in self.rules if words in sql), [])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


def _client(monkeypatch, cur):
    from fastapi.testclient import TestClient

    class Conn:
        def cursor(self):
            return cur

        def commit(self):
            pass
    monkeypatch.setattr(main, "get_db", lambda: contextlib.nullcontext(Conn()))
    main.app.dependency_overrides[main.get_current_user] = lambda: "u1"
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def _no_overrides():
    yield
    main.app.dependency_overrides.pop(main.get_current_user, None)


def test_registering_a_known_code_names_the_live_product(monkeypatch):
    """The code is still held by a merged-away duplicate (the column is UNIQUE,
    deleted rows included): the 409 names the product it became, under
    "detail" where the app looks, so the bottle is counted against it."""
    cur = ScriptCursor([
        ("WHERE upc = ANY(%s) AND deleted_at IS NULL", []),
        ("JOIN product_aliases", [{"id": "keeper", "name": "Red Label"}]),
        ("WHERE upc = ANY(%s)", [{"id": "old-dup", "deleted_at": "2026-09-01"}]),
    ])
    r = _client(monkeypatch, cur).post("/v1/products", json={
        "name": "JW Red", "category": "spirits", "upc": "0 12345 67890 5"})
    assert r.status_code == 409
    assert r.json()["detail"]["existing_product"]["id"] == "keeper"
    assert cur.sql[0][1] == (barcode_variants("012345678905"),)          # typed spaces ignored


def test_a_new_code_is_stored_as_digits(monkeypatch):
    cur = ScriptCursor([])
    r = _client(monkeypatch, cur).post("/v1/products", json={
        "name": "House Gin", "category": "spirits", "upc": "0 12345 67890 5"})
    assert r.status_code == 201 and r.json()["product"]["upc"] == "012345678905"
    insert = next(p for s, p in cur.sql if s.startswith("INSERT INTO products"))
    assert "012345678905" in insert


def _merge(monkeypatch, target_upc):
    cur = ScriptCursor([
        ("SELECT id, name, brand, verified, created_by_user_id, upc", [
            {"id": "dup", "name": "Red", "brand": "Johnnie Walker", "verified": 0,
             "created_by_user_id": "u1", "upc": "012345678905"}]),
        ("SELECT id, upc FROM products", [{"id": "keeper", "upc": target_upc}]),
    ])
    r = _client(monkeypatch, cur).post("/v1/products/dup/merge",
                                       json={"target_product_id": "keeper"})
    assert r.status_code == 200
    return r.json(), [s for s, _ in cur.sql if s.startswith("UPDATE products")], cur


def test_a_merge_takes_the_barcode_to_the_keeper(monkeypatch):
    out, updates, cur = _merge(monkeypatch, None)
    assert out["barcode_moved"] is True
    # cleared off the duplicate BEFORE the keeper takes it — the column is UNIQUE
    assert updates[0] == "UPDATE products SET upc = NULL WHERE id = %s"
    assert updates[1].startswith("UPDATE products SET upc = %s")
    assert ("012345678905", ) == next(p for s, p in cur.sql if s == updates[1])[:1]


def test_a_keeper_with_its_own_code_keeps_it(monkeypatch):
    out, updates, _ = _merge(monkeypatch, "5000267024004")
    assert out["barcode_moved"] is False
    assert not any("SET upc" in s for s in updates)       # the alias resolves the old code
