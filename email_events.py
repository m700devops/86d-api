"""Resend's delivery webhooks, pure: is this request really from Resend, and
what does the event say about a distributor's address?

Resend counts an order email "sent" the moment it accepts it — what /orders/email
reports — so a distributor address that bounces failed silently every week: the
bar thought the order went, the rep never saw it. Resend tells us afterwards,
through a webhook (signed the Svix way: `svix-id`, `svix-timestamp`,
`svix-signature` headers; HMAC-SHA256 over "id.timestamp.body" with the base64
key after `whsec_`). main.py's route stores the verdict on every distributor
row with that address, and the app shows it on the distributor.
"""
import base64
import hashlib
import hmac
from typing import Optional

TOLERANCE_SECONDS = 5 * 60   # a replayed old delivery is refused


def verify(secret: str, msg_id: str, timestamp: str, signature_header: str, body: bytes,
           now: float) -> bool:
    """True only for a body Resend signed with this webhook's secret, sent
    within TOLERANCE_SECONDS. Any malformed piece is a no, never an error."""
    if not (secret and msg_id and timestamp and signature_header):
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(now - ts) > TOLERANCE_SECONDS:
        return False
    key = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except (ValueError, TypeError):
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key_bytes, signed, hashlib.sha256).digest()).decode()
    # The header can carry several "v1,<sig>" pairs (during a key rotation).
    for part in signature_header.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False


def classify(event: dict) -> tuple[Optional[str], list[str], str]:
    """(kind, addresses, reason) for an event, kind None = nothing to record.

    - email.bounced, permanent → "bounced": the address doesn't take mail.
      A transient bounce (mailbox full, greylisting) changes nothing — Resend
      retries it, and flagging a working address would be crying wolf.
    - email.complained → "complained": the rep marked an order as spam, which
      also hurts every bar's deliverability.
    - email.delivered → "delivered": the address works again; clears a flag
      (a rep who fixed their mailbox, or a bar that corrected a typo).
    """
    kind = (event or {}).get("type")
    data = (event or {}).get("data") or {}
    to = data.get("to") or []
    if isinstance(to, str):
        to = [to]
    addresses = sorted({a.strip().lower() for a in to if isinstance(a, str) and "@" in a})
    if kind == "email.bounced":
        bounce = data.get("bounce") or {}
        if str(bounce.get("type") or "").lower() in ("transient", "soft"):
            return None, addresses, ""
        reason = " ".join(str(bounce.get("message") or bounce.get("subType") or "").split())[:300]
        return "bounced", addresses, reason or "The address rejected the email."
    if kind == "email.complained":
        return "complained", addresses, "The recipient marked the order email as spam."
    if kind == "email.delivered":
        return "delivered", addresses, ""
    return None, addresses, ""


def bare_address(raw: str) -> str:
    """"Metro Beverage <Orders@Metro.com>" -> "orders@metro.com"."""
    s = (raw or "").strip()
    if "<" in s and s.endswith(">"):
        s = s[s.rindex("<") + 1:-1]
    return s.strip().lower()
