"""One price for everyone: $49.99/month (STRIPE_PRICE_ID).

There was a $29.99 launch price for the first 10 accounts
(STRIPE_LAUNCH_PRICE_ID); the owner dropped it on 2026-10-07. These pin that
nothing reads it any more — even if it's still set on Render — so the paywall
and Stripe checkout can't disagree on what a bar pays.
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


def _env(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_regular")
    # Left over on Render from the launch offer: must be ignored.
    monkeypatch.setenv("STRIPE_LAUNCH_PRICE_ID", "price_launch")


class _Cur:
    def __init__(self, row=None):
        self.row, self.sql = row, []

    def execute(self, sql, params=()):
        self.sql.append(" ".join(sql.split()))

    def fetchone(self):
        return self.row

    def fetchall(self):
        # Asked who the first accounts are, it would be this one: the old code
        # gave it the launch price.
        return [{"id": "u1"}]


def test_the_price_route_answers_one_price_and_never_fails(monkeypatch):
    _env(monkeypatch)
    cur = _Cur()

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cur)

    monkeypatch.setattr(main, "get_db", db)
    # The very first account ever made pays the same as everyone.
    assert main.billing_price("u1") == {"price": "$49.99", "per": "month", "launch": False,
                                        "regular_price": "$49.99", "first_charge_date": None}
    assert not any("ORDER BY created_at" in q for q in cur.sql)   # no "first 10" ranking

    @contextmanager
    def broken():
        raise RuntimeError("pool exhausted")
        yield

    monkeypatch.setattr(main, "get_db", broken)
    assert main.billing_price("u1")["price"] == "$49.99"


def test_checkout_uses_the_one_price_for_the_first_account_too(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(main.stripe, "api_key", "sk_test")

    @contextmanager
    def db():
        yield types.SimpleNamespace(
            cursor=lambda: _Cur({"email": "bar@x.com", "stripe_customer_id": "cus_1"}),
            commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    seen = {}

    def create(**kw):
        seen.update(kw)
        return types.SimpleNamespace(url="https://checkout")

    monkeypatch.setattr(main.stripe.checkout.Session, "create", create)
    monkeypatch.setattr(main.stripe.Subscription, "list",
                        lambda **kw: types.SimpleNamespace(data=[]))  # no live subscription yet
    assert main.create_checkout_session("u1") == {"checkout_url": "https://checkout"}
    assert seen["line_items"] == [{"price": "price_regular", "quantity": 1}]


def test_nothing_reads_the_launch_price_any_more():
    import inspect
    src = inspect.getsource(main)
    assert "STRIPE_LAUNCH_PRICE_ID\")" not in src and "LAUNCH_PRICE_SLOTS" not in src
