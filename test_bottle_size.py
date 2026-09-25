"""The bottle size: read off the label, checked against the model's own reading
of it, used in matching, stored on new products, returned to the app for the
count and the order line.

Bars stock the same spirit in 750ml, 1L and 1.75L, and the scanner had no idea
which one it was looking at: the matcher ignored sizes, new products were stored
without one, and a distributor's order said "Tito's Handmade x 6". Everything
here is checked without a network or a database — the matcher's SQL is faked
per step, and was also run against a real Postgres (see CLAUDE.md).
"""
import contextlib
import json
import os
import sys
import time

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import main  # noqa: E402
from helpers import (  # noqa: E402
    label_shows_size, normalize_size, single_fit, size_fits, size_ml, sizes_compatible,
)

IMAGE = "aGVsbG8="


# ─── reading a size ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, ml, label", [
    ("750 ML", 750, "750ml"),
    ("75 cl", 750, "750ml"),
    ("70CL", 700, "700ml"),
    ("0.75 L", 750, "750ml"),
    ("1 LITER", 1000, "1L"),
    ("1 Lt.", 1000, "1L"),
    ("1.0 L", 1000, "1L"),
    ("1000 ML", 1000, "1L"),
    ("1.75 L", 1750, "1.75L"),
    ("1,75 L", 1750, "1.75L"),            # decimal comma
    ("1,750 ml", 1750, "1.75L"),          # thousands separator
    ("0,7 l", 700, "700ml"),
    ("50 ML", 50, "50ml"),
    ("187 ML", 187, "187ml"),
    ("5 LITRES", 5000, "5L"),
    ("12 FL OZ", 354.9, "12oz"),
    ("12 fl. oz.", 354.9, "12oz"),
    ("16.9 FL OZ", 499.8, "16.9oz"),
    ("1 PINT", 473.2, "16oz"),
    ("1 PT. 9.4 FL. OZ.", 751.2, "25.4oz"),   # a 750ml beer, the way US beer labels say it
    ("1 PT 6 FL OZ", 650.6, "22oz"),          # a bomber
    ("1 QT 8 FL OZ", 1182.9, "40oz"),         # a forty
    ("12 FL OZ (355 mL)", 354.9, "12oz"),     # the first size printed
    ("ALC.40%VOL.750ML", 750, "750ml"),
    ("Patrón 750 mL", 750, "750ml"),
    ("Grey Goose Original 750ml", 750, "750ml"),
    ("Coors Coors Light 12oz", 354.9, "12oz"),
])
def test_reads_sizes_as_labels_print_them(text, ml, label):
    assert size_ml(text) == pytest.approx(ml, abs=0.1)
    assert normalize_size(text) == label
    assert normalize_size(label) == label     # the catalog's own form reads back unchanged


@pytest.mark.parametrize("text", [
    "OLD NO. 7 BRAND", "80 PROOF", "12 YEAR OLD", "No. 7 LABEL", "Absolut 100",
    "75 L",      # a misread "75 cl": no bar counts a 75-litre bottle
    "1 ml", "", None,
])
def test_things_that_are_not_sizes(text):
    assert size_ml(text) is None
    assert normalize_size(text) == ""


def test_sizes_compatible_joins_units_not_neighbouring_sizes():
    assert sizes_compatible(size_ml("12 FL OZ"), size_ml("355 mL"))
    assert sizes_compatible(size_ml("1 PT. 9.4 FL. OZ."), size_ml("750 ML"))
    assert sizes_compatible(size_ml("33.8 FL OZ"), size_ml("1 L"))
    assert sizes_compatible(size_ml("59.2 FL OZ"), size_ml("1.75 L"))
    assert sizes_compatible(size_ml("8.4 FL OZ"), size_ml("250 ML"))
    assert not sizes_compatible(size_ml("720 ML"), size_ml("750 ML"))   # real, different bottles
    assert not sizes_compatible(size_ml("700 ML"), size_ml("750 ML"))
    assert not sizes_compatible(size_ml("1 L"), size_ml("750 ML"))
    assert not sizes_compatible(size_ml("1.75 L"), size_ml("1 L"))
    assert sizes_compatible(None, size_ml("750 ML"))                   # unknown never contradicts
    assert sizes_compatible(size_ml("750 ML"), None)


@pytest.mark.parametrize("size, label, shown", [
    ("750 ML", "TITO'S HANDMADE VODKA 40% ALC/VOL 750 ML", True),
    ("355 mL", "BUD LIGHT 12 FL OZ", True),                  # the same size, another unit
    ("750ml", "JACK DANIEL'S OLD NO. 7 750ML 2 OZ POUR", True),  # any size printed will do
    ("750 ML", "TITO'S HANDMADE VODKA", False),              # not on the label: from memory
    ("1 L", "TITO'S HANDMADE VODKA 750 ML", False),          # on the label, but a different one
    ("", "TITO'S HANDMADE VODKA 750 ML", False),
    ("750 ML", "", False),
    ("750 ML", None, False),
])
def test_label_shows_size(size, label, shown):
    assert label_shows_size(size, label) is shown


def test_size_parsing_cannot_stall_on_a_hostile_reading():
    for text in ("1" * 200_000, "1 " * 100_000, "1,000" * 40_000, "1 pt " * 40_000,
                 "1." * 100_000, "12 fl " * 30_000):
        started = time.monotonic()
        list(size_ml(t) for t in (text,))
        normalize_size(text)
        label_shows_size("750 ML", text)
        assert time.monotonic() - started < 1.0


# ─── choosing between products by size ───────────────────────────────────────

P750 = {"id": "p750", "size": "750ml", "name": "Handmade"}
P1L = {"id": "p1l", "size": "1L", "name": "Handmade"}
PNONE = {"id": "pnone", "size": None, "name": "Handmade"}
SEED = {"id": "seed", "size": None, "name": "Tito's Handmade 750ml"}   # size only in the name


def ids(rows):
    return [r["id"] for r in rows]


def test_size_fits():
    assert ids(size_fits([PNONE, P750, P1L], size_ml("1 L"))) == ["p1l", "pnone"]  # known first
    assert ids(size_fits([PNONE, P750, P1L], size_ml("750 ML"))) == ["p750", "pnone"]
    assert ids(size_fits([PNONE, P750, P1L], size_ml("1.75 L"))) == ["pnone"]     # no size = wildcard
    assert ids(size_fits([PNONE, P750, P1L], None)) == ["pnone", "p750", "p1l"]   # order unchanged
    assert ids(size_fits([SEED], size_ml("1 L"))) == []                          # the name's size counts


def test_single_fit():
    assert single_fit([P750, P1L], size_ml("1 L"))["id"] == "p1l"
    assert single_fit([PNONE, P1L], size_ml("1 L"))["id"] == "p1l"   # recorded size beats none
    assert single_fit([PNONE, P1L], size_ml("750 ML"))["id"] == "pnone"
    assert single_fit([P750, P1L], None) is None                      # no size read: a guess
    assert single_fit([P750, dict(P750, id="p750b")], size_ml("750 ML")) is None  # two of that size
    assert single_fit([P750], None)["id"] == "p750"


# ─── the model's answer ──────────────────────────────────────────────────────

def reply(size, label, name="Handmade", brand="Tito's", confidence=0.93):
    return json.dumps({"label_text": label, "name": name, "brand": brand, "category": "spirits",
                       "product_type": "Vodka", "size": size, "confidence": confidence})


def test_schema_and_prompt_ask_for_the_size():
    schema = main.SCAN_SCHEMA
    assert "size" in schema["required"]
    assert set(schema["required"]) == set(schema["properties"])  # strict mode needs all of them
    assert schema["required"][0] == "label_text"                  # still read before anything else
    assert '"size": "Net contents' in main.BOTTLE_PROMPT           # Gemini gets the shape from here
    assert '"size":""' in main.BOTTLE_PROMPT                       # ...including the no-bottle reply


def test_parse_keeps_the_size_as_a_short_string():
    assert main._parse_ai_result(reply(" 750 ML ", "X"))["size"] == "750 ML"
    assert main._parse_ai_result(json.dumps({"name": "x", "size": 750}))["size"] == ""
    assert main._parse_ai_result(json.dumps({"name": "x"}))["size"] == ""   # an older reply
    assert len(main._parse_ai_result(reply("7" * 500, "X"))["size"]) == 40


@pytest.fixture
def lookup(monkeypatch):
    """Stub the read-only catalog lookup; returns what it was asked about."""
    seen = []

    def find(result, user, location=None):
        seen.append(dict(result))
        return ("p1", "exact")
    monkeypatch.setattr(main, "_find_product", find)
    return seen


def evaluate(text):
    return main._evaluate_answer(text, main.ScanAnalyzeRequest(image=IMAGE), "u1", None,
                                 "openai", "gpt-4o")


def test_a_size_on_the_label_is_used_normalized(lookup):
    answer = evaluate(reply("1.75 LITERS", "TITO'S HANDMADE VODKA 1.75 LITERS"))
    assert answer.result["size"] == "1.75L"
    assert answer.result["size_read"] == "1.75 LITERS"
    assert lookup[0]["size"] == "1.75L"                  # what the matcher was given


def test_a_size_missing_from_the_reading_is_dropped(lookup):
    answer = evaluate(reply("750 ML", "TITO'S HANDMADE VODKA"))
    assert answer.result["size"] == ""                   # remembered, not read
    assert answer.result["size_read"] == "750 ML"        # kept for the log
    assert lookup[0]["size"] == ""
    assert answer.readable                               # the product itself still counts


def test_a_reading_with_no_size_is_fine(lookup):
    answer = evaluate(reply("", "TITO'S HANDMADE VODKA"))
    assert answer.readable and answer.result["size"] == ""


# ─── two providers ───────────────────────────────────────────────────────────

def answer(size, product_id="p1", provider="openai"):
    a = main._Answer(provider, "m", {"name": "Handmade", "brand": "Tito's", "size": size,
                                     "confidence": 0.9, "label_text": "TITO'S HANDMADE"})
    a.product_id, a.method, a.label_supported = product_id, "exact", True
    return a


def test_two_sizes_read_differently_are_a_disagreement_even_on_one_product():
    decision = main._decide([answer("750ml"), answer("1.75L", provider="gemini")])
    assert decision["opinion"] == "disagree" and decision["confirm"]
    assert not decision["allow_create"]
    assert decision["other"].label() == "Tito's Handmade 1.75L"   # what the app shows


@pytest.mark.parametrize("a, b", [("750ml", "750ml"), ("750ml", ""), ("", ""), ("12oz", "355ml")])
def test_matching_or_missing_sizes_still_agree(a, b):
    assert main._decide([answer(a), answer(b, provider="gemini")])["opinion"] == "agree"


def test_label_without_a_size():
    assert answer("").label() == "Tito's Handmade"


# ─── the matcher, with its SQL faked per step ────────────────────────────────

class FakeCursor:
    def __init__(self, db):
        self.db, self.rows = db, []

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.db.queries.append((sql, params))
        if self.db.fail:
            raise RuntimeError("database down")
        self.rows = [dict(r) for r in self.db.answer(sql, params)]

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDB:
    def __init__(self, answer=lambda sql, params: [], fail=False):
        self.answer, self.fail, self.queries, self.commits = answer, fail, [], 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def use_db(monkeypatch, db):
    monkeypatch.setattr(main, "get_db", lambda: contextlib.nullcontext(db))
    return db


def steps(bar_book=(), exact=()):
    """The bar's own bottles and the exact-name step answer; every other step finds nothing."""
    def answer(sql, params):
        if "JOIN par_levels" in sql:
            return bar_book
        if "LOWER(name) = LOWER(%s)" in sql:
            return exact
        return []
    return answer


def find(size, location="loc-1"):
    return main._find_product({"name": "Handmade", "brand": "Tito's", "size": size}, "u1", location)


def test_bar_book_takes_the_bottle_of_the_size_read(monkeypatch):
    use_db(monkeypatch, FakeDB(steps(bar_book=[P750, P1L])))
    assert find("1L") == ("p1l", "bar_book")
    assert find("750ml") == ("p750", "bar_book")
    assert find("") == (None, "none")          # no size read: two fits, no guess


def test_bar_book_prefers_the_recorded_size_to_none(monkeypatch):
    use_db(monkeypatch, FakeDB(steps(bar_book=[PNONE, P1L])))
    assert find("1L") == ("p1l", "bar_book")
    assert find("750ml") == ("pnone", "bar_book")


def test_exact_name_of_another_size_is_not_this_bottle(monkeypatch):
    use_db(monkeypatch, FakeDB(steps(exact=[P750, P1L])))
    assert find("1L", location=None) == ("p1l", "exact")
    assert find("", location=None) == ("p750", "exact")      # no size read: as before
    assert find("1.75L", location=None) == (None, "none")    # both known, neither this one


def test_a_product_recorded_at_the_size_read_beats_one_with_none(monkeypatch):
    use_db(monkeypatch, FakeDB(steps(exact=[PNONE, P1L])))   # the database's own order puts PNONE first
    assert find("1L", location=None) == ("p1l", "exact")
    assert find("", location=None) == ("pnone", "exact")


def test_a_size_in_the_stored_name_counts(monkeypatch):
    use_db(monkeypatch, FakeDB(steps(exact=[SEED])))
    assert find("1L", location=None) == (None, "none")
    assert find("750ml", location=None) == ("seed", "exact")


# ─── counting and creating ───────────────────────────────────────────────────

def result(size, confidence=0.93):
    return {"name": "Handmade", "brand": "Tito's", "category": "spirits", "product_type": "",
            "size": size, "confidence": confidence}


def claimed(stored):
    return lambda sql, params: [{"size": stored}] if "RETURNING size" in sql else []


def test_a_counted_product_reports_its_recorded_size(monkeypatch):
    use_db(monkeypatch, FakeDB(claimed("750 mL")))          # as someone typed it into the catalog
    assert main._record_match(result("75cl"), "u1", "p1", "exact") == ("p1", False, "exact", "750 mL")


def test_a_product_with_no_size_reports_the_size_read_and_is_not_changed(monkeypatch):
    db = use_db(monkeypatch, FakeDB(claimed(None)))
    assert main._record_match(result("1.75L"), "u1", "p1", "exact") == ("p1", False, "exact", "1.75L")
    assert not any("SET size" in sql for sql, _ in db.queries)   # one bar's read never sets it


def test_no_size_anywhere_is_empty(monkeypatch):
    use_db(monkeypatch, FakeDB(claimed(None)))
    assert main._record_match(result(""), "u1", "p1", "exact")[3] == ""


def test_a_new_product_is_stored_with_the_size_read(monkeypatch):
    db = use_db(monkeypatch, FakeDB())
    new_id, is_new, method, size = main._record_match(result("1.75L"), "u1", None, "none")
    assert (is_new, method, size) == (True, "auto_created", "1.75L")
    insert = [params for sql, params in db.queries if sql.startswith("INSERT INTO products")]
    assert insert and insert[0][4] == "1.75L"                     # the size column


def test_a_new_product_with_no_size_read_stores_none(monkeypatch):
    db = use_db(monkeypatch, FakeDB())
    main._record_match(result(""), "u1", None, "none")
    insert = [params for sql, params in db.queries if sql.startswith("INSERT INTO products")]
    assert insert[0][4] is None


def test_a_database_failure_still_reports_the_size_read(monkeypatch):
    use_db(monkeypatch, FakeDB(fail=True))
    assert main._record_match(result("1L"), "u1", "p1", "exact") == (None, False, "none", "1L")


# ─── the response and the log ────────────────────────────────────────────────

def respond(monkeypatch, text, recorded_size):
    monkeypatch.setattr(main, "_find_product", lambda result, user, location=None: ("p1", "exact"))
    monkeypatch.setattr(main, "_record_match",
                        lambda result, user, pid, method, allow_create=True:
                        (pid, False, method, recorded_size))
    event = {"id": "scan-1"}
    response = main._process_ai_result(text, main.ScanAnalyzeRequest(image=IMAGE), "u1", event)
    return response, event


def test_the_response_carries_the_size_to_order_by(monkeypatch):
    response, event = respond(monkeypatch, reply("1 LITER", "TITO'S HANDMADE VODKA 1 LITER"), "1L")
    assert response.size == "1L"
    assert (event["size"], event["size_read"]) == ("1L", "1 LITER")


def test_the_log_keeps_a_dropped_size(monkeypatch):
    response, event = respond(monkeypatch, reply("750 ML", "TITO'S HANDMADE VODKA"), "")
    assert response.size == ""
    assert (event["size"], event["size_read"]) == (None, "750 ML")


def test_an_unreadable_answer_has_no_size(monkeypatch):
    response, _ = respond(monkeypatch, reply("750 ML", "TITO'S 750 ML", confidence=0.4), "750ml")
    assert response.match_method == "unreadable" and response.size == ""
