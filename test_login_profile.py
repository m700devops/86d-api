"""Sign-in hands the app the WHOLE account, the same as /users/me.

The app stores whatever user a sign-in returns. Login used to return a short
version without business_name / manager_name, so after every sign-in the order
screen asked "what's your bar called?" again — for a bar that had told us.
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

import main  # noqa: E402
from models import AppleSignInRequest, TokenResponse, UserLogin  # noqa: E402

ROW = {
    "id": "u1", "email": "dana@cornertavern.com", "name": "Dana Reyes",
    "business_name": "The Corner Tavern", "manager_name": "Dana Reyes",
    "phone": "615-742-9095", "title": "GM", "order_reply_to": None,
    "count_day": "sun", "count_time": "after_close",
    "subscription_status": None, "subscription_tier": None,
    "trial_ends_at": "2026-11-10T00:00:00+00:00", "terms_accepted_at": None,
    "privacy_accepted_at": None, "created_at": "2026-10-26T00:00:00+00:00",
    "password_hash": "$2b$12$secret-hash",
}


def _db(monkeypatch, row):
    sql = []

    class Cur:
        def execute(self, q, params=()): sql.append(" ".join(q.split()))
        def fetchone(self): return dict(row)

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    return sql


def _bare_name(col):
    return col.strip()


def test_login_returns_the_bar_name_and_everything_else(monkeypatch):
    sql = _db(monkeypatch, ROW)
    monkeypatch.setattr(main, "verify_password", lambda pw, h: True)
    main._login_failures.clear()
    out = main.login(UserLogin(email="dana@cornertavern.com", password="hunter22"))
    user = out["user"]
    assert user["business_name"] == "The Corner Tavern"
    assert user["manager_name"] == "Dana Reyes"
    assert user["count_day"] == "sun" and user["phone"] == "615-742-9095"
    assert user["subscription_status"] == "trial" and user["subscription_tier"] == "starter"
    # the hash is selected to check the password, and never handed back
    assert "password_hash" not in user
    assert "password_hash" in sql[0]
    # and it survives the response model (a field the model lacks is dropped silently)
    validated = TokenResponse(**out).model_dump()["user"]
    assert validated["business_name"] == "The Corner Tavern"
    assert validated["manager_name"] == "Dana Reyes"


def test_login_and_users_me_read_the_same_columns():
    keys = [_bare_name(c) for c in main.PROFILE_COLUMNS.split(",")]
    for needed in ("business_name", "manager_name", "phone", "title", "order_reply_to",
                   "count_day", "count_time"):
        assert needed in keys
    assert "password_hash" not in keys


def test_returning_apple_user_gets_the_whole_profile(monkeypatch):
    row = {k: v for k, v in ROW.items() if k != "password_hash"}
    _db(monkeypatch, row)
    monkeypatch.setattr(main, "fetch_apple_keys", lambda: [])
    monkeypatch.setattr(main, "verify_identity_token",
                        lambda tok, keys, aud: {"sub": "apple-sub-1", "email": "dana@cornertavern.com"})
    out = main.apple_sign_in(AppleSignInRequest(identity_token="x" * 40))
    user = out["user"]
    assert user["business_name"] == "The Corner Tavern"
    assert user["manager_name"] == "Dana Reyes"
    assert TokenResponse(**out).model_dump()["user"]["manager_name"] == "Dana Reyes"


@pytest.mark.parametrize("missing", ["business_name", "manager_name"])
def test_a_blank_profile_stays_blank(monkeypatch, missing):
    row = dict(ROW, **{missing: None})
    _db(monkeypatch, row)
    monkeypatch.setattr(main, "verify_password", lambda pw, h: True)
    main._login_failures.clear()
    out = main.login(UserLogin(email="dana@cornertavern.com", password="hunter22"))
    assert out["user"][missing] is None
