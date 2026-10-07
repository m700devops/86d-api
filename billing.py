"""Billing decisions, pure: no Stripe calls, no database.

main.py does the I/O (Checkout, the webhook, /billing/price) and asks these
functions what to do, so every rule is testable on its own (test_billing.py).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

# Checkout's own limit, from Stripe's API reference for
# subscription_data.trial_end: "Has to be at least 48 hours in the future."
# Five minutes of margin for the time between computing it and Stripe
# receiving the request.
TRIAL_END_MIN = timedelta(hours=48, minutes=5)

# Stripe subscription statuses that keep a customer subscribed. past_due is a
# failed renewal Stripe is still retrying (dunning): locking a paying bar out
# of its inventory over a card hiccup is worse than a few days' grace.
# trialing is a subscriber with a card on file whose first charge is still
# ahead (they subscribed during our free trial).
SUBSCRIBED = ("active", "past_due", "trialing")

# Statuses where a live subscription already exists on the Stripe customer, so
# a second Checkout would bill them twice. unpaid is still a subscription
# (Stripe stopped retrying but didn't cancel); it's fixed in the portal.
LIVE = ("active", "trialing", "past_due", "unpaid")


def _as_utc(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def checkout_trial_end(status: Optional[str], trial_ends_at, now: datetime) -> Optional[int]:
    """The `trial_end` to give Checkout, or None to charge on subscribing.

    Someone still inside our free trial who subscribes early isn't billed
    until that trial ends. With less than Checkout's 48-hour minimum left, or
    no trial left at all, they're charged now: the trial is not extended.
    """
    if status != "trial":
        return None
    ends = _as_utc(trial_ends_at)
    if ends is None or ends - now < TRIAL_END_MIN:
        return None
    return int(ends.timestamp())


def app_status(stripe_status: Optional[str], trial_ends_at, now: datetime) -> str:
    """The users.subscription_status a Stripe subscription status means.

    A subscription that ended (or never got paid) while the account still has
    free days left goes back to 'trial', so cancelling during the free trial
    doesn't take the rest of it away.
    """
    if stripe_status in SUBSCRIBED:
        return "active"
    ends = _as_utc(trial_ends_at)
    if ends is not None and ends > now:
        return "trial"
    return "canceled"


def first_charge_date(sub: Optional[dict], status: Optional[str], trial_ends_at,
                      now: datetime) -> Optional[str]:
    """ISO date of the first charge, or None for "today / already billing".

    A subscription Stripe holds is the authority: its trial_end when it's
    trialing. Without one, it's what a Checkout started now would do.
    """
    if sub:
        if sub.get("status") == "trialing" and sub.get("trial_end"):
            return datetime.fromtimestamp(int(sub["trial_end"]), timezone.utc).date().isoformat()
        return None
    ts = checkout_trial_end(status, trial_ends_at, now)
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat() if ts else None


def subscription_id_of(event: dict) -> Optional[str]:
    """The subscription a billing event is about, or None if it isn't."""
    obj = (event.get("data") or {}).get("object") or {}
    kind = event.get("type") or ""
    if kind == "checkout.session.completed":
        sub = obj.get("subscription")
        return sub if isinstance(sub, str) else (sub or {}).get("id")
    if kind.startswith("customer.subscription."):
        return obj.get("id")
    return None
