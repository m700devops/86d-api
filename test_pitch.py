"""The email drafter's master sheet, and every CRM AI on one strong model.

The drafts were bland: the rules capped them at four sentences with no list,
the model knew no price, trial or phone number, and it ran on Haiku. pitch.py
now carries the owner's facts, his own example email and a style guide, and
every AI in the CRM runs on CRM_AI_MODEL (Opus 5) at CRM_AI_EFFORT (medium).
"""
import sys
import types
from contextlib import contextmanager

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402
import pitch  # noqa: E402


# ── the master sheet ────────────────────────────────────────────────────────

def test_the_sheet_carries_the_owners_facts():
    sheet = pitch.master_sheet()
    for fact in ("Stephan", "910-335-2760", "$49.99/month", "15 days free",
                 "No credit card", pitch.APP_URL, "iOS only", "restaurant's name",
                 "bar manager's name"):
        assert fact in sheet, fact
    assert pitch.APP_URL == "https://apps.apple.com/us/app/86d-bar-inventory/id6798359825"


def test_the_sheet_claims_the_order_number_the_email_really_carries():
    # Kept off the sheet until the distributor email carried one; now it does,
    # so the sheet says so — in the same shape the email uses.
    import helpers
    sheet = pitch.master_sheet()
    assert "order number (#1001, #1002" in sheet
    assert "order number" in pitch.EXAMPLE_EMAIL.lower()
    m = helpers.order_email(order_number=1001, business_name="Rioja", location_name=None,
                            items=[{"name": "x", "quantity": 1}], sender_name="Ed")
    subject, body = m["subject"], m["text"]
    assert "#1001" in subject and "put order #1001 on the invoice" in body


def test_the_prompt_has_the_sheet_the_example_and_the_rules():
    p = pitch.system_prompt(pitch.lead_context({"name": "Rioja", "loc": "Denver, CO"}))
    assert "MASTER SHEET" in p and "Cut bar inventory to 15 minutes" in p
    assert "NEVER state a product fact" in p and "who owns 86'd" in p
    assert "RELEVANCE FIRST" in p and "ONE EASY ASK" in p      # the human part
    assert "App Store link, on its own line" in p and "ONLY link in the body" in p
    assert "Never give him a backstory" in p                  # nothing invented about him


def test_what_we_know_is_only_whats_on_file():
    ctx = pitch.lead_context(
        {"name": "Workhorse Bar", "loc": "Austin, TX", "contact": "Lesley",
         "opener": "a big tap list"},
        [{"text": "open since 2014", "source": "their website"}],
        ["busy Sundays"], "[2026-09-25] call: spoke with Lesley")
    assert "Decision maker — write to them: Lesley" in ctx
    assert "open since 2014 (from their website)" in ctx
    assert "busy Sundays" in ctx and "spoke with Lesley" in ctx
    thin = pitch.lead_context({"name": "Nowhere Bar"})
    assert "Nothing else is known" in thin


# ── the drafter uses it, on the strong model ────────────────────────────────

def _lead(**kw):
    row = {k: None for k in crm.LEAD_COLUMNS}
    row.update(id="L1", name="Rioja", loc="Denver, CO", status="new",
               notes="[2026-09-24] call · attempt 1: spoke with Alex, the GM",
               venue_facts=None, call_brief='["tapas and a long wine list"]')
    row.update(kw)
    return row


@pytest.fixture
def drafted(monkeypatch):
    sent = {}

    class _Cur:
        def execute(self, *a): pass
        def fetchone(self): return _lead()

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: _Cur())

    def fake(system, ask, schema, model=None, max_tokens=0, timeout=0, **kw):
        sent.update(system=system, ask=ask, schema=schema, max_tokens=max_tokens)
        return {"subject": "Rioja's Sunday count", "body": "Hi Alex, ..."}

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_claude_json", fake)
    monkeypatch.setattr(crm, "_knowledge", lambda *a, **k: "")
    monkeypatch.setattr(crm, "_winning_emails", lambda *a, **k: [])
    return sent


def test_a_first_email_reads_the_log_and_the_prep_sheet(drafted):
    out = crm.draft_lead_email("L1", crm.DraftRequest(brief="first email, mention the free month"))
    assert out["subject"] == "Rioja's Sunday count"
    # The bar goes in the message; the system prompt is the cached, same-for-
    # everyone part (sheet, brain, examples, style).
    assert "spoke with Alex" in drafted["ask"]             # the log, for a first email too
    assert "tapas and a long wine list" in drafted["ask"]
    assert "MASTER SHEET" in drafted["system"]
    for detail in ("spoke with Alex", "tapas", "Denver"):
        assert detail not in drafted["system"], detail
    assert drafted["schema"] == pitch.SCHEMA
    assert drafted["max_tokens"] >= crm.AI_MIN_TOKENS


def test_a_follow_up_carries_the_log_once(drafted):
    crm.draft_lead_email("L1", crm.DraftRequest(followup=True))
    assert drafted["ask"].count("spoke with Alex") == 1   # in the follow-up ask, not twice
    assert "spoke with Alex" not in drafted["system"]


# ── every AI on one model and effort ────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def _record(monkeypatch, *responses):
    import httpx
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"json": json, "timeout": timeout})
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_the_default_is_opus_5_at_medium():
    assert crm.AI_MODEL == "claude-opus-5" and crm.AI_EFFORT == "medium"
    assert crm.ASSIST_MODEL == crm.AI_MODEL == crm.DEBRIEF_MODEL


def test_the_old_helper_speaks_to_current_models(monkeypatch):
    calls = _record(monkeypatch, _Resp(200, {"stop_reason": "end_turn", "content": [
        {"type": "thinking", "thinking": "..."},
        {"type": "text", "text": 'Sure: {"points": ["a"]}'}]}))
    assert crm._ask_claude("sys", "msg", max_tokens=350, temperature=1) == {"points": ["a"]}
    body = calls[0]["json"]
    assert body["model"] == "claude-opus-5"
    assert body["output_config"] == {"effort": "medium"}
    assert "temperature" not in body                                # rejected by Opus 5
    assert [m["role"] for m in body["messages"]] == ["user"]         # and so is a prefill
    assert body["max_tokens"] >= crm.AI_MIN_TOKENS                   # room to think
    assert calls[0]["timeout"] >= crm.AI_MIN_TIMEOUT


def test_a_400_on_effort_is_retried_without_it(monkeypatch):
    calls = _record(monkeypatch, _Resp(400, {"error": "effort"}),
                    _Resp(200, {"stop_reason": "end_turn", "content": [
                        {"type": "text", "text": '{"ok": true}'}]}))
    assert crm._ask_claude("sys", "msg") == {"ok": True}
    assert "output_config" not in calls[1]["json"]


def test_the_structured_helper_sends_effort_too(monkeypatch):
    calls = _record(monkeypatch, _Resp(200, {"stop_reason": "end_turn", "content": [
        {"type": "text", "text": '{"subject": "s", "body": "b"}'}]}))
    crm._claude_json("sys", "msg", pitch.SCHEMA)
    oc = calls[0]["json"]["output_config"]
    assert oc["effort"] == "medium" and oc["format"]["schema"] == pitch.SCHEMA


# ── reply mode, and learning from what got replies ──────────────────────────

def test_a_reply_is_drafted_from_the_email_they_sent(drafted, monkeypatch):
    monkeypatch.setattr(crm, "_inbox_mail", lambda mid: {
        "message_id": mid, "from_name": "Jed", "from_addr": "jed@fbrmgmt.com",
        "subject": "following up", "body_text": "Does it work with two locations?"})
    crm.draft_lead_email("L1", crm.DraftRequest(reply_to="<reply-1@fbrmgmt.com>"))
    assert "Does it work with two locations?" in drafted["ask"]
    assert 'Subject: "Re: following up"' in drafted["ask"]


def test_a_reply_to_an_email_that_is_gone_is_a_404(drafted, monkeypatch):
    import pytest
    from fastapi import HTTPException
    monkeypatch.setattr(crm, "_inbox_mail", lambda mid: None)
    with pytest.raises(HTTPException) as e:
        crm.draft_lead_email("L1", crm.DraftRequest(reply_to="<gone@x>"))
    assert e.value.status_code == 404


def test_emails_that_got_replies_are_shown_as_examples_not_to_copy():
    p = pitch.system_prompt("OWNER'S STANDING INSTRUCTIONS ...\nDemos Tue/Thu.",
                            [{"subject": "Workhorse's Sunday count", "body": "Hi Brent, ..."}])
    assert "Demos Tue/Thu." in p
    assert "EMAILS OF OURS THAT GOT A REPLY" in p and "Workhorse's Sunday count" in p
    assert "never reuse a venue, name or detail" in p
    plain = pitch.system_prompt()
    assert "GOT A REPLY" not in plain and "WHAT THE OWNER SAYS" not in plain


def test_the_system_prompt_is_the_same_for_every_bar():
    # What makes it cacheable: nothing about one bar is in it.
    assert pitch.system_prompt("k", []) == pitch.system_prompt("k", [])
    assert "WHAT WE KNOW ABOUT THIS BAR" not in pitch.system_prompt("k", [])
    assert pitch.user_prompt("Venue: Rioja", "Write it").startswith("=== WHAT WE KNOW ABOUT THIS BAR")


# ── the master sheet, as an owner would brief a rep ─────────────────────────

def test_the_sheet_says_what_the_app_does_not_do():
    sheet = pitch.master_sheet()
    for limit in ("No Android version", "doesn't connect to a POS",
                  "doesn't measure how full a bottle is",
                  "doesn't order through distributor websites",
                  "No customer numbers, testimonials"):
        assert limit in sheet, limit


def test_the_sheet_briefs_who_its_for_the_pains_and_the_pushback():
    sheet = pitch.master_sheet()
    for part in ("WHO IT'S FOR", "PAINS TO ASK ABOUT", "HONEST ANSWERS TO THE USUAL PUSHBACK",
                 "WHAT WE ASK FOR", '"My staff use Android."', "Cancel any time, from the app"):
        assert part in sheet, part
    assert "as questions, never as statements about their bar" in sheet


def test_the_angle_follows_the_state_tip_credit():
    assert "no tip credit" in pitch.state_angle("Reno, NV")
    assert "no tip credit" in pitch.state_angle("Los Angeles, CA")
    assert "allows a tip credit" in pitch.state_angle("Austin, TX")
    assert pitch.state_angle("Austin") is None and pitch.state_angle(None) is None
    ctx = pitch.lead_context({"name": "Shiner's Saloon", "loc": "Austin, TX"})
    assert "Angle for this state: TX allows a tip credit" in ctx


# ── every draft ends with the owner's signature, and goes to the decision maker ─

SIG = "Stephan Khouri\nOwner of 86'd Bar inventory\nWebsite: My86d.com"


def test_the_signature_is_the_owners_exact_words():
    assert pitch.SIGNATURE == SIG
    assert pitch.EXAMPLE_EMAIL.endswith("Best,\n" + SIG)
    assert "Stephan Khouri / Owner of 86'd Bar inventory / Website: My86d.com" in pitch.master_sheet()


def test_sign_replaces_whatever_sign_off_the_model_wrote():
    for body in ("Hi Laura,\n\nBody.\n\nBest,\nStephan\nOwner of 86'd",
                 "Hi Laura,\n\nBody.\n\nBest,\n— Stephan\n910-335-2760\nmy86d.com",
                 "Hi Laura,\n\nBody.\n\nBest,\n" + SIG,
                 "Hi Laura,\n\nBody.\n\nBest,\n" + SIG + "\n\n"):
        assert pitch.sign(body) == "Hi Laura,\n\nBody.\n\nBest,\n" + SIG, body


def test_signing_twice_changes_nothing():
    once = pitch.sign("Hi Laura,\n\nBody.\n\nThanks,")
    assert pitch.sign(once) == once and once.count("Stephan Khouri") == 1


def test_no_closing_word_still_ends_with_the_signature():
    assert pitch.sign("Hi Laura,\n\nWorth a look before your next order day?") == (
        "Hi Laura,\n\nWorth a look before your next order day?\n\n" + SIG)


def test_a_ps_moves_above_the_sign_off():
    out = pitch.sign("Hi Laura,\n\nBody.\n\nThanks,\nStephan\n\nP.S. Hope the cat's ok.")
    assert out == "Hi Laura,\n\nBody.\n\nP.S. Hope the cat's ok.\n\nThanks,\n" + SIG


def test_the_body_keeps_a_phone_line_that_is_not_the_sign_off():
    out = pitch.sign("Hi Laura,\n\nCall me directly at 910-335-2760.\n\nBest,")
    assert "Call me directly at 910-335-2760." in out


def test_who_decides_comes_before_who_picked_up():
    lead = {"name": "Barrel House", "contact": "Laura Keene (owner)",
            "notes": "[2026-09-20] call — Spoke to: Jake (bartender) · Your notes: Laura orders"}
    assert pitch.decision_maker(lead) == ("Laura Keene (owner)", "")
    ctx = pitch.lead_context(lead)
    assert "Decision maker — write to them: Laura Keene (owner)" in ctx
    assert "Spoke to on the phone (not the decision maker): Jake (bartender)" in ctx
    site = pitch.decision_maker({"manager_name": "Ed Park", "manager_role": "GM"})
    assert site == ("Ed Park", " (GM, per their website — may have moved on)")
    assert pitch.decision_maker({}) == (None, "")


def test_first_names_skip_roles():
    assert pitch.first_name("Laura Keene (owner)") == "Laura"
    assert pitch.first_name("GM Brent") == "Brent"
    assert pitch.first_name("the owner") is None and pitch.first_name("bar manager") is None


def test_the_greeting_goes_to_the_decision_maker():
    assert pitch.address_to("Hi Jake,\n\nBody.", "Laura") == "Hi Laura,\n\nBody."
    assert pitch.address_to("Hi there,\n\nBody.", "Laura") == "Hi Laura,\n\nBody."
    assert pitch.address_to("Laura — quick one.\n\nBody.", "Laura") == "Laura — quick one.\n\nBody."
    assert pitch.address_to("Your Sunday count.\n\nBody.", "Laura").startswith("Hi Laura,\n\nYour")
    assert pitch.address_to("Hi there,\n\nBody.", None) == "Hi there,\n\nBody."


def test_the_style_writes_to_the_decision_maker_and_never_signs():
    p = pitch.system_prompt()
    assert "Write to the DECISION MAKER" in p and "A REPLY is the exception" in p
    assert "never write one" in p and "No P.S." in p


def test_a_first_email_is_signed_and_greets_the_decision_maker(drafted, monkeypatch):
    import types as _t
    monkeypatch.setattr(crm, "_claude_json", lambda *a, **k: {
        "subject": "Rioja's Sunday count", "body": "Hi Jake,\n\nBody.\n\nThanks,\nStephan"})

    class _Cur:
        def execute(self, *a): pass
        def fetchone(self): return _lead(contact="Alex Ruiz (GM)")

    monkeypatch.setattr(crm, "get_db", contextmanager(lambda: (yield _t.SimpleNamespace(cursor=lambda: _Cur())))) 
    out = crm.draft_lead_email("L1", crm.DraftRequest(brief="first email"))
    # Outreach: the opt-out line goes under the signature.
    assert out["body"] == "Hi Alex,\n\nBody.\n\nThanks,\n" + SIG + "\n\n" + pitch.OPT_OUT_LINE


def test_a_reply_answers_whoever_wrote(drafted, monkeypatch):
    monkeypatch.setattr(crm, "_inbox_mail", lambda mid: {
        "message_id": mid, "from_name": "Jed", "from_addr": "jed@fbrmgmt.com",
        "subject": "q", "body_text": "Two locations?"})
    monkeypatch.setattr(crm, "_claude_json", lambda *a, **k: {"subject": "Re: q", "body": "Hi Jed,\n\nYes."})
    out = crm.draft_lead_email("L1", crm.DraftRequest(reply_to="<r@x>"))
    assert out["body"] == "Hi Jed,\n\nYes.\n\n" + SIG          # not re-addressed to the lead's contact


def test_a_revision_keeps_its_greeting(drafted, monkeypatch):
    monkeypatch.setattr(crm, "_claude_json", lambda *a, **k: {"subject": "s", "body": "Hi Jed,\n\nShorter."})
    out = crm.draft_lead_email("L1", crm.DraftRequest(brief="shorter", subject="s", body="Hi Jed,\n\nLong."))
    assert out["body"].startswith("Hi Jed,")


def test_the_page_gets_the_same_signature():
    import mailer
    assert crm.mail_status(True)["signature"] == SIG


def test_trial_length_is_one_number_everywhere():
    # What the pitch promises is what signup grants: main.py sets trial_ends_at
    # from pitch.TRIAL_DAYS, and no sales line may still promise a month.
    import coach
    assert pitch.TRIAL_DAYS == 15
    for text in (pitch.master_sheet(), pitch.STYLE, pitch.EXAMPLE_EMAIL, coach.PRODUCT, coach.ASKS_TEXT):
        assert "month free" not in text.lower() and "first month" not in text.lower()
        assert "{TRIAL" not in text
    assert "15 days free" in pitch.master_sheet() and "15 days free" in pitch.STYLE


# ── "write a long informative email" means long ────────────────────────────
# It came back as 70 words: typed under the auto-draft, the request was sent
# as "change it, keeping everything else as it is", and the style's 30-90
# word follow-up rule beat it.

def test_asking_for_long_is_recognised_and_shorter_is_not():
    for b in ("write a long informative email about what we do", "explain how it works",
              "make it longer, all the detail", "more detail on ordering"):
        assert pitch.wants_long(b), b
    for b in ("shorter", "keep everything else, just shorter", "use his full name",
              "mention the free trial", "not so long, shorter", ""):
        assert not pitch.wants_long(b), b


def test_a_long_request_under_a_short_draft_is_a_rewrite(drafted):
    crm.draft_lead_email("L1", crm.DraftRequest(
        brief="write a long informative email about what we do",
        subject="old", body="Hi Alex,\n\nShort one.\n\nBest,"))
    assert "REWRITE it as a new, longer email" in drafted["ask"]
    assert "keeping everything else as it is" not in drafted["ask"]
    assert "250-400 words" in drafted["ask"]


def test_a_long_request_reaches_a_follow_up_and_a_first_email(drafted):
    crm.draft_lead_email("L1", crm.DraftRequest(followup=True, brief="long, explain what we do"))
    assert "LENGTH: the salesperson wants a LONG" in drafted["ask"]
    crm.draft_lead_email("L1", crm.DraftRequest(brief="first email, short and sweet"))
    assert "LENGTH:" not in drafted["ask"]


def test_lint_holds_a_long_request_to_length():
    short = "Hi Alex,\n\n" + "The count takes too long on Sundays. " * 5 + "\n\nBest,"
    asked = "write a long informative email about what we do"
    for kind in ("first", "followup", "revision"):
        assert any("asked for a long" in p for p in pitch.lint("your count", short, kind, asked))
    assert not any("asked for a long" in p for p in pitch.lint("your count", short, "first"))
    long_body = "Hi Alex,\n\n" + "The count takes too long on Sundays. " * 45 + "\n\nBest,"
    probs = pitch.lint("your count", long_body, "followup", asked)
    assert not any("words" in p for p in probs)


# ── the drafter learns from what the owner actually sends ──────────────────

def _signed(text):
    return pitch.sign(text + "\n\nBest,") + "\n\n" + pitch.OPT_OUT_LINE


def test_owner_edits_keeps_only_drafts_he_changed():
    rows = [
        {"draft_subject": "your count", "sent_subject": "your count",
         "draft_body": _signed("Hi Ed,\n\nCount and orders run 10 to 15 minutes."),
         "sent_body": _signed("Hi Ed,\n\nThe whole count and every order takes about 15 "
                              "minutes, and every rep gets their order at once.")},
        {"draft_subject": "same", "sent_subject": "same",
         "draft_body": _signed("Hi Jo,\n\nUntouched draft."),
         "sent_body": _signed("Hi Jo,\n\nUntouched draft.")},
        {"draft_subject": "old subject", "sent_subject": "better subject",
         "draft_body": _signed("Hi Al,\n\nSame body."), "sent_body": _signed("Hi Al,\n\nSame body.")},
    ]
    out = pitch.owner_edits(rows)
    assert [e["sent_subject"] for e in out] == ["your count", "better subject"]
    assert "every rep gets their order" in out[0]["sent"]
    # Signature and footer are not part of the lesson.
    assert pitch.SIGNATURE.split("\n")[0] not in out[0]["sent"]
    assert pitch.OPT_OUT_LINE not in out[0]["draft"]
    assert len(pitch.owner_edits(rows * 5, limit=3)) == 3


def test_his_edits_go_in_the_prompt_above_the_style_guide():
    edits = [{"draft_subject": "a", "sent_subject": "b", "draft": "DRAFTED TEXT",
              "sent": "SENT TEXT"}]
    p = pitch.system_prompt("", None, edits)
    assert "HOW THE OWNER CHANGED OUR LAST DRAFTS" in p
    assert p.index("SENT TEXT") < p.index("HOW TO WRITE IT")
    assert "HOW THE OWNER CHANGED" not in pitch.system_prompt("", None, [])


def test_every_draft_is_kept_for_comparison(drafted, monkeypatch):
    saved = []
    monkeypatch.setattr(crm, "_save_draft", lambda *a: saved.append(a))
    monkeypatch.setattr(crm, "_owner_edits", lambda *a, **k: [])
    crm.draft_lead_email("L1", crm.DraftRequest(brief="first email"))
    assert saved and saved[0][0] == "L1" and saved[0][2] == "Rioja's Sunday count"


def test_the_style_asks_for_whole_sentences_not_telegrams():
    assert "COMPLETE, NATURAL SENTENCES" in pitch.STYLE
    assert "occasional fragment" not in pitch.STYLE
    assert "Don't narrate the sales process" in pitch.STYLE
