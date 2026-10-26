"""The one welcome email a new customer gets, from the owner's own mailbox.

Pure: who gets it and what it says. main._send_welcome_emails does the
sending (mailer.py, so a reply lands in the owner's inbox, not a no-reply).

Why it exists: five bars signed up in October 2026 and none of them counted,
and the first thing the company said to any of them after "Start trial" was
the trial-ending email on day ten. A sign-up happens on a sales call, away
from the bar, days before the next count; this email asks the one question
that ties the two together — when do you count? — and makes the first step
small: three bottles, about a minute.

Plain text, short, in the owner's voice, signed like every other email he
sends (pitch.SIGNATURE). A message to someone who just made an account is a
relationship email, not outreach: no opt-out footer, no selling.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pitch

# Not the instant the account is made: a "personal" email two seconds after
# tapping Create reads as exactly what it is. A little later reads as a person.
WELCOME_DELAY_MINUTES = 15

# An account older than this that somehow has no welcome stamp (the mailbox
# wasn't configured when it signed up, say) is stamped and skipped. "Welcome!"
# on day six is worse than nothing.
WELCOME_MAX_AGE_HOURS = 72

# Apple's Hide My Email relay only forwards mail from domains registered with
# Apple, so a send from the owner's mailbox would bounce. The app already
# warns those customers in Settings; there's nothing useful to send here.
RELAY_DOMAINS = ("privaterelay.appleid.com",)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def decide(created_at: Optional[str], email: Optional[str], now: datetime) -> str:
    """'send', 'wait' (too soon), or 'skip' (too old, a relay address, or no
    usable address) — skips are stamped so they're never looked at again."""
    created = _parse(created_at)
    if created is None:
        return "skip"
    addr = (email or "").strip().lower()
    if "@" not in addr or addr.endswith(RELAY_DOMAINS):
        return "skip"
    age = now - created
    if age < timedelta(minutes=WELCOME_DELAY_MINUTES):
        return "wait"
    if age > timedelta(hours=WELCOME_MAX_AGE_HOURS):
        return "skip"
    return "send"


def first_name(*names: Optional[str]) -> Optional[str]:
    """The first word of the first name given that looks like a name."""
    for n in names:
        word = (n or "").strip().split(" ")[0] if (n or "").strip() else ""
        if word and word[0].isalpha() and "@" not in word:
            return word[:40]
    return None


def compose(name: Optional[str] = None, manager_name: Optional[str] = None,
            business_name: Optional[str] = None) -> dict:
    """{subject, body} for one new customer."""
    who = first_name(manager_name, name)
    greeting = f"Hi {who}," if who else "Hi there,"
    bar = (business_name or "").strip()
    for_bar = f" for {bar}" if bar else ""
    body = f"""{greeting}

Thanks for setting up 86'd{for_bar}. I'm {pitch.OWNER_NAME}, I built it, and this is my own inbox.

The quickest way to see if it's for you: next time you're behind the bar, open the app and count three bottles. Point the camera at the label, tap, and type how many you have. It takes about a minute, and a full count works exactly the same way.

When do you usually do your count? Reply and tell me, and I'll make sure you're set up before then. If you'd like me on the phone for your first few bottles, call or text me at {pitch.OWNER_PHONE}.

{pitch.SIGNATURE}"""
    return {"subject": "Welcome to 86'd", "body": body}
