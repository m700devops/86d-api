"""The distributor order email, laid out like the order card on the landing page.

Subject "Order #1042 from <bar>"; under it the bar's account number with this
distributor and the date it wants delivery; one line per item with its unit;
"Order sent by Dana Reyes, Bar Manager at <bar>. Please put order #1042 on the
invoice."; plain text plus a light HTML card with the same words. From shows
the bar ("<bar> via 86'd"), To shows the distributor, and replies go to the
bar's chosen reply-to, else its login email.

The account number is saved once per bar per distributor (Settings, or typed
on the order screen) and kept until edited; delivery days are saved once per
distributor and the app fills in the next one.
"""
import sys
import types
from contextlib import contextmanager
from datetime import date

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

ITEMS = [{"name": "Tito's", "size": "1L", "quantity": 24, "unit": "case", "case_size": 12},
         {"name": "Green Chartreuse", "quantity": 1}]


def _mail(**kw):
    args = dict(order_number=1042, business_name="Marlins Seafood and Grille", location_name=None,
                items=ITEMS, sender_name="Dana Reyes", account_number="4471",
                deliver_by=date(2026, 10, 9))
    args.update(kw)
    return helpers.order_email(**args)


# ── the email ────────────────────────────────────────────────────────────────

def test_the_email_reads_like_the_card():
    m = _mail()
    assert m["subject"] == "Order #1042 from Marlins Seafood and Grille"
    assert m["text"] == (
        "Order #1042 from Marlins Seafood and Grille\n"
        "Acct #4471\n"
        "Deliver by Fri, Oct 9\n"
        "\n"
        "- Tito's 1L — 2 cases (24 btl)\n"
        "- Green Chartreuse — 1 btl\n"
        "\n"
        "Total: 2 cases + 1 btl (25 btl)\n"
        "\n"
        "Order sent by Dana Reyes, Bar Manager at Marlins Seafood and Grille. "
        "Please put order #1042 on the invoice.\n"
        "\n"
        "Sent with 86'd bar inventory")


def test_no_account_or_date_means_no_line_for_them():
    text = _mail(account_number=None, deliver_by=None)["text"]
    assert "Acct" not in text and "Deliver by" not in text
    assert text.startswith("Order #1042 from Marlins Seafood and Grille\n\n- Tito's")
    assert "Acct" not in _mail(account_number="  ")["text"]


def test_the_title_and_the_bar_location():
    text = _mail(sender_title="Owner", location_name="Harbor St")["text"]
    assert "Order sent by Dana Reyes, Owner at Marlins Seafood and Grille (Harbor St)." in text
    # No person on file: the bar sends it, never "Marlins, Bar Manager at Marlins".
    assert "Order sent by Marlins Seafood and Grille." in _mail(sender_name=None)["text"]
    assert "Order sent by Marlins Seafood and Grille." in _mail(sender_name="Marlins Seafood and Grille")["text"]


def test_the_html_card_says_the_same_and_escapes_what_a_bar_typed():
    html = _mail()["html"]
    for part in ("Order #1042 from Marlins Seafood and Grille", "Acct #4471", "Deliver by Fri, Oct 9",
                 "2 cases (24 btl)", "Total", "Please put order #1042 on the invoice."):
        assert part in html
    evil = _mail(business_name="<script>x</script> & Co",
                 items=[{"name": '<img src=x onerror="y">', "quantity": 1}])["html"]
    assert "<script>" not in evil and "<img" not in evil
    assert "&lt;script&gt;" in evil and "&amp; Co" in evil


def test_account_numbers_and_delivery_days_are_cleaned():
    assert helpers.clean_account_number(" Acct # 4471 ") == "4471"
    assert helpers.clean_account_number("Account number: A-99") == "A-99"
    assert helpers.clean_account_number("#88\n12") == "88 12"
    assert helpers.clean_account_number("") is None
    assert helpers.clean_delivery_days("Thursday, mon") == "mon,thu"
    assert helpers.clean_delivery_days("tue tue") == "tue"
    assert helpers.clean_delivery_days("") is None
    with pytest.raises(ValueError):
        helpers.clean_delivery_days("funday")
    assert helpers.delivery_label(date(2026, 10, 10)) == "Sat, Oct 10"


# ── who it's from, who it's to, where replies go ─────────────────────────────

def test_the_sender_is_the_bar_on_86ds_address(monkeypatch):
    monkeypatch.setenv("ORDER_EMAIL_FROM", "86'd Orders <orders@my86d.com>")
    assert main._order_sender("Marlins Seafood and Grille") == \
        "Marlins Seafood and Grille via 86'd <orders@my86d.com>"
    # A comma would split the address list: it's quoted.
    assert main._order_sender("Smith, Jones & Co") == '"Smith, Jones & Co via 86\'d" <orders@my86d.com>'
    # A name can't smuggle a header or a second address in.
    sneaky = main._order_sender("Bar\r\nBcc: x@evil.com")
    assert "\n" not in sneaky and sneaky.endswith("<orders@my86d.com>")
    monkeypatch.delenv("ORDER_EMAIL_FROM")
    assert main._order_sender("Marlins").endswith("<onboarding@resend.dev>")
    assert main._order_sender("") == "86'd Orders <onboarding@resend.dev>"


def test_reply_to_must_be_one_plain_address():
    assert main._clean_reply_to(" orders@marlins.com ") == "orders@marlins.com"
    assert main._clean_reply_to("") is None
    for bad in ("not an email", "a@b.com, c@d.com", "a@b.com\r\nBcc: c@d.com", "<a@b.com>"):
        with pytest.raises(HTTPException) as e:
            main._clean_reply_to(bad)
        assert e.value.status_code == 422


def test_resend_gets_the_card_the_names_and_the_reply_to(monkeypatch):
    monkeypatch.setenv("ORDER_EMAIL_FROM", "orders@my86d.com")
    seen = {}

    class R:
        status_code = 200

    monkeypatch.setattr(main.httpx, "post", lambda url, headers, json, timeout: (seen.update(json) or R()))
    ok, _ = main._send_via_resend("k", "rep@metro.com", "S", "T", reply_to="dana@marlins.com",
                                  bcc="dana@marlins.com", html="<p>H</p>",
                                  from_name="Marlins", to_name="Metro Beverage")
    assert ok
    assert seen["from"] == "Marlins via 86'd <orders@my86d.com>"
    assert seen["to"] == ["Metro Beverage <rep@metro.com>"]
    assert seen["html"] == "<p>H</p>" and seen["text"] == "T"
    assert seen["reply_to"] == "dana@marlins.com" and seen["bcc"] == ["dana@marlins.com"]


# ── the send route ───────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, log, rows):
        self.log, self.rows, self.rowcount = log, rows, 1

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        self.log.append((flat, params))
        self._out = next((v for k, v in self.rows.items() if k in flat), [])

    def fetchone(self):
        return self._out[0] if self._out else None

    def fetchall(self):
        return list(self._out)


def _send(monkeypatch, user, saved=None, typed=None, deliver_by=None):
    log, sent = [], []
    rows = {
        "FROM locations": [{"id": "loc"}],
        "FROM users": [user],
        "FROM distributors": [{"id": "d1", "name": "Metro Beverage", "email": "rep@metro.com"}],
        "SELECT distributor_id, account_number FROM location_distributor_accounts":
            [{"distributor_id": "d1", "account_number": n} for n in [saved] if n],
    }

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: _Cur(log, rows), commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setattr(main, "_send_via_resend", lambda key, to, subject, body, **kw: (
        sent.append(dict(to=to, subject=subject, body=body, **kw)) or (True, None)))
    monkeypatch.setattr(main, "_draw_order_number", lambda uid: 1042)
    monkeypatch.setattr(main, "_save_order_history", lambda *a, **k: "order-1")
    order = {"distributor_id": "d1", "items": [{"name": "Tito's", "quantity": 2}]}
    if typed is not None:
        order["account_number"] = typed
    if deliver_by:
        order["deliver_by"] = deliver_by
    req = main.SendOrderEmailsRequest(location_id="loc", location_name="Marlins Seafood and Grille",
                                      orders=[order])
    main.send_order_emails(req, "u1")
    return sent, log


USER = {"name": "Dana", "email": "dana@icloud.com", "business_name": "Marlins Seafood and Grille",
        "manager_name": "Dana Reyes", "title": None, "order_reply_to": None}


def test_the_route_sends_the_card_from_the_bar_with_the_saved_account(monkeypatch):
    sent, _ = _send(monkeypatch, USER, saved="4471", deliver_by="2026-10-09")
    m = sent[0]
    assert m["subject"] == "Order #1042 from Marlins Seafood and Grille"
    assert "Acct #4471\nDeliver by Fri, Oct 9\n" in m["body"]
    assert "Order sent by Dana Reyes, Bar Manager at Marlins Seafood and Grille." in m["body"]
    assert m["html"] and m["from_name"] == "Marlins Seafood and Grille" and m["to_name"] == "Metro Beverage"
    assert m["reply_to"] == "dana@icloud.com"


def test_an_account_typed_on_the_order_screen_is_saved_for_good(monkeypatch):
    sent, log = _send(monkeypatch, USER, typed=" #5520 ")
    saves = [p for s, p in log if s.startswith("INSERT INTO location_distributor_accounts")]
    assert saves and saves[0][:3] == ("loc", "d1", "5520")


def test_replies_go_to_the_reply_to_the_bar_chose(monkeypatch):
    sent, _ = _send(monkeypatch, dict(USER, order_reply_to="orders@marlins.com", title="Owner"))
    assert sent[0]["reply_to"] == "orders@marlins.com" and sent[0]["bcc"] == "orders@marlins.com"
    assert "Dana Reyes, Owner at" in sent[0]["body"]


def test_setting_and_clearing_an_account_number(monkeypatch):
    log = []
    rows = {"FROM locations": [{"id": "loc"}], "FROM distributors": [{"id": "d1"}]}

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: _Cur(log, rows), commit=lambda: None)

    monkeypatch.setattr(main, "get_db", db)
    out = main.set_distributor_account("loc", "d1", main.DistributorAccountUpdate(account_number="Acct 4471"), "u1")
    assert out["account_number"] == "4471"
    assert any(s.startswith("INSERT INTO location_distributor_accounts") for s, _ in log)
    log.clear()
    out = main.set_distributor_account("loc", "d1", main.DistributorAccountUpdate(account_number=""), "u1")
    assert out["account_number"] is None
    assert any(s.startswith("DELETE FROM location_distributor_accounts") for s, _ in log)


def test_bad_delivery_days_are_a_422():
    with pytest.raises(HTTPException) as e:
        main._delivery_days_or_422("someday")
    assert e.value.status_code == 422 and e.value.detail["error"] == "invalid_delivery_days"
