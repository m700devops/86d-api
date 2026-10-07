"""The customer's own phone, from the bar-name screen (PATCH /users/me).

Optional; blank clears it; anything else must be a dialable US number and is
stored dashed (615-742-9095), the form the CRM copies into CloudTalk.
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
from fastapi import HTTPException  # noqa: E402
from models import UpdateProfileRequest  # noqa: E402


@pytest.mark.parametrize("raw", ["(615) 742-9095", "615.742.9095", "+1 615 742 9095", "6157429095"])
def test_a_us_number_is_stored_dashed(raw):
    assert main._clean_profile_phone(raw) == "615-742-9095"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_clears_it(raw):
    assert main._clean_profile_phone(raw) is None


@pytest.mark.parametrize("raw", ["12345", "call me", "+44 20 7946 0958", "911"])
def test_a_number_nobody_can_call_is_refused(raw):
    with pytest.raises(HTTPException) as e:
        main._clean_profile_phone(raw)
    assert e.value.status_code == 422 and e.value.detail["error"] == "invalid_phone"


def _db(monkeypatch, log):
    row = {"id": "u1", "email": "a@b.com", "name": "A", "business_name": "Bar", "manager_name": None,
           "phone": "615-742-9095", "subscription_status": "trial", "subscription_tier": "starter",
           "trial_ends_at": None, "terms_accepted_at": None, "privacy_accepted_at": None,
           "created_at": "2026-10-01T00:00:00+00:00"}

    class Cur:
        rowcount = 1
        def execute(self, sql, params=()): log.append((" ".join(sql.split()), params))
        def fetchone(self): return row

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)


def test_the_save_writes_the_cleaned_number_and_returns_it(monkeypatch):
    log = []
    _db(monkeypatch, log)
    out = main.update_user_profile(UpdateProfileRequest(business_name="Bar", phone="(615) 742 9095"), "u1")
    sql, params = log[0]
    assert sql.startswith("UPDATE users SET business_name = %s, phone = %s")
    assert params[:2] == ("Bar", "615-742-9095")
    assert out["phone"] == "615-742-9095"
    assert "phone" in log[1][0]  # the profile read back includes it


def test_a_bad_number_saves_nothing(monkeypatch):
    log = []
    _db(monkeypatch, log)
    with pytest.raises(HTTPException):
        main.update_user_profile(UpdateProfileRequest(business_name="Bar", phone="12345"), "u1")
    assert log == []
