"""Sending mail from a real mailbox over SMTP.

Spacemail has no API. It doesn't need one: it speaks SMTP, which is what every
mail client on earth uses, and Python has spoken SMTP since 1997. So this is
stdlib only — no new dependency, nothing to keep in version lockstep.

Settings are the ones Spaceship documents for any mail client:
    host  mail.spacemail.com
    port  465 with SSL, or 587 with STARTTLS
    user  the full email address
    pass  the mailbox password

This is deliberately NOT the path order confirmations use. Those go through
Resend in main.py and must keep going through Resend: they are transactional
mail to people who asked for it, and mixing them with cold outreach on one
reputation means a few spam complaints from strangers start bouncing real
customers' receipts. Two paths, two reputations, on purpose.

One mailbox sending individually-addressed plain-text mail is also simply the
right shape for cold outreach — it is what it appears to be, rather than a bulk
blast wearing a personal return address.
"""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Optional

HOST = os.getenv("SPACEMAIL_HOST", "mail.spacemail.com")
PORT = int(os.getenv("SPACEMAIL_PORT", "465"))
USER = os.getenv("SPACEMAIL_USER", "")
PASSWORD = os.getenv("SPACEMAIL_PASSWORD", "")
FROM_NAME = os.getenv("SPACEMAIL_FROM_NAME", "")
TIMEOUT = int(os.getenv("SPACEMAIL_TIMEOUT", "20"))

# Enough to reject nonsense before opening a connection. Deliberately not a
# full RFC 5322 implementation — the mail server is the real authority on
# whether an address is deliverable.
_ADDR_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class MailNotConfigured(RuntimeError):
    """No mailbox credentials. The caller should fall back, not fail."""


class MailFailed(RuntimeError):
    """The send was attempted and didn't work. The message says why."""


def is_configured() -> bool:
    return bool(USER and PASSWORD)


def sender() -> Optional[str]:
    return USER or None


def valid_address(addr: Optional[str]) -> bool:
    return bool(addr and _ADDR_RE.match(addr.strip()))


def send(to: str, subject: str, body: str,
         reply_to: Optional[str] = None) -> dict:
    """Send one plain-text message. Returns {message_id, to, from}.

    Plain text on purpose. A one-to-one note to a bar manager should look like
    a person wrote it; an HTML template with tracking pixels reads as a blast
    and filters accordingly.
    """
    if not is_configured():
        raise MailNotConfigured(
            "No SPACEMAIL_USER / SPACEMAIL_PASSWORD set, so mail can't be sent "
            "from the server.")
    to = (to or "").strip()
    if not valid_address(to):
        raise MailFailed(f"{to!r} doesn't look like an email address.")
    if not (subject or "").strip():
        raise MailFailed("The subject is empty.")
    if not (body or "").strip():
        raise MailFailed("The message is empty.")

    msg = EmailMessage()
    msg["From"] = f"{FROM_NAME} <{USER}>" if FROM_NAME else USER
    msg["To"] = to
    msg["Subject"] = subject.strip()
    msg["Reply-To"] = reply_to or USER
    # Set explicitly rather than left to the server. A message with no Date or
    # Message-ID is one of the cheapest spam signals there is.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(parseaddr(USER)[1].split("@")[-1] or None))
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        if PORT == 465:
            with smtplib.SMTP_SSL(HOST, PORT, timeout=TIMEOUT, context=context) as smtp:
                smtp.login(USER, PASSWORD)
                smtp.send_message(msg)
        else:
            # 587, and anything else, upgrades in place.
            with smtplib.SMTP(HOST, PORT, timeout=TIMEOUT) as smtp:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls(context=context)
                    smtp.ehlo()
                smtp.login(USER, PASSWORD)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        # Never echo the password, and don't make them guess which half is
        # wrong: on Spacemail the username is always the full address.
        raise MailFailed(
            f"{HOST} rejected the login for {USER}. The username must be the "
            "full email address, and the password is the mailbox password.")
    except smtplib.SMTPRecipientsRefused:
        raise MailFailed(f"The mail server wouldn't accept {to} as a recipient.")
    except smtplib.SMTPSenderRefused:
        raise MailFailed(
            f"The mail server wouldn't let {USER} send. Check that the mailbox "
            "exists and the account is active.")
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailFailed(f"Couldn't reach {HOST}:{PORT} — {exc}")

    return {"message_id": msg["Message-ID"], "to": to, "from": USER}
