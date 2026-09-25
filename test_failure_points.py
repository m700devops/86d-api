"""The failure points found in the September 2026 audit, each pinned.

- The Stripe webhook read the event through the library's object, and
  stripe-python 15 stopped making that a dict: every webhook raised, so a
  customer who paid stayed locked out. It now reads the verified payload.
- The scan's fallback skipped Gemini exactly when OpenAI was broken (a
  rejected key) or answered in something other than JSON.
- Sending an order held a database transaction across the emails, so a
  failure after they had gone rolled the record back, answered 500, and the
  manager — told it failed — sent it again. A client_ref now makes a retry
  skip every distributor already emailed this exact order.
- The inbox retried a mail that always fails every 5 minutes, a paid model
  call each time.

Fake cursors stand in for Postgres; the SQL itself is exercised against a real
database by the audit's end-to-end run.
"""
import asyncio
import hashlib
import hmac
import json
import sys
import time
import types
from contextlib import contextmanager

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub
# Another test file may have made the stub first, without init_db — main needs it.
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import main  # noqa: E402
import crm  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Cur:
    def __init__(self, log, rows=None):
        self.log, self.rows, self.rowcount = log, rows or {}, 1

    def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), params))
        self._out = next((v for k, v in self.rows.items() if k in sql), [])

    def fetchone(self):
        return self._out[0] if self._out else None

    def fetchall(self):
        return list(self._out)


def _db(monkeypatch, module, rows=None):
    log = []

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: _Cur(log, rows), commit=lambda: None)

    monkeypatch.setattr(module, "get_db", db)
    return log


# ── the Stripe webhook ───────────────────────────────────────────────────────

SECRET = "whsec_test"


def _signed(event: dict):
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return payload.encode(), f"t={ts},v1={sig}"


class _Req:
    def __init__(self, body, sig):
        self._body, self.headers = body, {"stripe-signature": sig}

    async def body(self):
        return self._body


def _hook(monkeypatch, event, sig=None):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    body, good = _signed(event)
    return asyncio.run(main.billing_webhook(_Req(body, sig or good)))


def test_a_paid_checkout_activates_the_account_on_the_installed_stripe(monkeypatch):
    log = _db(monkeypatch, main)
    out = _hook(monkeypatch, {"id": "evt_1", "object": "event", "type": "checkout.session.completed",
                              "data": {"object": {"object": "checkout.session", "id": "cs_1",
                                                  "client_reference_id": "user-1", "customer": "cus_9",
                                                  "payment_status": "paid"}}})
    assert out == {"received": True}
    sql, params = log[-1]
    assert "subscription_status = 'active'" in sql and params[0] == "cus_9" and params[2] == "user-1"


def test_the_user_id_can_come_from_metadata(monkeypatch):
    log = _db(monkeypatch, main)
    _hook(monkeypatch, {"type": "checkout.session.completed", "data": {"object": {
        "customer": "cus_9", "metadata": {"user_id": "user-2"}}}})
    assert log[-1][1][2] == "user-2"


def test_an_unpaid_checkout_waits_for_the_subscription(monkeypatch):
    log = _db(monkeypatch, main)
    _hook(monkeypatch, {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": "user-1", "customer": "cus_9", "payment_status": "unpaid"}}})
    assert not any("UPDATE users" in q for q, _ in log)


@pytest.mark.parametrize("status,expected", [("active", "active"), ("past_due", "active"),
                                             ("canceled", "canceled"), ("unpaid", "canceled")])
def test_subscription_changes_land(monkeypatch, status, expected):
    log = _db(monkeypatch, main)
    _hook(monkeypatch, {"type": "customer.subscription.updated",
                        "data": {"object": {"customer": "cus_9", "status": status}}})
    assert log[-1][1][0] == expected and log[-1][1][2] == "cus_9"


def test_a_forged_webhook_is_refused(monkeypatch):
    _db(monkeypatch, main)
    with pytest.raises(HTTPException) as e:
        _hook(monkeypatch, {"type": "checkout.session.completed", "data": {"object": {}}},
              sig=f"t={int(time.time())},v1=deadbeef")
    assert e.value.status_code == 400


# ── the scan's fallback ──────────────────────────────────────────────────────

def _scan(monkeypatch, openai_behaviour, gemini_text=None, gemini_key="g"):
    import httpx
    calls = []

    async def fake_openai(key, prompt, image):
        calls.append("openai")
        if openai_behaviour == "auth":
            raise main.openai.AuthenticationError(
                "bad key", response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                body=None)
        return openai_behaviour

    async def fake_gemini(key, prompt, image):
        calls.append("gemini")
        return gemini_text

    monkeypatch.setattr(main, "_call_openai", fake_openai)
    monkeypatch.setattr(main, "_call_gemini", fake_gemini)
    monkeypatch.setattr(main, "_process_ai_result",
                        lambda text, req, uid: {"parsed": json.loads(text)})
    req = types.SimpleNamespace(image="aGk=")
    result = asyncio.run(main._run_providers("o", gemini_key, "p", req, "u"))
    return calls, result


def test_a_rejected_openai_key_falls_back_to_gemini(monkeypatch):
    calls, result = _scan(monkeypatch, "auth", '{"name": "Tito\'s"}')
    assert calls == ["openai", "gemini"] and result["parsed"]["name"] == "Tito's"


def test_an_unreadable_openai_answer_falls_back_to_gemini(monkeypatch):
    calls, result = _scan(monkeypatch, "I think this is vodka.", '{"name": "Tito\'s"}')
    assert calls == ["openai", "gemini"] and result["parsed"]["name"] == "Tito's"


def test_both_unreadable_is_still_parse_failed(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _scan(monkeypatch, "vodka?", "also vodka?")
    assert e.value.status_code == 500 and e.value.detail["error"] == "parse_failed"


def test_a_rejected_key_with_no_fallback_says_so(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _scan(monkeypatch, "auth", gemini_key=None)
    assert e.value.status_code == 503


# ── sending an order ─────────────────────────────────────────────────────────

def test_the_items_hash_ignores_order_but_not_quantity():
    a = [{"name": "Tito's", "quantity": 2, "size": None, "price": 20.0},
         {"name": "Jameson", "quantity": 1, "size": "1L", "price": None}]
    assert main._items_hash(a) == main._items_hash(list(reversed(a)))
    b = [dict(a[0], quantity=3), a[1]]
    assert main._items_hash(a) != main._items_hash(b)


def _send(monkeypatch, claims, ref="ref-1", history_fails=False):
    """One send of two distributors, with the claim store, Resend and the
    history write faked. Returns (response, emails actually sent)."""
    _db(monkeypatch, main, rows={
        "FROM locations": [{"id": "loc"}],
        "FROM users": [{"name": "Dan", "email": "dan@bar.com", "business_name": "Barrel",
                        "manager_name": "Dan"}],
        "FROM distributors": [{"id": "d1", "name": "Breakthru", "email": "orders@bt.com"},
                              {"id": "d2", "name": "RNDC", "email": "rep@rndc.com"}]})
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    sent = []
    monkeypatch.setattr(main, "_send_via_resend",
                        lambda key, to, subject, body, **kw: (sent.append(to) or (True, None)))
    numbers = iter([1043, 1044])
    monkeypatch.setattr(main, "_draw_order_number", lambda uid: next(numbers))

    def claim(uid, r, dist_id, ihash):
        held = claims.get((r, dist_id, ihash))
        if held and held["status"] == "sent":
            return held
        claims[(r, dist_id, ihash)] = {"status": "sending", "order_number": None, "email": None}
        return None

    def finish(uid, r, dist_id, ihash, ok, number, email, error):
        claims[(r, dist_id, ihash)] = {"status": "sent" if ok else "failed",
                                       "order_number": number if ok else None, "email": email}

    monkeypatch.setattr(main, "_claim_send", claim)
    monkeypatch.setattr(main, "_finish_send", finish)

    def history(*a, **k):
        if history_fails:
            raise RuntimeError("database went away")
        return "order-1"

    monkeypatch.setattr(main, "_save_order_history", history)
    req = main.SendOrderEmailsRequest(location_id="loc", client_ref=ref, orders=[
        {"distributor_id": "d1", "items": [{"name": "Tito's", "quantity": 2}]},
        {"distributor_id": "d2", "items": [{"name": "Jameson", "quantity": 1}]}])
    return main.send_order_emails(req, "user-1"), sent


def test_a_retry_after_a_lost_response_emails_nobody_twice(monkeypatch):
    claims = {}
    first, sent1 = _send(monkeypatch, claims)
    assert sent1 == ["orders@bt.com", "rep@rndc.com"] and first["order_number"] == 1043
    again, sent2 = _send(monkeypatch, claims)          # same ref: the app never heard back
    assert sent2 == []
    assert [r["status"] for r in again["results"]] == ["sent", "sent"]
    assert all(r["already_sent"] and r["order_number"] == 1043 for r in again["results"])
    assert again["order_number"] == 1043 and again["order_id"] is None


def test_changed_items_are_a_new_order_not_a_retry(monkeypatch):
    claims = {}
    _send(monkeypatch, claims)
    for key in list(claims):                     # d1's items changed since
        if key[1] == "d1":
            claims.pop(key)
    _, sent = _send(monkeypatch, claims)
    assert sent == ["orders@bt.com"]


def test_a_history_failure_after_sending_is_not_a_500(monkeypatch):
    out, sent = _send(monkeypatch, {}, history_fails=True)
    assert len(sent) == 2 and out["sent"] == 2 and out["order_id"] is None


def test_an_old_app_without_a_ref_sends_as_before(monkeypatch):
    claims = {}
    _send(monkeypatch, claims, ref=None)
    _, sent = _send(monkeypatch, claims, ref=None)
    assert len(sent) == 2 and claims == {}


# ── the inbox's retry cap ────────────────────────────────────────────────────

def test_a_mail_that_always_fails_stops_after_three_tries(monkeypatch):
    import mailer
    log = _db(monkeypatch, crm, rows={"FROM crm_leads WHERE email IS NOT NULL": [
        {"id": "L1", "email": "jed@fbr.com"}]})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(mailer, "is_configured", lambda: True)
    monkeypatch.setattr(mailer, "sender", lambda: "stephan@my86d.com")
    monkeypatch.setattr(mailer, "fetch_recent", lambda days=3: [b"raw"])
    mail = {"message_id": "<m1@x>", "from_addr": "jed@fbr.com", "from_name": "Jed",
            "subject": "hi", "date": None, "text": "hello"}
    import inbox
    monkeypatch.setattr(inbox, "parse", lambda raw: dict(mail))
    monkeypatch.setattr(inbox, "worth_reading", lambda m, me: True)
    monkeypatch.setattr(inbox, "match_leads", lambda m, book, sent: ["L1"])
    reads = []

    def failing(m, ids):
        reads.append(1)
        raise RuntimeError("model said something unparseable")

    monkeypatch.setattr(crm, "_read_reply", failing)
    crm._INBOX_FAILS.clear()
    for _ in range(3):
        crm.process_inbox()
    assert len(reads) == 3
    recorded = [p for q, p in log if q.startswith("INSERT INTO crm_inbox")]
    assert len(recorded) == 1 and recorded[0][6] == "failed"
