"""Ordering by the case: a label on top of a quantity that stays in bottles.

Bars order fast movers by the case (cheaper per bottle, no split-case fee) and
the top shelf by the bottle. Order lines used to carry only "x 24", which a rep
reads as 24 cases, 24 bottles or 2 cases depending on the item. Now a bar can
set a bottle to order by the case (par_levels.order_unit / case_size), the app
sends `unit`/`case_size` on that line, and the email says both:
"x 2 cases (12/cs, 24 bottles)".

`quantity` stays BOTTLES everywhere, which is what keeps every old path right:
cost totals, the history, an old build reordering a case order. And a bottle
line is untouched — same dict, same hash, same email text — so a retry that
straddles the deploy still matches its first send.
"""
import sys
import types
from contextlib import contextmanager

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import helpers  # noqa: E402
import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from models import OrderLineItem, ParLevelResponse, ProductStockUpdate  # noqa: E402
from pydantic import ValidationError  # noqa: E402


def _email(items):
    return helpers.order_email(1042, "Breakthru", "Olde Town Tavern", "", items,
                               "Dan", "October 8, 2026")[1]


# ── the email ────────────────────────────────────────────────────────────────

def test_a_case_line_says_cases_pack_and_bottles():
    body = _email([{"name": "Tito's", "size": "1L", "quantity": 24, "unit": "case", "case_size": 12}])
    assert "- Tito's 1L x 2 cases (12/cs, 24 bottles)" in body
    assert "Total: 2 cases (24 bottles)\n" in body


def test_one_case_is_singular_and_a_part_case_shows_the_loose_bottles():
    body = _email([{"name": "Jameson", "quantity": 12, "unit": "case", "case_size": 12},
                   {"name": "Hennessy", "quantity": 15, "unit": "case", "case_size": 12}])
    assert "- Jameson x 1 case (12/cs, 12 bottles)" in body
    assert "- Hennessy x 1 case + 3 bottles (12/cs, 15 bottles)" in body


def test_under_a_case_never_says_zero_cases():
    body = _email([{"name": "Malibu", "quantity": 5, "unit": "case", "case_size": 6}])
    assert "- Malibu x 5 bottles (6/cs)\n" in body
    assert "0 case" not in body


def test_a_mixed_order_totals_cases_and_loose_bottles():
    body = _email([{"name": "Tito's", "quantity": 24, "unit": "case", "case_size": 12},
                   {"name": "Macallan 12", "quantity": 2},
                   {"name": "Smirnoff", "size": "1.75L", "quantity": 6, "unit": "case", "case_size": 6}])
    assert "- Macallan 12 x 2\n" in body                      # a bottle line, as before
    assert "Total: 3 cases + 2 bottles (32 bottles)\n" in body


def test_a_bottle_only_order_is_the_old_email_byte_for_byte():
    items = [{"name": "Tito's", "size": "1L", "quantity": 2}, {"name": "Jameson", "quantity": 1.5}]
    body = _email(items)
    assert "- Tito's 1L x 2\n- Jameson x 1.5\n\nTotal: 3.5 bottles\n" in body
    # A "bottle" unit or a stray case_size on a bottle line changes nothing.
    assert _email([dict(items[0], unit="bottle", case_size=12), items[1]]) == body
    assert _email([dict(items[0], unit="case", case_size=None), items[1]]) == body


# ── the order line ───────────────────────────────────────────────────────────

def _line(**kw):
    return main._order_line(main.OrderEmailItem(**{"name": "Tito's", "quantity": 24, **kw}))


def test_a_bottle_line_is_the_same_dict_and_hash_as_before():
    old = {"name": "Tito's", "quantity": 24.0, "size": None, "price": None}   # pydantic makes it a float, as before
    assert _line() == old and _line(unit="bottle") == old
    assert main._items_hash([_line()]) == main._items_hash([old])


def test_a_case_line_carries_its_unit_and_hashes_differently():
    line = _line(unit="case", case_size=12)
    assert line["unit"] == "case" and line["case_size"] == 12 and line["quantity"] == 24
    assert main._items_hash([line]) != main._items_hash([_line()])
    assert main._items_hash([line]) != main._items_hash([_line(unit="case", case_size=6)])


def test_a_case_line_needs_a_sane_pack_size():
    with pytest.raises(ValidationError):
        main.OrderEmailItem(name="Tito's", quantity=24, unit="case")
    with pytest.raises(ValidationError):
        main.OrderEmailItem(name="Tito's", quantity=24, unit="case", case_size=1)
    with pytest.raises(ValidationError):
        main.OrderEmailItem(name="Tito's", quantity=24, unit="crate", case_size=12)


def test_an_old_app_payload_still_parses():
    req = main.SendOrderEmailsRequest(location_id="loc", orders=[
        {"distributor_id": "d1", "items": [{"name": "Tito's", "quantity": 2, "price": 20.0}]}])
    assert main._order_line(req.orders[0].items[0]) == {
        "name": "Tito's", "quantity": 2, "size": None, "price": 20.0}


def test_history_keeps_the_unit_through_the_response_model():
    # GET /orders runs through response_model; an undeclared key is dropped.
    line = OrderLineItem(name="Tito's", quantity=24, unit="case", case_size=12)
    assert line.model_dump()["unit"] == "case" and line.case_size == 12
    assert OrderLineItem(name="Jameson", quantity=1).unit is None


def test_cost_stays_per_bottle_for_a_case_line(monkeypatch):
    saved = {}

    class Cur:
        def execute(self, sql, params=()):
            if "INSERT INTO orders" in sql:
                saved["cost"] = params[5]

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=Cur, commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    req = main.SendOrderEmailsRequest(location_id="loc", orders=[
        {"distributor_id": "d1", "items": [{"name": "Tito's", "quantity": 1}]}])
    dists = [{"items": [{"name": "Tito's", "quantity": 24, "price": 20.0, "unit": "case", "case_size": 12}]}]
    main._save_order_history(req, "u", dists, [], "Bar", "Dan", 1042)
    assert saved["cost"] == 480.0          # 24 bottles x $20, not 2 x $20


# ── the bar's setting (PATCH /locations/{id}/products/{pid}, GET par-levels) ──

class _Cur:
    def __init__(self, log, existing):
        self.log, self.existing = log, existing

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        if "FROM par_levels" in sql:
            self._out = [self.existing] if self.existing else []
        elif "FROM locations" in sql or "FROM products" in sql:
            self._out = [{"id": "x"}]
        else:
            self._out = []

    def fetchone(self):
        return self._out[0] if self._out else None


def _patch(monkeypatch, existing=None, **body):
    log = []

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: _Cur(log, existing), commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    out = main.update_product_stock("loc", "p1", ProductStockUpdate(**body), "u")
    insert = next(p for s, p in log if s.startswith("INSERT INTO par_levels"))
    return out, insert


ROW = {"par_quantity": 6, "full_quantity": 0, "current_stock": 2, "price": 20,
       "par_set_at": "t", "order_unit": None, "case_size": None}


def test_setting_case_ordering_is_saved_and_returned(monkeypatch):
    out, insert = _patch(monkeypatch, ROW, order_unit="case", case_size=12)
    assert out["order_unit"] == "case" and out["case_size"] == 12
    assert insert[8:10] == ("case", 12)
    assert out["par"] == 6 and out["price"] == 20       # nothing else moved


def test_a_patch_that_doesnt_mention_it_keeps_it(monkeypatch):
    out, insert = _patch(monkeypatch, dict(ROW, order_unit="case", case_size=6), price=25)
    assert out["order_unit"] == "case" and out["case_size"] == 6 and insert[8:10] == ("case", 6)


def test_a_new_row_defaults_to_bottles(monkeypatch):
    out, insert = _patch(monkeypatch, None, par=4)
    assert out["order_unit"] == "bottle" and out["case_size"] is None


def test_case_with_no_pack_size_is_refused_not_guessed(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _patch(monkeypatch, ROW, order_unit="case")
    assert e.value.status_code == 422 and e.value.detail["error"] == "case_size_required"
    with pytest.raises(HTTPException):         # clearing the size of a case bottle
        _patch(monkeypatch, dict(ROW, order_unit="case", case_size=12), case_size=0)


def test_back_to_bottles_keeps_the_pack_size_for_next_time(monkeypatch):
    out, _ = _patch(monkeypatch, dict(ROW, order_unit="case", case_size=12), order_unit="bottle")
    assert out["order_unit"] == "bottle" and out["case_size"] == 12


def test_the_book_response_declares_the_fields():
    base = dict(id="1", location_id="l", product_id="p", par_quantity=6, updated_at="2026-10-08T00:00:00")
    assert ParLevelResponse(**base).order_unit == "bottle"
    r = ParLevelResponse(**base, order_unit="case", case_size=12)
    assert r.model_dump()["case_size"] == 12
