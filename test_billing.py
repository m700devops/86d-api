"""Billing hardening: billing.py's rules, Checkout, the webhook and /billing/price.

Stripe is faked at the library call (the real stripe-python is installed, so
the attribute names are real). The database is a fake with TRANSACTIONS:
writes only land on commit, an INSERT ... ON CONFLICT DO NOTHING behaves like
Postgres's (a second insert of the same key returns no row), and two threads
share it, which is what the concurrent-duplicate test needs.
"""
import sys
import threading
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2.errors
import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import billing  # noqa: E402
import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


# ── billing.checkout_trial_end ───────────────────────────────────────────────

@pytest.mark.parametrize("left,expect", [
    (timedelta(days=10), True),
    (timedelta(hours=48, minutes=5), True),          # exactly the limit
    (timedelta(hours=48, minutes=4), False),         # just under it: charge now
    (timedelta(days=1), False),                      # 1 day left → charge now (no extension)
    (timedelta(hours=-1), False),                    # trial over
])
def test_trial_end_only_when_checkout_will_take_it(left, expect):
    out = billing.checkout_trial_end("trial", iso(NOW + left), NOW)
    assert out == (int((NOW + left).timestamp()) if expect else None)


@pytest.mark.parametrize("status,ends", [("active", NOW + timedelta(days=9)),
                                         ("canceled", NOW + timedelta(days=9)),
                                         ("trial", None)])
def test_no_trial_end_outside_a_running_trial(status, ends):
    assert billing.checkout_trial_end(status, ends and iso(ends), NOW) is None


def test_a_naive_timestamp_is_read_as_utc():
    naive = (NOW + timedelta(days=5)).replace(tzinfo=None).isoformat()
    assert billing.checkout_trial_end("trial", naive, NOW) == int((NOW + timedelta(days=5)).timestamp())


# ── billing.app_status ───────────────────────────────────────────────────────

@pytest.mark.parametrize("stripe_status", ["active", "past_due", "trialing"])
def test_subscribed_statuses_are_active(stripe_status):
    assert billing.app_status(stripe_status, None, NOW) == "active"


@pytest.mark.parametrize("stripe_status", ["canceled", "unpaid", "incomplete_expired", "incomplete"])
def test_an_ended_subscription_keeps_free_days_left(stripe_status):
    assert billing.app_status(stripe_status, iso(NOW + timedelta(days=6)), NOW) == "trial"
    assert billing.app_status(stripe_status, iso(NOW - timedelta(days=1)), NOW) == "canceled"
    assert billing.app_status(stripe_status, None, NOW) == "canceled"


# ── billing.first_charge_date ────────────────────────────────────────────────

def test_a_stripe_trial_reports_its_own_end_not_null():
    """Status 'active' locally (trialing maps to active), stale trial_ends_at:
    the date comes from Stripe's subscription."""
    end = NOW + timedelta(days=7)
    sub = {"status": "trialing", "trial_end": int(end.timestamp())}
    assert billing.first_charge_date(sub, "active", iso(NOW - timedelta(days=3)), NOW) == end.date().isoformat()


def test_first_charge_without_a_subscription():
    assert billing.first_charge_date(None, "trial", iso(NOW + timedelta(days=9)), NOW) == \
        (NOW + timedelta(days=9)).date().isoformat()
    assert billing.first_charge_date(None, "trial", iso(NOW + timedelta(hours=20)), NOW) is None
    assert billing.first_charge_date({"status": "active"}, "active", None, NOW) is None


def test_subscription_id_of():
    assert billing.subscription_id_of({"type": "checkout.session.completed",
                                       "data": {"object": {"subscription": "sub_1"}}}) == "sub_1"
    assert billing.subscription_id_of({"type": "customer.subscription.deleted",
                                       "data": {"object": {"id": "sub_2"}}}) == "sub_2"
    assert billing.subscription_id_of({"type": "invoice.paid", "data": {"object": {"id": "in_1"}}}) is None


# ── a fake Postgres with transactions ────────────────────────────────────────

class Store:
    def __init__(self, users):
        self.users = {u["id"]: dict(u) for u in users}
        self.events = set()
        self.lock = threading.Lock()   # the event table's unique index
        self.updates = []
        self.connections = 0
        self.commit_raises = None


class Conn:
    def __init__(self, store):
        self.s, self.pending_events, self.pending_updates = store, [], []

    def cursor(self):
        return Cur(self)

    def commit(self):
        if self.s.commit_raises:
            raise self.s.commit_raises
        with self.s.lock:
            self.s.events.update(self.pending_events)
        for uid, vals in self.pending_updates:
            self.s.users[uid].update(vals)
            self.s.updates.append((uid, vals))
        self.pending_events, self.pending_updates = [], []

    def rollback(self):
        self.pending_events, self.pending_updates = [], []


class Cur:
    def __init__(self, conn):
        self.c, self._row = conn, None

    def execute(self, sql, params=()):
        s = self.c.s
        q = " ".join(sql.split())
        self._row = None
        if q.startswith("INSERT INTO stripe_events"):
            with s.lock:
                if params[0] in s.events or params[0] in _claimed:
                    return
                _claimed.add(params[0])
            self.c.pending_events.append(params[0])
            self._row = {"event_id": params[0]}
        elif q.startswith("SELECT id, trial_ends_at FROM users"):
            col = q.split("WHERE ")[1].split(" ")[0]
            hit = [u for u in s.users.values() if u.get(col) == params[0]]
            self._row = {"id": hit[0]["id"], "trial_ends_at": hit[0].get("trial_ends_at")} if hit else None
        elif q.startswith("UPDATE users SET subscription_status"):
            status, sub_id, cust, _, uid = params
            self.c.pending_updates.append((uid, {"subscription_status": status,
                                                 "stripe_subscription_id": sub_id,
                                                 **({"stripe_customer_id": cust} if cust else {})}))
        elif q.startswith("SELECT email, stripe_customer_id"):
            u = s.users[params[0]]
            self._row = {k: u.get(k) for k in ("email", "stripe_customer_id", "subscription_status", "trial_ends_at")}
        elif q.startswith("SELECT subscription_status, trial_ends_at, stripe_subscription_id"):
            u = s.users[params[0]]
            self._row = {k: u.get(k) for k in ("subscription_status", "trial_ends_at", "stripe_subscription_id")}
        elif "FROM users WHERE deleted_at IS NULL AND email !~*" in q:   # launch-price ranking
            self._row = []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._row or []


_claimed = set()  # ids an uncommitted transaction holds (Postgres would block on them)


@pytest.fixture
def store(monkeypatch):
    _claimed.clear()
    st = Store([{"id": "u1", "email": "bar@x.com", "stripe_customer_id": "cus_1",
                 "subscription_status": "trial", "trial_ends_at": iso(NOW + timedelta(days=10)),
                 "stripe_subscription_id": None}])

    @contextmanager
    def db():
        st.connections += 1
        conn = Conn(st)
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr(main, "get_db", db)
    monkeypatch.setattr(main, "datetime", _FrozenDatetime)
    monkeypatch.setattr(main.stripe, "api_key", "sk_test")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_regular")
    monkeypatch.delenv("STRIPE_LAUNCH_PRICE_ID", raising=False)
    return st


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def _stripe_sub(monkeypatch, status, sub_id="sub_1", calls=None, **extra):
    def retrieve(_id):
        if calls is not None:
            calls.append(("retrieve", _id))
        return types.SimpleNamespace(to_dict=lambda: {"id": sub_id, "status": status, "customer": "cus_1",
                                                      "metadata": {"user_id": "u1"}, **extra})
    monkeypatch.setattr(main.stripe.Subscription, "retrieve", retrieve)


def _event(eid, kind="customer.subscription.updated", status="trialing", sub_id="sub_1"):
    obj = {"id": sub_id, "status": status, "customer": "cus_1"} if kind.startswith("customer.") \
        else {"subscription": sub_id, "client_reference_id": "u1", "customer": "cus_1", "payment_status": "paid"}
    return {"id": eid, "type": kind, "data": {"object": obj}}


# ── the webhook ──────────────────────────────────────────────────────────────

def test_trialing_is_active_and_the_subscription_is_stored(store, monkeypatch):
    _stripe_sub(monkeypatch, "trialing")
    main._handle_billing_event(_event("evt_1", "checkout.session.completed"))
    u = store.users["u1"]
    assert u["subscription_status"] == "active" and u["stripe_subscription_id"] == "sub_1"


def test_a_late_trialing_event_after_active_leaves_active(store, monkeypatch):
    """The event says trialing, Stripe now says active: Stripe wins."""
    _stripe_sub(monkeypatch, "active")
    main._handle_billing_event(_event("evt_2", status="trialing"))
    assert store.users["u1"]["subscription_status"] == "active"


def test_a_stale_canceled_event_cannot_lock_out_a_subscriber(store, monkeypatch):
    """An old 'canceled' delivered after they re-subscribed: the event's own
    copy says canceled, Stripe now says active. Trusting the event would lock
    a paying customer out until the next event."""
    store.users["u1"]["trial_ends_at"] = iso(NOW - timedelta(days=3))
    _stripe_sub(monkeypatch, "active")
    main._handle_billing_event(_event("evt_2b", status="canceled"))
    assert store.users["u1"]["subscription_status"] == "active"


def test_the_same_event_twice_writes_once(store, monkeypatch):
    _stripe_sub(monkeypatch, "active")
    main._handle_billing_event(_event("evt_3"))
    main._handle_billing_event(_event("evt_3"))
    assert len(store.updates) == 1


def test_two_concurrent_deliveries_write_once_and_neither_fails(store, monkeypatch):
    _stripe_sub(monkeypatch, "active")
    barrier, errors = threading.Barrier(2), []

    def deliver():
        barrier.wait()
        try:
            main._handle_billing_event(_event("evt_4"))
        except Exception as e:   # would be a 500, and Stripe would retry an applied event
            errors.append(e)

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(store.updates) == 1


def test_a_unique_violation_at_commit_is_answered_not_raised(store, monkeypatch, capsys):
    _stripe_sub(monkeypatch, "active")
    store.commit_raises = psycopg2.errors.UniqueViolation()
    main._handle_billing_event(_event("evt_5"))          # no exception = 200
    assert store.updates == [] and "BILLING_EVENT_DUPLICATE evt_5 (concurrent)" in capsys.readouterr().out


def test_stripe_is_asked_before_any_connection_and_a_failure_records_nothing(store, monkeypatch):
    def boom(_id):
        assert store.connections == 0, "no DB connection may be held during the Stripe call"
        raise main._STRIPE_ERROR("down")
    monkeypatch.setattr(main.stripe.Subscription, "retrieve", boom)
    with pytest.raises(HTTPException) as e:
        main._handle_billing_event(_event("evt_6"))
    assert e.value.status_code == 503
    assert store.connections == 0 and store.events == set() and store.updates == []
    # Stripe retries the event later and it applies then.
    _stripe_sub(monkeypatch, "active")
    main._handle_billing_event(_event("evt_6"))
    assert store.users["u1"]["subscription_status"] == "active"


def test_deleted_during_the_free_trial_keeps_the_free_days(store, monkeypatch):
    store.users["u1"]["stripe_subscription_id"] = "sub_1"
    _stripe_sub(monkeypatch, "canceled")
    main._handle_billing_event(_event("evt_7", "customer.subscription.deleted", "canceled"))
    u = store.users["u1"]
    assert u["subscription_status"] == "trial" and u["stripe_subscription_id"] is None


def test_deleted_after_the_trial_is_canceled(store, monkeypatch):
    store.users["u1"].update(stripe_subscription_id="sub_1", trial_ends_at=iso(NOW - timedelta(days=2)))
    _stripe_sub(monkeypatch, "canceled")
    main._handle_billing_event(_event("evt_8", "customer.subscription.deleted", "canceled"))
    assert store.users["u1"]["subscription_status"] == "canceled"


def test_an_unknown_subscription_writes_nothing(store, monkeypatch, capsys):
    monkeypatch.setattr(main.stripe.Subscription, "retrieve", lambda _id: types.SimpleNamespace(
        to_dict=lambda: {"id": "sub_x", "status": "active", "customer": "cus_x", "metadata": {}}))
    main._handle_billing_event(_event("evt_9", sub_id="sub_x"))
    assert store.updates == [] and "BILLING_USER_NOT_FOUND" in capsys.readouterr().out


def test_events_that_are_not_about_a_subscription_are_ignored(store, monkeypatch):
    monkeypatch.setattr(main.stripe.Subscription, "retrieve", lambda _id: pytest.fail("no lookup"))
    main._handle_billing_event({"id": "evt_10", "type": "invoice.paid", "data": {"object": {"id": "in_1"}}})
    assert store.connections == 0


# ── Checkout ─────────────────────────────────────────────────────────────────

def _checkout(monkeypatch, subs=()):
    seen = {}
    monkeypatch.setattr(main.stripe.Subscription, "list", lambda **kw: types.SimpleNamespace(
        data=[{"id": f"sub_{i}", "status": s} for i, s in enumerate(subs)]))
    monkeypatch.setattr(main.stripe.billing_portal.Session, "create",
                        lambda **kw: types.SimpleNamespace(url="https://portal"))

    def create(**kw):
        seen.update(kw)
        return types.SimpleNamespace(url="https://checkout")
    monkeypatch.setattr(main.stripe.checkout.Session, "create", create)
    return seen


def test_subscribing_mid_trial_bills_when_the_trial_ends(store, monkeypatch):
    seen = _checkout(monkeypatch)
    assert main.create_checkout_session("u1") == {"checkout_url": "https://checkout"}
    assert seen["subscription_data"]["trial_end"] == int((NOW + timedelta(days=10)).timestamp())
    assert seen["payment_method_collection"] == "always"
    assert "trial_settings" not in seen["subscription_data"]


def test_one_day_left_is_charged_now(store, monkeypatch):
    store.users["u1"]["trial_ends_at"] = iso(NOW + timedelta(days=1))
    seen = _checkout(monkeypatch)
    main.create_checkout_session("u1")
    assert "trial_end" not in seen["subscription_data"]


@pytest.mark.parametrize("live", ["active", "trialing", "past_due", "unpaid"])
def test_an_existing_subscription_is_a_409_not_a_second_checkout(store, monkeypatch, live):
    seen = _checkout(monkeypatch, subs=["canceled", live])
    with pytest.raises(HTTPException) as e:
        main.create_checkout_session("u1")
    assert e.value.status_code == 409 and seen == {}
    assert e.value.detail["error"] == "already_subscribed"
    assert "Manage Subscription" in e.value.detail["message"]   # what every app build shows
    assert e.value.detail["portal_url"] == "https://portal"


def test_only_ended_subscriptions_do_not_block(store, monkeypatch):
    seen = _checkout(monkeypatch, subs=["canceled", "incomplete_expired"])
    assert main.create_checkout_session("u1") == {"checkout_url": "https://checkout"}
    assert seen


# ── /billing/price first_charge_date ─────────────────────────────────────────

def test_price_reports_the_stripe_trial_end_for_a_subscriber(store, monkeypatch):
    end = NOW + timedelta(days=6)
    store.users["u1"].update(subscription_status="active", stripe_subscription_id="sub_1",
                             trial_ends_at=iso(NOW - timedelta(days=1)))
    _stripe_sub(monkeypatch, "trialing", trial_end=int(end.timestamp()))
    assert main.billing_price("u1")["first_charge_date"] == end.date().isoformat()


def test_price_reports_the_trial_end_before_subscribing(store, monkeypatch):
    out = main.billing_price("u1")
    assert out["first_charge_date"] == (NOW + timedelta(days=10)).date().isoformat()
    assert out["price"] == "$49.99"          # the existing fields are unchanged


def test_price_survives_stripe_being_down(store, monkeypatch):
    store.users["u1"].update(subscription_status="active", stripe_subscription_id="sub_1")
    monkeypatch.setattr(main.stripe.Subscription, "retrieve", lambda _id: (_ for _ in ()).throw(RuntimeError("x")))
    assert main.billing_price("u1")["first_charge_date"] is None
