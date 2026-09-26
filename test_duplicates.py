"""The Bottle Book's duplicate finder: one bar's products that are the same
bottle twice, and which copy to keep (helpers.duplicate_groups), plus the
route that serves it. Pure, except the route, which runs on a faked cursor.
"""
import contextlib
import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import pytest  # noqa: E402

import main  # noqa: E402
from helpers import duplicate_groups, product_match_key  # noqa: E402


def P(pid, name, brand, size=None, verified=False, price=None, par=None,
      dist=False, scans=0, created="2026-01-01"):
    return {"id": pid, "name": name, "brand": brand, "size": size,
            "match_key": product_match_key(name, brand), "verified": 1 if verified else 0,
            "price": price, "par_quantity": par, "has_distributor": dist,
            "scan_count": scans, "created_at": created}


def pairs(rows):
    return {(g["keep"]["id"], tuple(r["id"] for r in g["fold"])) for g in duplicate_groups(rows)}


def test_the_seed_and_a_scan_minted_copy():
    rows = [P("scan", "Original", "Grey Goose", price=32, par=3),
            P("seed", "Grey Goose Original 750ml", "Grey Goose", "750ml", verified=True)]
    assert pairs(rows) == {("seed", ("scan",))}      # the catalog's own is kept


def test_one_brand_spelled_two_ways():
    # the key reads brands run together; word by word, "J&B" and "JB" differ
    assert pairs([P("a", "Rare", "J&B"), P("b", "Rare", "JB", price=25)]) == {("b", ("a",))}


def test_two_readings_of_one_label():
    rows = [P("a", "Red", "Johnnie Walker"), P("b", "Red Label", "Johnnie Walker", price=30)]
    assert pairs(rows) == {("b", ("a",))}            # the one the bar priced is kept


@pytest.mark.parametrize("a, b", [
    (("Bulleit", "Bulleit"), ("Rye", "Bulleit")),
    (("Red Label", "Johnnie Walker"), ("Black Label", "Johnnie Walker")),
    (("Light", "Bud"), ("Light Lime", "Bud")),
    (("12", "Glenlivet"), ("15", "Glenlivet")),
    (("Original", "Absolut"), ("Citron", "Absolut")),
])
def test_different_bottles_are_never_suggested(a, b):
    assert pairs([P("a", *a), P("b", *b)]) == set()


def test_two_sizes_are_two_bottles():
    rows = [P("s", "Handmade 750ml", "Tito's", "750ml"), P("l", "Handmade 1L", "Tito's", "1L")]
    assert pairs(rows) == set()


def test_a_sizeless_copy_beside_two_sizes_is_left_alone():
    rows = [P("s", "Handmade", "Tito's", "750ml", verified=True),
            P("l", "Handmade 1L", "Tito's", "1L", price=40),
            P("x", "Handmade", "Tito's")]                # the 750ml or the litre?
    assert pairs(rows) == set()


def test_a_sizeless_keeper_with_copies_of_two_sizes_is_withheld():
    rows = [P("k", "Handmade", "Tito's", price=30, par=2),
            P("s", "Handmade 750ml", "Tito's"), P("l", "Handmade 1L", "Tito's")]
    assert pairs(rows) == set()


def test_what_the_bar_set_decides_the_keeper_then_scans_then_age():
    rows = [P("old", "Red", "Johnnie Walker", scans=50, created="2025-01-01"),
            P("set", "Red Label", "Johnnie Walker", par=2, dist=True)]
    assert pairs(rows) == {("set", ("old",))}
    rows = [P("few", "Red", "Johnnie Walker", scans=2), P("many", "Red Label", "Johnnie Walker", scans=9)]
    assert pairs(rows) == {("many", ("few",))}
    rows = [P("a-new", "Red", "Johnnie Walker", created="2026-05-01"),       # ids sort the
            P("z-first", "Red Label", "Johnnie Walker", created="2026-02-01")]  # other way
    assert pairs(rows) == {("z-first", ("a-new",))}


def test_a_verified_product_is_never_folded():
    rows = [P("v1", "Red", "Johnnie Walker", verified=True, price=30),
            P("v2", "Red Label", "Johnnie Walker", verified=True)]
    assert pairs(rows) == set()                      # the merge route would refuse it


def test_three_copies_fold_into_one():
    rows = [P("seed", "Johnnie Walker Red Label 750ml", "Johnnie Walker", "750ml", verified=True),
            P("a", "Red", "Johnnie Walker"), P("b", "Red Label", "Johnnie Walker", price=30)]
    assert pairs(rows) == {("seed", ("b", "a"))}


# ─── the route ───────────────────────────────────────────────────────────────

class Cur:
    def __init__(self, owned, rows):
        self.owned, self.book, self.rows = owned, rows, []

    def execute(self, sql, params=()):
        if "FROM locations" in sql:
            self.rows = [{"id": params[0]}] if self.owned else []
        elif "FROM par_levels pl" in sql:
            self.rows = self.book
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    def use(owned, rows):
        cur = Cur(owned, rows)

        class Conn:
            def cursor(self):
                return cur
        monkeypatch.setattr(main, "get_db", lambda: contextlib.nullcontext(Conn()))
        return TestClient(main.app)
    main.app.dependency_overrides[main.get_current_user] = lambda: "u1"
    yield use
    main.app.dependency_overrides.pop(main.get_current_user, None)


def test_the_route_serves_what_to_merge_into_what(client):
    rows = [P("scan", "Original", "Grey Goose"),
            P("seed", "Grey Goose Original 750ml", "Grey Goose", "750ml", verified=True)]
    r = client(True, rows).get("/v1/locations/L1/duplicates")
    assert r.status_code == 200
    (group,) = r.json()["groups"]
    assert group["keep"] == {"product_id": "seed", "name": "Grey Goose Original 750ml",
                             "brand": "Grey Goose", "size": "750ml", "verified": True}
    assert [f["product_id"] for f in group["fold"]] == ["scan"]


def test_someone_elses_bar_is_refused(client):
    r = client(False, []).get("/v1/locations/L1/duplicates")
    assert r.status_code == 403 and r.json()["detail"]["error"] == "forbidden"


# ─── fast, and exactly what comparing everything with everything gives ───────

def _brute(rows):
    """The finder without its index: every row against every group."""
    from helpers import _keeper_rank, _row_ml, same_bottle, sizes_compatible
    groups = []
    for row in sorted(rows, key=_keeper_rank):
        homes = [] if row.get("verified") else [g for g in groups if same_bottle(g["keep"], row)]
        if len(homes) == 1:
            if all(sizes_compatible(_row_ml(m), _row_ml(row)) for m in homes[0]["fold"]):
                homes[0]["fold"].append(row)
            else:
                homes[0]["contested"] = True
        elif not homes:
            groups.append({"keep": row, "fold": [], "contested": False})
    return {(g["keep"]["id"], tuple(r["id"] for r in g["fold"])) for g in groups
            if g["fold"] and not g["contested"]}


def _seeded_book():
    from seed_data import SEED_PRODUCTS
    from helpers import seed_display_name
    rows = [P(f"s{i}", p["name"], p["brand"], p.get("size"), verified=True) for i, p in enumerate(SEED_PRODUCTS)]
    rows += [P(f"c{i}", seed_display_name(p["name"], p["brand"]), p["brand"], price=10, scans=1)
             for i, p in enumerate(SEED_PRODUCTS)]
    return rows


def test_the_index_changes_nothing_on_the_whole_catalog():
    rows = _seeded_book()
    assert pairs(rows) == _brute(rows) and len(pairs(rows)) > 400


def test_the_index_changes_nothing_on_messy_books():
    import random
    rng = random.Random(7)
    brands = ["Johnnie Walker", "Jack Daniel's", "Jack Daniels", "Tito's", "Titos", "Smirnoff", "Smirnof",
              "Grey Goose", "Patrón", "Patron", "J&B", "JB", "Bulleit", "", None]
    names = ["Red", "Red Label", "Black Label", "Original", "Handmade", "Old No. 7", "Old No 7 Tennessee Whiskey",
             "Silver", "Sliver", "Rye", "Bourbon", "12", "12 Year Old", "Light", "Light Lime", "Citron", "Citroen",
             "Handmade 750ml", "Handmade 1L", "Red Label 1.75L", "Jack Daniel's Old No. 7"]
    for trial in range(60):
        rows = [P(f"r{trial}-{i}", rng.choice(names), rng.choice(brands),
                  rng.choice([None, None, "750ml", "1L"]), verified=rng.random() < 0.15,
                  price=rng.choice([None, 20]), par=rng.choice([None, 2]), scans=rng.randint(0, 5),
                  created=f"2026-0{rng.randint(1, 9)}-01") for i in range(rng.randint(2, 40))]
        assert pairs(rows) == _brute(rows), trial


@pytest.mark.parametrize("keeper_is_short", [True, False])
def test_found_whichever_reading_starts_with_the_shared_word(keeper_is_short):
    # "The Glenlivet 12" starts with a word the other reading doesn't have, so
    # only one of the two index lookups can find the pair: each direction once.
    short = P("short", "12", "Glenlivet", price=30 if keeper_is_short else None)
    long = P("long", "The Glenlivet 12", None, price=None if keeper_is_short else 30)
    keep, fold = ("short", "long") if keeper_is_short else ("long", "short")
    assert pairs([short, long]) == {(keep, (fold,))}


def test_a_big_book_is_quick():
    import time
    rows = _seeded_book()                     # 914 products: every seed, and a copy of each
    started = time.perf_counter()
    duplicate_groups(rows)
    assert time.perf_counter() - started < 1.0
