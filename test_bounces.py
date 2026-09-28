"""An address that bounces comes off the lead and is never emailed again.

The inbox reader skipped every mailer-daemon message as a robot, so a dead
address stayed on the lead and was emailed again — and repeated hard bounces
are one of the fastest ways for a small sender to get filtered.
"""
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402
import inbox  # noqa: E402

DSN = b"""From: Mail Delivery Subsystem <mailer-daemon@mail.spacemail.com>
To: stephan@my86d.com
Subject: Undelivered Mail Returned to Sender
Message-ID: <bounce1@spacemail>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain

This is the mail system. Your message could not be delivered.

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; mail.spacemail.com

Final-Recipient: rfc822; info@hideawayodenton.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 <info@hideawayodenton.com>: Recipient address rejected

--B--
"""
SOFT = DSN.replace(b"Status: 5.1.1", b"Status: 4.2.2").replace(
    b"550 5.1.1 <info@hideawayodenton.com>: Recipient address rejected", b"452 mailbox full")
PLAIN = b"""From: postmaster@outlook.com
To: stephan@my86d.com
Subject: Undeliverable: the end-of-night count
Message-ID: <bounce2@outlook>
Content-Type: text/plain

Delivery has failed to these recipients or groups:

mike@thehideaway.example
The email address you entered couldn't be found. 550 5.1.10 RESOLVER.ADR.RecipientNotFound

From: Stephan Khouri <stephan@my86d.com>
"""
REPLY = b"""From: Bill <bill@libbeys.example>
To: stephan@my86d.com
Subject: Re: the end-of-night count
Message-ID: <r1@libbeys>
Content-Type: text/plain

We're happy with MarginEdge, thanks. The address info@old.example doesn't exist any more.
"""


def test_a_standard_bounce_names_the_address_and_is_permanent():
    b = inbox.parse(DSN)["bounce"]
    assert b["addresses"] == ["info@hideawayodenton.com"] and b["permanent"]
    assert "Recipient address rejected" in b["reason"]


def test_mailbox_full_is_not_permanent():
    assert inbox.parse(SOFT)["bounce"]["permanent"] is False


def test_a_plain_words_bounce_is_read_too():
    b = inbox.parse(PLAIN)["bounce"]
    assert "mike@thehideaway.example" in b["addresses"] and b["permanent"]


def test_a_person_saying_an_address_is_dead_is_not_a_bounce():
    assert inbox.parse(REPLY)["bounce"] is None


def _db(sent, leads):
    log = []

    class Cur:
        def execute(self, sql, params=None):
            log.append((" ".join(sql.split()), params))
            self.sql = sql

        def fetchall(self):
            if "FROM crm_sent_emails" in self.sql:
                return [{"addr": a} for a in sent]
            if "FROM crm_leads" in self.sql:
                return [dict(l) for l in leads]
            return []

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    return db, log


def test_only_an_address_we_sent_to_comes_off_and_the_lead_stays(monkeypatch):
    db, log = _db(sent=["info@hideawayodenton.com"],
                  leads=[{"id": "H1", "name": "The Hideaway", "email": "info@hideawayodenton.com"}])
    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_snapshot", lambda cur, lead, action, counters_spent=1: "U1")
    out = crm._record_bounce({}, {"addresses": ["info@hideawayodenton.com", "stephan@my86d.com"],
                                  "permanent": True, "reason": "550 5.1.1 rejected"})
    assert out["bounced"] == ["info@hideawayodenton.com"]
    assert [a["name"] for a in out["applied"]] == ["The Hideaway"]
    sqls = [s for s, _ in log]
    assert any(s.startswith("INSERT INTO crm_suppressions") for s in sqls)
    update = next(p for s, p in log if s.startswith("UPDATE crm_leads SET email = NULL"))
    assert "bounced" in update[1] and update[-1] == "H1"
    assert not any("DELETE" in s for s in sqls)                  # the bar stays


def test_an_address_we_never_emailed_changes_nothing(monkeypatch):
    db, log = _db(sent=[], leads=[])
    monkeypatch.setattr(crm, "get_db", db)
    out = crm._record_bounce({}, {"addresses": ["someone@else.example"], "permanent": True,
                                  "reason": ""})
    assert out["applied"] == [] and not any("INSERT" in s for s, _ in log)
