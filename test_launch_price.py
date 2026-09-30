"""The price: $49.99/month, except the first LAUNCH_PRICE_SLOTS (10) real
accounts, who check out at the $29.99 launch price.

Fake cursors stand in for Postgres: they check the query asks for the oldest
live accounts that aren't test/review accounts, and hand back rows.
"""
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import main  # noqa: E402
import crm  # noqa: E402


class _Cur:
    def __init__(self, first_ids, log):
        self.first_ids, self.log = first_ids, log

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))

    def fetchall(self):
        limit = self.log[-1][1][1]
        return [{"id": i} for i in self.first_ids[:limit]]


def _env(monkeypatch, launch=True):
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_regular")
    if launch:
        monkeypatch.setenv("STRIPE_LAUNCH_PRICE_ID", "price_launch")
    else:
        monkeypatch.delenv("STRIPE_LAUNCH_PRICE_ID", raising=False)


def test_the_first_ten_get_the_launch_price_and_the_eleventh_does_not(monkeypatch):
    _env(monkeypatch)
    ids = [f"u{i}" for i in range(1, 16)]
    log = []
    assert main._price_for(_Cur(ids, log), "u10") == {
        "price_id": "price_launch", "label": "$29.99", "launch": True}
    assert main._price_for(_Cur(ids, log), "u11") == {
        "price_id": "price_regular", "label": "$49.99", "launch": False}
    sql, params = log[-1]
    assert "deleted_at IS NULL" in sql and "email !~*" in sql and "ORDER BY created_at, id" in sql
    assert params == (crm.TEST_EMAIL_PATTERN, 10)


def test_no_launch_price_configured_means_everyone_pays_the_regular_price(monkeypatch):
    _env(monkeypatch, launch=False)
    log = []
    p = main._price_for(_Cur(["u1"], log), "u1")
    assert p["price_id"] == "price_regular" and not p["launch"] and log == []


def test_zero_slots_ends_the_offer(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(main, "LAUNCH_PRICE_SLOTS", 0)
    assert main._price_for(_Cur(["u1"], []), "u1")["price_id"] == "price_regular"


def test_the_price_route_answers_the_label_and_never_fails(monkeypatch):
    _env(monkeypatch)

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: _Cur(["u1"], []))

    monkeypatch.setattr(main, "get_db", db)
    assert main.billing_price("u1") == {"price": "$29.99", "per": "month", "launch": True,
                                        "regular_price": "$49.99"}

    @contextmanager
    def broken():
        raise RuntimeError("pool exhausted")
        yield

    monkeypatch.setattr(main, "get_db", broken)
    assert main.billing_price("u1")["price"] == "$49.99"


def test_checkout_uses_the_launch_price_for_a_launch_account(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(main.stripe, "api_key", "sk_test")

    class Cur(_Cur):
        def fetchone(self):
            return {"email": "bar@x.com", "stripe_customer_id": "cus_1"}

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(["u1"], []), commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    seen = {}

    def create(**kw):
        seen.update(kw)
        return types.SimpleNamespace(url="https://checkout")

    monkeypatch.setattr(main.stripe.checkout.Session, "create", create)
    assert main.create_checkout_session("u1") == {"checkout_url": "https://checkout"}
    assert seen["line_items"] == [{"price": "price_launch", "quantity": 1}]
    main.create_checkout_session("u2")
    assert seen["line_items"] == [{"price": "price_regular", "quantity": 1}]
