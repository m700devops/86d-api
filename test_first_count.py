"""Sign-up → first count: the count day on the profile, the post-sign-up app
events, the CRM's activation stage, the "haven't scanned yet" list and the
welcome email. Pure or with a faked database — no network, no Postgres.
"""
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import crm  # noqa: E402
import main  # noqa: E402
import welcome  # noqa: E402
from models import UpdateProfileRequest  # noqa: E402
from pydantic import ValidationError  # noqa: E402


# ---------- count day on the profile ----------

def test_a_count_day_and_time_are_accepted():
    r = UpdateProfileRequest(count_day="sun", count_time="after_close")
    assert r.model_dump(exclude_unset=True) == {"count_day": "sun", "count_time": "after_close"}


@pytest.mark.parametrize("field,value", [("count_day", "sunday"), ("count_day", "Sun"),
                                         ("count_time", "night"), ("count_time", "23:00")])
def test_anything_else_is_refused(field, value):
    with pytest.raises(ValidationError):
        UpdateProfileRequest(**{field: value})


def test_null_clears_it():
    assert UpdateProfileRequest(count_day=None).model_dump(exclude_unset=True) == {"count_day": None}


def test_the_profile_saves_and_reads_back_the_count_day(monkeypatch):
    log = []
    row = {"id": "u1", "email": "a@b.com", "name": "A", "business_name": "Bar",
           "count_day": "sun", "count_time": "after_close",
           "subscription_status": "trial", "subscription_tier": "starter",
           "created_at": "2026-10-01T00:00:00+00:00"}

    class Cur:
        rowcount = 1
        def execute(self, sql, params=()): log.append((" ".join(sql.split()), params))
        def fetchone(self): return row

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    out = main.update_user_profile(UpdateProfileRequest(count_day="sun", count_time="after_close"), "u1")
    assert log[0][0].startswith("UPDATE users SET count_day = %s, count_time = %s")
    assert log[0][1][:2] == ("sun", "after_close")
    assert "count_day, count_time" in log[1][0]
    assert out["count_day"] == "sun" and out["count_time"] == "after_close"


# ---------- app events ----------

def test_the_steps_after_sign_up_are_allowlisted():
    for e in ("start_screen_viewed", "start_tapped", "camera_allowed", "camera_denied",
              "first_scan_sent", "first_scan_identified", "first_bottle_saved", "first_try_done",
              "count_day_set", "reminder_on", "reminder_opened", "order_sent"):
        assert e in main.APP_EVENTS
    # and the pre-sign-up five are still there
    assert {"app_opened", "register_succeeded"} <= main.APP_EVENTS


# ---------- activation stage ----------

@pytest.mark.parametrize("row,stage", [
    ({}, "not_started"),
    ({"scans": 0, "bottles_in_draft": 0, "orders_sent": 0}, "not_started"),
    ({"scans": 3}, "tried"),
    ({"scans": 3, "bottles_in_draft": 2}, "counting"),
    ({"scans": 0, "bottles_in_draft": 5}, "counting"),   # typed in by hand, no scan
    ({"scans": 40, "bottles_in_draft": 0, "orders_sent": 1}, "ordered"),
    ({"scans": None, "bottles_in_draft": None, "orders_sent": None}, "not_started"),
])
def test_the_stage_is_what_they_did(row, stage):
    assert crm.activation_stage(row) == stage


def test_count_day_text():
    assert crm.count_day_text("sun", "after_close") == "Sunday after close"
    assert crm.count_day_text("mon", None) == "Monday"
    assert crm.count_day_text("varies", "afternoon") == "No set day"
    assert crm.count_day_text(None, "afternoon") is None


def test_activation_reads_scans_and_drafts_not_just_sent_orders():
    sql = crm.ACTIVATION_SQL
    assert "FROM scan_events" in sql and "FROM inventory_drafts" in sql
    assert "orders_sent" in sql


# ---------- the CRM's "haven't scanned yet" list ----------

def _fake_db(monkeypatch, module, rows_by_call):
    log = []

    class Cur:
        def __init__(self): self.i = None
        def execute(self, sql, params=()):
            log.append((" ".join(sql.split()), params))
        def fetchall(self):
            return rows_by_call.pop(0) if rows_by_call else []

    cur = Cur()

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cur, commit=lambda: None)

    monkeypatch.setattr(module, "get_db", db)
    return log


def test_stuck_users_lists_trial_signups_with_no_scan(monkeypatch):
    user = {"id": "u1", "email": "dana@cornertavern.com", "name": "Dana Reyes",
            "business_name": "The Corner Tavern", "manager_name": None, "phone": "615-742-9095",
            "created_at": "2026-10-20T00:00:00+00:00", "trial_ends_at": "2026-11-04T00:00:00+00:00",
            "count_day": "sun", "count_time": "after_close",
            "scans": 0, "bottles_in_draft": 0, "orders_sent": 0}
    lead = {"matched_user_id": "u1", "id": "L9", "name": "The Corner Tavern", "loc": "Nashville, TN"}
    log = _fake_db(monkeypatch, crm, [[user], [lead]])
    out = crm.stuck_users(True)
    sql, params = log[0]
    assert "NOT EXISTS (SELECT 1 FROM scan_events" in sql
    assert "'trial'" in sql and "email !~* %s" in sql
    assert params[0] == crm.TEST_EMAIL_PATTERN
    cutoff = datetime.fromisoformat(params[1])
    expect = datetime.now(timezone.utc) - timedelta(hours=crm.STUCK_AFTER_HOURS)
    assert abs((cutoff - expect).total_seconds()) < 60
    u = out["users"][0]
    assert u["counts_on"] == "Sunday after close"
    assert u["stage"] == "not_started"
    assert u["lead"] == {"id": "L9", "name": "The Corner Tavern", "loc": "Nashville, TN"}


def test_stuck_route_is_not_swallowed_by_a_user_id_route():
    paths = [(r.path, sorted(r.methods)) for r in crm.crm_router.routes]
    assert ("/v1/crm/users/stuck", ["GET"]) in paths
    assert not any(p == "/v1/crm/users/{user_id}" and "GET" in m for p, m in paths)


# ---------- welcome email ----------

NOW = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("minutes_ago,email,verdict", [
    (2, "dana@cornertavern.com", "wait"),
    (20, "dana@cornertavern.com", "send"),
    (60 * 48, "dana@cornertavern.com", "send"),
    (60 * 80, "dana@cornertavern.com", "skip"),          # too late to say welcome
    (20, "abc123@privaterelay.appleid.com", "skip"),     # Apple's relay would bounce it
    (20, "", "skip"),
    (20, None, "skip"),
])
def test_who_gets_a_welcome(minutes_ago, email, verdict):
    created = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    assert welcome.decide(created, email, NOW) == verdict


def test_a_naive_or_missing_timestamp():
    assert welcome.decide("2026-10-26T17:30:00", "a@b.com", NOW) == "send"
    assert welcome.decide(None, "a@b.com", NOW) == "skip"
    assert welcome.decide("not a date", "a@b.com", NOW) == "skip"


def test_the_welcome_reads_like_the_owner_wrote_it():
    mail = welcome.compose(name="Dana Reyes", business_name="The Corner Tavern")
    body = mail["body"]
    assert mail["subject"] == "Welcome to 86'd"
    assert body.startswith("Hi Dana,")
    assert "The Corner Tavern" in body
    assert "three bottles" in body
    assert "When do you usually do your count?" in body
    assert welcome.pitch.OWNER_PHONE in body
    assert body.rstrip().endswith(welcome.pitch.SIGNATURE.strip())
    # nothing a drafted outreach email would be checked for
    assert "!" not in body and "%" not in body and "$" not in body


def test_no_name_no_guess():
    assert welcome.compose().get("body").startswith("Hi there,")
    assert welcome.compose(name="dana@x.com").get("body").startswith("Hi there,")
    assert welcome.compose(manager_name="Mike", name="Dana").get("body").startswith("Hi Mike,")


def test_send_welcome_emails_sends_once_and_stamps(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": "new", "email": "dana@cornertavern.com", "name": "Dana", "manager_name": None,
         "business_name": "The Corner Tavern", "created_at": (now - timedelta(minutes=30)).isoformat()},
        {"id": "fresh", "email": "x@y.com", "name": "X", "manager_name": None,
         "business_name": None, "created_at": (now - timedelta(minutes=1)).isoformat()},
        {"id": "relay", "email": "q@privaterelay.appleid.com", "name": "Q", "manager_name": None,
         "business_name": None, "created_at": (now - timedelta(minutes=30)).isoformat()},
        {"id": "review", "email": "appreview444@icloud.com", "name": "R", "manager_name": None,
         "business_name": None, "created_at": (now - timedelta(minutes=30)).isoformat()},
    ]
    log = []

    class Cur:
        def execute(self, sql, params=()): log.append((" ".join(sql.split()), params))
        def fetchall(self): return rows

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    sent = []
    monkeypatch.setattr(main, "get_db", db)
    monkeypatch.setattr(main.mailer, "is_configured", lambda: True)
    monkeypatch.setattr(main.mailer, "send", lambda to, subject, body, **k: sent.append(to))
    main._send_welcome_emails()
    assert sent == ["dana@cornertavern.com"]
    stamped = [p[1] for s, p in log if s.startswith("UPDATE users SET welcome_sent_at")]
    assert stamped == ["new", "relay", "review"]   # the 1-minute-old one waits


def test_a_failed_send_is_not_stamped(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [{"id": "u", "email": "a@b.com", "name": "A", "manager_name": None, "business_name": None,
             "created_at": (now - timedelta(minutes=30)).isoformat()}]
    log = []

    class Cur:
        def execute(self, sql, params=()): log.append(sql)
        def fetchall(self): return rows

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    def boom(*a, **k):
        raise main.mailer.MailFailed("refused")

    monkeypatch.setattr(main, "get_db", db)
    monkeypatch.setattr(main.mailer, "is_configured", lambda: True)
    monkeypatch.setattr(main.mailer, "send", boom)
    main._send_welcome_emails()
    assert not any("welcome_sent_at =" in s for s in log)


def test_no_mailbox_does_nothing(monkeypatch):
    monkeypatch.setattr(main.mailer, "is_configured", lambda: False)
    monkeypatch.setattr(main, "get_db", lambda: (_ for _ in ()).throw(AssertionError("touched the db")))
    main._send_welcome_emails()
