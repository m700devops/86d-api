"""Every emailed order carries a number, and past orders keep it.

The distributor email used to say only "Order from {bar} — {date}": nothing a
rep could quote back, nothing to put on an invoice, and two orders on one day
read the same. Now each send draws the bar's next number (#1001, #1002, … per
account) from a counter on the users row, committed BEFORE any email goes, and
the orders row keeps it for the history. The allocator and the routes need a
database; the email itself is pure and tested here. The full path — first
number, no number burned when nobody had an address, one number per send when
one distributor bounces, distinct numbers under concurrent sends, search by
"#1002", and a second bar starting its own sequence — was run against a real
Postgres before merging.
"""
import helpers
from models import OrderResponse


def _email(n):
    m = helpers.order_email(
        order_number=n, business_name="Olde Town Tavern", location_name="Main St",
        items=[{"name": "Tito's", "size": "1L", "quantity": 2}, {"name": "Jameson", "quantity": 1.5}],
        sender_name="Laura")
    return m["subject"], m["text"]


def test_the_number_leads_the_subject():
    subject, _ = _email(1042)
    assert subject == "Order #1042 from Olde Town Tavern"


def test_the_body_names_it_and_asks_for_it_on_the_invoice():
    _, body = _email(1042)
    assert body.startswith("Order #1042 from Olde Town Tavern\n")
    assert "Please put order #1042 on the invoice." in body
    assert "- Tito's 1L — 2 btl" in body and "- Jameson — 1.5 btl" in body
    assert "Total: 3.5 btl" in body


def test_without_a_number_it_still_reads_right():
    subject, body = _email(None)
    assert subject == "Order from Olde Town Tavern"
    assert "#" not in body
    assert "Please reference this order on the invoice." in body
    assert "Order sent by Laura, Bar Manager at Olde Town Tavern (Main St)." in body


def test_numbers_start_at_1001_and_format_with_a_hash():
    assert helpers.FIRST_ORDER_NUMBER == 1001
    assert helpers.format_order_number(1001) == "#1001"
    assert helpers.format_order_number(None) is None


def test_the_history_response_keeps_the_number():
    # list_orders runs through response_model=OrderListResponse; a field the
    # model doesn't declare is silently dropped before it reaches the app.
    base = dict(id="o1", session_id="s1", location_id="l1", total_items=1,
                created_at="2026-09-24T10:00:00+00:00")
    assert OrderResponse(**base, order_number=1042).order_number == 1042
    assert OrderResponse(**base).order_number is None     # sent before numbers existed
