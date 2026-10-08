"""Resend's delivery webhook: a bounced or spam-reported distributor address is
recorded on the distributor, so the app can warn instead of failing silently.
The signature check is Svix's scheme (what Resend signs with), checked here
against a signature computed independently in the test."""
import asyncio
import base64
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
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import email_events  # noqa: E402
import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402

KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
SECRET = "whsec_" + KEY


def _sign(body: bytes, msg_id="msg_1", ts=None, key=KEY):
    ts = str(int(time.time()) if ts is None else ts)
    sig = base64.b64encode(hmac.new(base64.b64decode(key), f"{msg_id}.{ts}.".encode() + body,
                                    hashlib.sha256).digest()).decode()
    return msg_id, ts, f"v1,{sig}"


def _event(kind, to="Orders@Metro.com", **data):
    return {"type": kind, "data": {"email_id": "e1", "to": [to], **data}}


# ── the signature ────────────────────────────────────────────────────────────

def test_a_real_signature_verifies():
    body = b'{"type":"email.bounced"}'
    mid, ts, sig = _sign(body)
    assert email_events.verify(SECRET, mid, ts, sig, body, time.time())
    assert email_events.verify(SECRET, mid, ts, "v1,bogus " + sig, body, time.time())  # key rotation


def test_forged_tampered_or_stale_is_refused():
    body = b'{"type":"email.bounced"}'
    mid, ts, sig = _sign(body)
    now = time.time()
    assert not email_events.verify(SECRET, mid, ts, sig, body + b" ", now)              # tampered
    other = base64.b64encode(b"x" * 32).decode()
    assert not email_events.verify(SECRET, *_sign(body, key=other), body, now)           # wrong key
    assert not email_events.verify(SECRET, mid, ts, sig, body, now + 600)               # replayed late
    assert not email_events.verify(SECRET, mid, "soon", sig, body, now)
    assert not email_events.verify(SECRET, "", ts, sig, body, now)
    assert not email_events.verify("", mid, ts, sig, body, now)
    assert not email_events.verify("whsec_!!!notbase64", mid, ts, sig, body, now)


# ── what an event means ──────────────────────────────────────────────────────

def test_a_permanent_bounce_is_recorded_with_its_reason():
    kind, to, reason = email_events.classify(_event(
        "email.bounced", bounce={"type": "Permanent", "message": "550 5.1.1 user unknown"}))
    assert (kind, to, reason) == ("bounced", ["orders@metro.com"], "550 5.1.1 user unknown")


def test_a_transient_bounce_changes_nothing():
    assert email_events.classify(_event("email.bounced", bounce={"type": "Transient"}))[0] is None


def test_complaint_delivery_and_everything_else():
    assert email_events.classify(_event("email.complained"))[0] == "complained"
    assert email_events.classify(_event("email.delivered"))[0] == "delivered"
    assert email_events.classify(_event("email.opened"))[0] is None
    assert email_events.classify({})[0] is None
    assert email_events.classify(_event("email.bounced", to="not-an-address"))[1] == []


def test_bare_address():
    assert email_events.bare_address("Metro Beverage <Orders@Metro.com>") == "orders@metro.com"
    assert email_events.bare_address(" A@B.com ") == "a@b.com"


# ── the route ────────────────────────────────────────────────────────────────

class _Req:
    def __init__(self, body: bytes, headers: dict):
        self._body, self.headers = body, headers

    async def body(self):
        return self._body


def _post(monkeypatch, event, secret=SECRET, sign=True):
    log = []

    class Cur:
        rowcount = 2

        def execute(self, sql, params=()):
            log.append((" ".join(sql.split()), params))

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=Cur, commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    if secret is None:
        monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)
    body = json.dumps(event).encode()
    mid, ts, sig = _sign(body)
    headers = {"svix-id": mid, "svix-timestamp": ts, "svix-signature": sig if sign else "v1,nope"}
    return asyncio.run(main.resend_webhook(_Req(body, headers))), log


def test_a_bounce_marks_every_distributor_with_that_address(monkeypatch):
    out, log = _post(monkeypatch, _event("email.bounced", bounce={"type": "Permanent", "message": "no such user"}))
    assert out == {"ok": True, "recorded": 2}
    sql, params = log[0]
    assert sql.startswith("UPDATE distributors SET email_problem = %s")
    assert params[0] == "bounced" and params[1] == "no such user" and params[3] == ["orders@metro.com"]


def test_a_delivery_clears_it(monkeypatch):
    _, log = _post(monkeypatch, _event("email.delivered"))
    assert "email_problem = NULL" in log[0][0] and log[0][1] == (["orders@metro.com"],)


def test_nothing_to_record_writes_nothing(monkeypatch):
    out, log = _post(monkeypatch, _event("email.opened"))
    assert out["recorded"] == 0 and log == []


def test_unsigned_is_401_and_no_secret_is_503(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _post(monkeypatch, _event("email.bounced"), sign=False)
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        _post(monkeypatch, _event("email.bounced"), secret=None)
    assert e.value.status_code == 503


def test_the_route_is_registered():
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/webhooks/resend" in paths
