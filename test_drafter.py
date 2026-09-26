"""The email drafter: the checker, its one fix round, the way out, threads.

What makes a cold email land, for the person reading it on a phone between
deliveries and for their spam filter, is checked in CODE, because a prompt can
only ask: `pitch.lint` flags template phrases, spam bait, invented figures and
backstory, a second link, shouting, a wall of text and a marketing subject, and
`crm._write_draft` sends a failing draft back ONCE and keeps the better one.
Outreach carries a plain-words way out under the signature (CAN-SPAM; "reply
and say so" instead of "report spam"), a reply to someone who wrote to us does
not. A follow-up titled "Re: <our earlier subject>" threads under that email,
and a "Re:" with no such email behind it comes off (a fake "Re:" is the oldest
trick in cold email). The mailer sends as a named person with no bulk-mail
headers; an opt-out in a subject THEY wrote counts, our own subject quoted
back in "Re: …" never does.
"""
import os
import re
import sys
import types
from contextlib import contextmanager

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402
import inbox  # noqa: E402
import mailer  # noqa: E402
import pitch  # noqa: E402

GOOD = ("Hi Laura,\n\nThanks for the call Tuesday. You said the Sunday count eats most of "
        "the night.\n\nWith 86'd you point your iPhone at each bottle, tap the count, and "
        "every rep gets their order at once.\n\n"
        "https://apps.apple.com/us/app/86d-bar-inventory/id6798359825\n\n"
        "Worth trying on this Sunday's count?\n\nBest,")


# ── the checker ─────────────────────────────────────────────────────────────

def _example():
    """The owner's own email: subject, and the body up to the signature."""
    head, rest = pitch.EXAMPLE_EMAIL.split("\n\n", 1)
    return head.replace("Subject: ", ""), rest[:rest.index(pitch.SIGNATURE)].strip()


def _page_default():
    """The compose box's hand-written starting text (crm.html's mailBody),
    with its placeholders filled the way the page fills them."""
    html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "static", "crm.html"), encoding="utf-8").read()
    subject = re.search(r'const MAIL_SUBJECT = "(.*?)";', html).group(1)
    start = html.index("const mailBody=(contact,venue)=>\n`") + len(
        "const mailBody=(contact,venue)=>\n`")
    body = html[start:html.index("\nBest,\n", start)] + "\nBest,"
    body = re.sub(r"\$\{[^}]*\}", lambda m: pitch.APP_URL if "app_url" in m.group(0)
                  else "Laura", body)
    return subject, body


def test_the_owners_own_email_passes():
    subject, body = _example()
    assert pitch.lint(subject, body, "first") == []


def test_the_compose_box_default_passes_the_same_checks():
    subject, body = _page_default()
    assert "Hi Laura," in body and pitch.APP_URL in body
    assert pitch.lint(subject, body, "first") == []


def test_a_good_draft_is_clean():
    assert pitch.lint("rioja's sunday count", GOOD, "first") == []


@pytest.mark.parametrize("line", [
    "I hope this email finds you well.",
    "I’m reaching out because inventory is a pain.",         # curly apostrophe
    "86'd will revolutionize the way you count.",
    "Just following up on my last note.",
    "Let's touch base next week.",
    "It's a seamless way to handle ordering.",
])
def test_template_phrases_are_caught(line):
    problems = pitch.lint("your order day", f"Hi Laura,\n\n{line}\n\nBest,")
    assert len(problems) == 1 and "reads like a template" in problems[0]


@pytest.mark.parametrize("line", ["Click here to start.", "Act now, it's a limited time thing.",
                                  "No obligation at all."])
def test_spam_bait_is_caught(line):
    assert any("spam-filter bait" in p
               for p in pitch.lint("your order day", f"Hi Laura,\n\n{line}\n\nBest,"))


@pytest.mark.parametrize("line", [
    "Bars like yours are saving hours every week.",
    "Hundreds of bars count this way now.",
    "I spent years behind the bar myself, so I get it.",
    "As a former bartender, I know the 1am count.",
    "Trust me, I've been there.",
])
def test_claims_we_cant_back_are_caught(line):
    assert any("can't back" in p
               for p in pitch.lint("your order day", f"Hi Laura,\n\n{line}\n\nBest,"))


def test_a_figure_is_caught_unless_it_came_from_what_we_know():
    body = "Hi Laura,\n\nYou said about 20% of the pour walks out the door.\n\nBest,"
    assert any("no figures to quote" in p for p in pitch.lint("your pour", body))
    known = "[2026-09-24] call: Laura says 20% of the pour walks out the door"
    assert pitch.lint("your pour", body, known=known) == []
    assert any("no figures" in p for p in pitch.lint(
        "your count", "Hi,\n\nMost bars lose 5 percent to over-pouring.\n\nBest,"))


def test_one_link_only_unless_they_asked_for_more():
    body = GOOD.replace("Worth trying", "More at https://my86d.com\n\nWorth trying")
    assert any("2 links" in p for p in pitch.lint("your order day", body))
    assert pitch.lint("your order day", body, brief="include a link to my website") == []
    assert pitch.lint("Re: your site?", body, "reply") == []        # they asked; answer them


def test_shouting_dashes_markdown_and_a_ps_are_caught():
    def flagged(body):
        return " | ".join(pitch.lint("your order day", f"Hi Laura,\n\n{body}\n\nBest,"))
    assert "exclamation" in flagged("Great talking! Try it! Seriously!")
    assert "exclamation" not in flagged("Great talking today!")
    assert "dashes" in flagged("Count — tap — send — done.")
    assert "markdown" in flagged("**Free month** for you.")
    assert "markdown" in flagged("Why it works:\n• fast\n• easy")
    assert "P.S." in flagged("See you soon.\n\nP.S. I'm in Denver next week.")
    assert "capitals (HUGE)" in flagged("This is HUGE for your bar.")
    assert "capitals" not in flagged("It runs on an IPHONE only.")


def test_a_name_in_capitals_that_came_from_the_map_is_theirs():
    body = "Hi,\n\nThe ROOST crew counts on Sundays, right?\n\nBest,"
    assert any("capitals (ROOST)" in p for p in pitch.lint("your sunday count", body))
    assert pitch.lint("your sunday count", body, known="Venue: THE ROOST (Denver, CO)") == []
    # …but "count" in the notes doesn't license shouting COUNT.
    assert any("capitals (COUNT)" in p for p in pitch.lint(
        "your sunday count", "Hi,\n\nThe COUNT is the worst part.\n\nBest,",
        known="the sunday count"))


def test_length_is_judged_by_kind_and_the_salespersons_word_wins():
    long_body = "Hi Laura,\n\n" + " ".join(["The count takes too long on Sundays."] * 30) + "\n\nBest,"
    assert any("words" in p for p in pitch.lint("your sunday count", long_body, "first"))
    assert any("under 100" in p for p in pitch.lint("your sunday count", long_body, "followup"))
    assert not any("words" in p for p in pitch.lint("your sunday count", long_body, "revision"))
    assert not any("words" in p for p in pitch.lint("your sunday count", long_body, "first",
                                                    brief="make it longer, all the detail"))


@pytest.mark.parametrize("subject,why", [
    ("", "no subject"),
    ("a subject line that just keeps going and going on", "over 7 words"),
    ("your free month", '"free" in the subject'),
    ("try the trial", '"trial" in the subject'),
    ("save time now", '"save" in the subject'),
    ("inventory, done!", '"!" in the subject'),
    ("Rioja's Sunday Count", "Title Case"),
])
def test_subjects_that_read_like_marketing(subject, why):
    assert any(why in p for p in pitch.lint(subject, GOOD, "first"))


def test_subjects_that_read_like_a_person():
    for subject in ("rioja's sunday count", "Laura mentioned the count", "Re: Rioja Sunday Count",
                    "86'd, the app from our call"):
        assert pitch.lint(subject, GOOD, "first") == [], subject
    # A reply keeps THEIR subject, whatever it says.
    assert pitch.lint("Re: FREE TRIAL??? $$", "Hi Jed,\n\nYes, two locations work.\n\nThanks,",
                      "reply") == []


def test_the_fix_request_lists_every_problem():
    ask = pitch.lint_ask(['"seamless" reads like a template', "subject in Title Case"])
    assert "fix ONLY these" in ask and '"seamless"' in ask and "Title Case" in ask


# ── threads: "Re:" only where there is one ─────────────────────────────────

def test_the_thread_base_of_a_subject():
    assert pitch.thread_base("Re: Re:  Sunday count ") == "sunday count"
    assert pitch.thread_base("RE:sunday count") == "sunday count"
    assert pitch.thread_base("sunday count") == ""
    assert pitch.thread_base("Rebecca: the count") == ""
    assert pitch.thread_base("Re:") == ""


def test_a_made_up_re_comes_off():
    assert pitch.honest_re("Re: sunday count", []) == "sunday count"
    assert pitch.honest_re("Re: sunday count", ["your order day"]) == "sunday count"
    assert pitch.honest_re("Re: sunday count", ["Sunday count"]) == "Re: sunday count"
    assert pitch.honest_re("RE: sunday count", ["Re: sunday count"]) == "RE: sunday count"
    assert pitch.honest_re("your order day", []) == "your order day"


# ── the way out, under the signature ───────────────────────────────────────

def test_the_footer_is_the_opt_out_and_the_postal_address_when_set(monkeypatch):
    monkeypatch.setattr(pitch, "POSTAL_ADDRESS", "")
    assert pitch.outreach_footer() == pitch.OPT_OUT_LINE
    assert "reply" in pitch.OPT_OUT_LINE.lower() and "won't email again" in pitch.OPT_OUT_LINE
    monkeypatch.setattr(pitch, "POSTAL_ADDRESS", "123 Main St, Wilmington, NC 28401")
    assert pitch.outreach_footer() == pitch.OPT_OUT_LINE + "\n123 Main St, Wilmington, NC 28401"


def test_signing_a_revision_never_doubles_the_footer(monkeypatch):
    monkeypatch.setattr(pitch, "POSTAL_ADDRESS", "123 Main St, Wilmington, NC 28401")
    on_screen = ("Hi Laura,\n\nShorter now.\n\nBest,\n" + pitch.SIGNATURE + "\n\n"
                 + pitch.outreach_footer())
    signed = pitch.sign(on_screen)
    assert signed == "Hi Laura,\n\nShorter now.\n\nBest,\n" + pitch.SIGNATURE
    assert pitch.OPT_OUT_LINE not in signed and "Main St" not in signed


def test_what_we_know_shows_the_emails_already_sent():
    ctx = pitch.lead_context({"name": "Rioja", "loc": "Denver, CO"}, sent=[
        {"subject": "rioja's sunday count", "body": "Hi Laura,\n\nThe four steps.",
         "sent_at": "2026-09-20T18:00:00+00:00", "replied": False},
        {"subject": "Re: rioja's sunday count", "body": "One more thing.",
         "sent_at": "2026-09-23T18:00:00+00:00", "replied": True,
         "to_addr": "laura@rioja.example"}])
    assert "EMAILS WE ALREADY SENT THEM (2" in ctx and "never repeat" in ctx
    assert "[2026-09-20] Subject: rioja's sunday count — no reply" in ctx
    assert "they REPLIED after this" in ctx and "The four steps." in ctx
    assert "[2026-09-23] to laura@rioja.example Subject: Re: rioja's sunday count" in ctx


# ── the draft writer: one fix round, the better version kept ───────────────

ROW = {"id": "L1", "name": "Rioja", "loc": "Denver, CO", "contact": "Laura (owner)"}
BAD = {"subject": "Rioja's Sunday Count",
       "body": "Hi Laura,\n\nI hope this finds you well. 86'd will streamline your count.\n\nBest,"}
FIXED = {"subject": "rioja's sunday count", "body": GOOD}


def _drafter(monkeypatch, *outs, sent=()):
    calls, queue = [], list(outs)

    def fake(system, user, schema=None, **k):
        calls.append({"purpose": k.get("purpose"), "user": user})
        out = queue.pop(0)
        if isinstance(out, Exception):
            raise out
        return dict(out)

    monkeypatch.setattr(crm, "_claude_json", fake)
    monkeypatch.setattr(crm, "_draft_system", lambda: "SYSTEM")
    monkeypatch.setattr(crm, "_sent_emails_to", lambda lead_id, limit=3: list(sent))
    monkeypatch.setattr(crm, "_draft_context",
                        lambda row, include_log=True, sent=None: "Venue: Rioja (Denver, CO)")
    monkeypatch.setattr(pitch, "POSTAL_ADDRESS", "")
    return calls


def test_a_draft_that_fails_the_checks_goes_back_once(monkeypatch):
    calls = _drafter(monkeypatch, BAD, FIXED)
    out = crm._write_draft(ROW, "Write the email.")
    assert [c["purpose"] for c in calls] == ["draft", "draft-fix"]
    fix = calls[1]["user"]
    assert "hope this finds you" in fix and "streamline" in fix and "Title Case" in fix
    assert "Write the email." in fix and BAD["body"] in fix     # the ask and the draft go back
    assert out["subject"] == "rioja's sunday count" and out["checks"] == []
    assert out["body"].startswith("Hi Laura,\n\nThanks for the call Tuesday.")
    assert out["body"].endswith("Best,\n" + pitch.SIGNATURE + "\n\n" + pitch.OPT_OUT_LINE)


def test_a_clean_draft_costs_one_call(monkeypatch):
    calls = _drafter(monkeypatch, FIXED)
    assert crm._write_draft(ROW, "Write the email.")["checks"] == []
    assert [c["purpose"] for c in calls] == ["draft"]


def test_a_fix_that_makes_it_worse_is_thrown_away(monkeypatch):
    worse = {"subject": "FREE Trial Offer", "body": BAD["body"] + " Act now!! Click here!"}
    _drafter(monkeypatch, BAD, worse)
    out = crm._write_draft(ROW, "Write the email.")
    assert "I hope this finds you well" in out["body"]
    assert out["checks"] == pitch.lint(BAD["subject"], BAD["body"], "first")  # shown on the page


def test_a_failed_fix_keeps_the_first_draft(monkeypatch):
    _drafter(monkeypatch, BAD, RuntimeError("overloaded"))
    out = crm._write_draft(ROW, "Write the email.")
    assert "streamline" in out["body"] and out["checks"]


def test_a_made_up_re_comes_off_a_draft_and_a_real_one_stays(monkeypatch):
    bump = {"subject": "Re: rioja's sunday count", "body": "Hi Laura,\n\nOne thing I missed: "
            "each order goes out with its own number.\n\nWorth a try Sunday?\n\nBest,"}
    _drafter(monkeypatch, bump)
    assert crm._write_draft(ROW, "Follow up.", kind="followup")["subject"] == "rioja's sunday count"
    first = {"subject": "Rioja's sunday count", "body": "…", "sent_at": "2026-09-20",
             "replied": False, "to_addr": "Brent@FBRmgmt.com"}
    _drafter(monkeypatch, bump, sent=[first])
    assert (crm._write_draft({**ROW, "email": "brent@fbrmgmt.com"}, "Follow up.",
                             kind="followup")["subject"] == "Re: rioja's sunday count")
    # Brent left and Jed is the contact now: he never saw that thread.
    _drafter(monkeypatch, bump, sent=[first])
    assert (crm._write_draft({**ROW, "email": "jed@fbrmgmt.com"}, "Follow up.",
                             kind="followup")["subject"] == "rioja's sunday count")


def test_a_reply_keeps_their_subject_and_carries_no_opt_out_line(monkeypatch):
    _drafter(monkeypatch, {"subject": "Re: pricing?", "body": "Hi Jed,\n\nIt's $29.99/month "
                           "after the free first month.\n\nThanks,"})
    out = crm._write_draft(ROW, "Reply.", kind="reply", outreach=False)
    assert out["subject"] == "Re: pricing?"
    assert out["body"].endswith(pitch.SIGNATURE) and pitch.OPT_OUT_LINE not in out["body"]


def _lead_db(monkeypatch):
    row = {k: None for k in crm.LEAD_COLUMNS}
    row.update(id="L1", name="Rioja", loc="Denver, CO", status="contacted",
               notes="[2026-09-24] call · attempt 1: spoke with Laura, the owner")

    class Cur:
        def execute(self, *a): pass
        def fetchone(self): return row

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur())

    monkeypatch.setattr(crm, "get_db", db)


def test_the_email_button_says_what_kind_of_email_it_is(monkeypatch):
    _lead_db(monkeypatch)
    got = []
    monkeypatch.setattr(crm, "_write_draft", lambda row, ask, **k: got.append(k) or {})
    monkeypatch.setattr(crm, "_inbox_mail", lambda mid: {
        "message_id": mid, "from_name": "Jed", "from_addr": "jed@fbrmgmt.com",
        "subject": "q", "body_text": "Two locations?"})
    crm.draft_lead_email("L1", crm.DraftRequest(brief="first email"))
    crm.draft_lead_email("L1", crm.DraftRequest(followup=True))
    crm.draft_lead_email("L1", crm.DraftRequest(brief="shorter", subject="s",
                                                body="Hi\n\nBest,\nS\n\n" + pitch.OPT_OUT_LINE))
    crm.draft_lead_email("L1", crm.DraftRequest(brief="shorter", subject="s", body="Hi, by hand"))
    crm.draft_lead_email("L1", crm.DraftRequest(reply_to="<r@x>"))
    assert [(k["kind"], k["outreach"]) for k in got] == [
        ("first", True), ("followup", True), ("revision", True), ("revision", False),
        ("reply", False)]
    assert got[0]["brief"] == "first email"


# ── sending a follow-up in our own thread ──────────────────────────────────

class _ThreadCur:
    def __init__(self, row):
        self.row, self.sql = row, []

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


def test_a_re_follow_up_finds_the_email_it_answers():
    cur = _ThreadCur({"message_id": "<first@my86d.com>"})
    assert crm._thread_parent(cur, "L1", "RE: Re:  Rioja's sunday count ") == "<first@my86d.com>"
    sql, params = cur.sql[0]
    assert params == ("L1", "rioja's sunday count", "", "")
    # Paired by time, not equality: the scheduled sender stamped them apart.
    assert "abs(extract(epoch" in sql and "< 120" in sql
    # Sending to an address: only a thread that address was part of.
    crm._thread_parent(cur, "L1", "Re: rioja's sunday count", " Jed@FBRmgmt.com ")
    assert cur.sql[1][1] == ("L1", "rioja's sunday count", "jed@fbrmgmt.com", "jed@fbrmgmt.com")


def test_a_fresh_subject_costs_no_query(monkeypatch):
    monkeypatch.setattr(crm, "get_db", lambda: pytest.fail("looked up a thread for a new subject"))
    assert crm._thread_parent_for("L1", "rioja's sunday count") is None


def test_a_failed_lookup_never_stops_the_send(monkeypatch):
    @contextmanager
    def db():
        raise RuntimeError("pool exhausted")
        yield  # pragma: no cover

    monkeypatch.setattr(crm, "get_db", db)
    assert crm._thread_parent_for("L1", "Re: rioja's sunday count") is None


def _send_db(monkeypatch, writes, job=None):
    lead = {"id": "L1", "name": "Rioja", "email": "laura@rioja.example", "status": "contacted",
            "email_date": "2026-09-20", "attempts": 1}
    jobs = [job] if job else []

    class Cur:
        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            writes.append((s, params))
            if "FROM crm_sent_emails e JOIN crm_sent_messages" in s:
                self._row = {"message_id": "<first@my86d.com>"}
            elif s.startswith("UPDATE crm_scheduled_emails SET status = 'sending'"):
                self._row = jobs.pop(0) if jobs else None
            elif s.startswith("SELECT status, email FROM crm_leads"):
                self._row = {"status": "contacted", "email": "laura@rioja.example"}
            elif s.startswith("SELECT * FROM crm_leads") or s.startswith("SELECT * FROM crm_counters"):
                self._row = lead
            else:
                self._row = None

        def fetchone(self):
            return self._row

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)


def test_a_follow_up_goes_out_in_our_thread_and_is_not_an_answered_reply(monkeypatch):
    writes, got = [], {}
    _send_db(monkeypatch, writes)
    monkeypatch.setattr(mailer, "valid_address", lambda a: True)
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, **k: got.update(k) or {
        "message_id": "<second@my86d.com>", "to": to, "saved_to": "Sent"})
    monkeypatch.setattr(crm, "_record_email_sent", lambda *a, **k: "U1")
    monkeypatch.setattr(crm, "_remember_sent", lambda *a, **k: None)
    monkeypatch.setattr(crm, "_lead_row", lambda row: dict(row))
    monkeypatch.setattr(crm, "_counters_row", lambda row: dict(row))
    crm.send_lead_email("L1", crm.OutgoingEmail(subject="Re: rioja's sunday count", body="Hi"),
                        True)
    assert got["in_reply_to"] == "<first@my86d.com>"
    # Our own earlier email is not an inbox message we "answered".
    assert not any(s.startswith("UPDATE crm_inbox SET replied_at") for s, _ in writes)


def test_a_scheduled_follow_up_threads_and_is_stamped_with_one_clock(monkeypatch):
    writes, got, stamps = [], {}, {}
    job = {"id": "J1", "lead_id": "L1", "to_addr": "laura@rioja.example",
           "subject": "Re: rioja's sunday count", "body": "Hi Laura",
           "send_at": crm.now_iso(), "lead_email_at_queue": "laura@rioja.example"}
    _send_db(monkeypatch, writes, job=job)
    ticks = iter(range(100))
    monkeypatch.setattr(crm, "now_iso", lambda: f"2026-09-26T18:00:{next(ticks):02d}+00:00")
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, **k: got.update(k) or {
        "message_id": "<second@my86d.com>"})
    monkeypatch.setattr(crm, "_remember_sent", lambda cur, mid, lead_id, now: stamps.update(msg=now))
    monkeypatch.setattr(crm, "_record_email_sent",
                        lambda cur, lead_id, to, subject, today, now, body=None:
                        stamps.update(email=now))
    assert crm.run_due_emails()["sent"] == 1
    assert got["in_reply_to"] == "<first@my86d.com>"
    assert stamps["msg"] == stamps["email"]           # what _thread_parent pairs them on
    claims = [p for s, p in writes if s.startswith("UPDATE crm_scheduled_emails SET status = 'sending'")]
    assert len({p[0] for p in claims}) == 1           # the claim cutoff never moved mid-run


# ── the mailer: a person's name, no bulk-mail headers ──────────────────────

def test_mail_goes_out_from_a_named_person_with_no_bulk_headers(monkeypatch):
    captured = {}

    class Smtp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, m): captured["m"] = m

    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", Smtp)
    monkeypatch.setattr(mailer, "PORT", 465)
    monkeypatch.setattr(mailer, "USER", "Stephan@my86d.com")
    monkeypatch.setattr(mailer, "PASSWORD", "x")
    monkeypatch.setattr(mailer, "FROM_NAME", "")
    monkeypatch.setattr(mailer, "file_copy", lambda m: ("Sent", None))
    mailer.send("laura@rioja.example", "rioja's sunday count", "Hi Laura")
    m = captured["m"]
    assert m["From"] == "Stephan Khouri <Stephan@my86d.com>"   # the signature's first line
    for header in ("List-Unsubscribe", "Precedence", "List-Id"):
        assert m[header] is None
    monkeypatch.setattr(mailer, "FROM_NAME", "Stephan at 86'd")
    mailer.send("laura@rioja.example", "rioja's sunday count", "Hi Laura")
    assert captured["m"]["From"].startswith("Stephan at 86'd")


def test_a_signature_that_isnt_a_name_leaves_the_bare_address(monkeypatch):
    monkeypatch.setattr(pitch, "SIGNATURE", "— 86'd team\nMy86d.com")
    assert mailer._signature_name() == ""


# ── the inbox: an opt-out in a subject THEY wrote ──────────────────────────

def test_an_unsubscribe_with_no_text_is_read_and_honoured():
    mail = {"from_addr": "laura@rioja.example", "subject": "Unsubscribe", "text": ""}
    assert inbox.worth_reading(mail, "stephan@my86d.com")
    assert inbox.looks_like_opt_out(inbox.opt_out_text(mail))


def test_our_own_subject_quoted_back_is_never_their_opt_out():
    mail = {"from_addr": "laura@rioja.example", "subject": "Re: stop sending orders at 1am",
            "text": "Sounds interesting, call me Tuesday."}
    assert not inbox.looks_like_opt_out(inbox.opt_out_text(mail))
    assert not inbox.worth_reading({**mail, "text": ""}, "stephan@my86d.com")
    # …while their own words in the body still count, whatever the subject.
    assert inbox.looks_like_opt_out(inbox.opt_out_text({**mail, "text": "Please stop emailing me."}))
