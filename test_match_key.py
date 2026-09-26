"""helpers.product_match_key — the key that decides whether a scan's answer is a
product already in the catalog — and the prompt's product list generated from the
same seed catalog. Pure: no database, no network.

Each case here is a way a correct read used to become a second product: an
accent written one time and not the next, the brand repeated in the name, a size
on the stored name, the seeded catalog's "Grey Goose Original 750ml" never meeting
a scan's "Original" / "Grey Goose".
"""
import os
import sys
from collections import defaultdict

import pytest

from helpers import product_match_key as key, seed_display_name
from seed_data import SEED_PRODUCTS


@pytest.mark.parametrize("a, b", [
    # accents: the prompt's own spelling list is accented, the seed catalog isn't
    (("Silver", "Patrón"), ("Silver", "Patron")),
    (("Original", "Jägermeister"), ("Original", "Jagermeister")),
    (("Añejo", "Don Julio"), ("Anejo", "Don Julio")),
    (("VSOP", "Rémy Martin"), ("VSOP", "Remy Martin")),
    # the brand repeated in the name
    (("Jack Daniel's Old No. 7", "Jack Daniel's"), ("Old No. 7", "Jack Daniel's")),
    (("Titos Handmade", "Tito's"), ("Handmade", "Tito's")),
    (("Rare", "J&B"), ("JB Rare", "JB")),
    # sizes and pack counts
    (("Tito's Handmade 750ml", "Tito's"), ("Handmade", "Tito's")),
    (("Handmade 1.75L", "Tito's"), ("Handmade", "Tito's")),
    (("Blue Bolt 28 fl oz", "Gatorade"), ("Blue Bolt", "Gatorade")),
    (("Light 12-pack", "Coors"), ("Light", "Coors")),
    # the seeded beers carry the brand twice
    (("Coors Coors Light 12oz", "Coors"), ("Light", "Coors")),
    (("Budweiser Budweiser 12oz", "Budweiser"), ("Original", "Budweiser")),
    # a name that is only the brand is the base product, "Original"
    (("Grey Goose", "Grey Goose"), ("Original", "Grey Goose")),
    (("Grey Goose Original 750ml", "Grey Goose"), ("Original", "Grey Goose")),
    # punctuation and case, as before
    (("OLD NO 7", "JACK DANIELS"), ("Old No. 7", "Jack Daniel's")),
])
def test_same_bottle_same_key(a, b):
    assert key(*a) == key(*b)


@pytest.mark.parametrize("a, b", [
    # class words are NOT dropped: these are different bottles
    (("Bourbon", "Bulleit"), ("Rye", "Bulleit")),
    # variants are never loosened
    (("Citron", "Absolut"), ("Mandrin", "Absolut")),
    (("Red Label", "Johnnie Walker"), ("Black Label", "Johnnie Walker")),
    (("Reposado", "Patrón"), ("Añejo", "Patrón")),
    (("12", "Glenfiddich"), ("15", "Glenfiddich")),
    # numbers that aren't sizes stay
    (("Old No. 7", "Jack Daniel's"), ("Old No. 8", "Jack Daniel's")),
    (("Silver", "1800"), ("Reposado", "1800")),
    # same name, different brand
    (("Original", "Grey Goose"), ("Original", "Absolut")),
])
def test_different_bottles_different_keys(a, b):
    assert key(*a) != key(*b)


def test_no_two_seeded_products_share_a_key():
    by_key = defaultdict(list)
    for p in SEED_PRODUCTS:
        by_key[key(p["name"], p.get("brand"))].append(p["name"])
    assert {k: v for k, v in by_key.items() if len(v) > 1} == {}


def test_every_seeded_product_is_reachable_from_the_answer_the_prompt_asks_for():
    # The model is taught seed_display_name()'s form; each must land on its row.
    for p in SEED_PRODUCTS:
        shown = seed_display_name(p["name"], p.get("brand"))
        assert key(shown, p.get("brand")) == key(p["name"], p.get("brand")), p["name"]
        assert "750ml" not in shown.lower() and "12oz" not in shown.lower()


# ─── the prompt's product list ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def main_module():
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
    if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
        sys.modules["database"].init_db = lambda: None
    import main
    return main


def test_prompt_list_teaches_the_catalogs_own_names(main_module):
    catalog = main_module.PRODUCT_CATALOG
    assert "Johnnie Walker: Red Label | Black Label" in catalog
    assert "Grey Goose: Original |" in catalog
    assert "Tito's: Handmade" in catalog
    assert "Coors: Banquet | Light" in catalog
    assert main_module.PRODUCT_CATALOG in main_module.BOTTLE_PROMPT


def test_prompt_list_has_no_junk_duplicates_or_sizes(main_module):
    catalog = main_module.PRODUCT_CATALOG
    assert "Putaendo" not in catalog
    assert "750ml" not in catalog.lower() and "12oz" not in catalog.lower()
    lines = catalog.splitlines()
    brand_lines = [line for line in lines[1:-1] if ": " in line]
    assert len(brand_lines) > 200
    for line in brand_lines:
        names = line.split(": ", 1)[1].split(" | ")
        assert len(names) == len(set(names)), line


def test_spelling_only_brands_are_not_already_in_the_catalog(main_module):
    seeded = {key(None, p["brand"]) for p in SEED_PRODUCTS}
    extras_line = main_module.PRODUCT_CATALOG.splitlines()[-1]
    assert extras_line.startswith("Other brands (spelling only): ")
    for brand in extras_line.split(": ", 1)[1].split(", "):
        assert key(None, brand) not in seeded, brand


# ─── sizes: ignored by the key, but a size the scan read must agree ───────────

@pytest.mark.parametrize("text, ml", [
    ("Tito's Handmade 750ml", 750), ("Handmade 1L", 1000), ("Handmade 1.75 L", 1750),
    ("Light 12oz", 12 * 29.5735), ("Blue Bolt 28 fl oz", 28 * 29.5735), ("Mini 5cl", 50),
    ("Old No. 7", None), ("1800 Silver", None), ("Glenfiddich 12", None), ("", None), (None, None),
])
def test_size_ml(text, ml):
    from helpers import size_ml
    assert size_ml(text) == (pytest.approx(ml) if ml is not None else None)


def test_sizes_compatible():
    from helpers import sizes_compatible, size_ml
    assert not sizes_compatible(size_ml("1L"), size_ml("750ml"))      # a litre read never lands on a 750
    assert not sizes_compatible(size_ml("1.75L"), size_ml("1L"))
    assert sizes_compatible(size_ml("12oz"), size_ml("355ml"))       # same can, two units
    assert sizes_compatible(None, size_ml("750ml"))                  # the scan read no size: key decides
    assert sizes_compatible(size_ml("750 ML"), None)


# ─── seeded names the prompt can't produce ───────────────────────────────────

def test_no_seeded_name_is_a_descriptor_the_prompt_turns_into_original(main_module):
    """The prompt answers a base product's descriptor ("classic", "original
    taste"…) with "Original". A seed named one of them is never reached: the
    Coca-Cola seed was "Classic", and every Coke scan minted a duplicate."""
    import re
    rule = re.search(r"Descriptor phrases like (.+?) mean base product", main_module.BOTTLE_PROMPT)
    phrases = re.findall(r'"([^"]+)"', rule.group(1))
    assert "classic" in phrases
    for p in SEED_PRODUCTS:
        assert seed_display_name(p["name"], p.get("brand")).lower() not in phrases, p["name"]
    assert "Coca-Cola: Original | Diet Coke" in main_module.PRODUCT_CATALOG


def _real_database_module():
    """database.py itself, even where another test file has stubbed
    sys.modules["database"]. Its pool is lazy: nothing connects."""
    import importlib.util
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
    spec = importlib.util.spec_from_file_location("database_for_rename_test", "database.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RenameCursor:
    def __init__(self, seeded):
        self.seeded, self.sql, self.rows = seeded, [], []

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.sql.append((sql, params))
        if sql.startswith("UPDATE products SET name"):
            new, _key, _now, brand, old = params
            self.rows = [r for r in self.seeded if (r["brand"], r["name"]) == (brand, old)]
            for r in self.rows:
                r["name"] = new
        else:
            self.rows = []

    def fetchall(self):
        return [{"id": r["id"]} for r in self.rows]


class RenameConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def test_a_database_seeded_as_classic_is_renamed_once_keeping_the_row():
    db = _real_database_module()
    cur = RenameCursor([{"id": "coke-row", "brand": "Coca-Cola", "name": "Classic"}])
    assert db.rename_seed_products(RenameConn(cur)) == 1
    update, alias = cur.sql[0], cur.sql[1]
    assert "source = 'seed'" in update[0] and "deleted_at IS NULL" in update[0]
    assert update[1][:2] == ("Original", key("Original", "Coca-Cola"))    # key moves with it
    assert alias[0].startswith("INSERT INTO product_aliases") and "DO NOTHING" in alias[0]
    assert alias[1][1:4] == ("coke-row", "classic", "cocacola")         # "Classic" still lands here
    cur.sql.clear()
    assert db.rename_seed_products(RenameConn(cur)) == 0                # the next boot writes nothing
    assert not any(s.startswith("INSERT") for s, _ in cur.sql)


def test_every_rename_matches_the_seed_list():
    db = _real_database_module()
    seeded = {(p.get("brand"), p["name"]) for p in SEED_PRODUCTS}
    for brand, old, new in db.SEED_RENAMES:
        assert (brand, new) in seeded and (brand, old) not in seeded


def test_boot_renames_before_it_seeds():
    """Seeding skips a product whose UPC exists, so the rename has to run
    first, on every boot, for an already-seeded database to change at all."""
    import inspect
    src = inspect.getsource(_real_database_module().init_db)
    assert src.index("rename_seed_products(conn)") < src.index("seed_products(conn)\n")
