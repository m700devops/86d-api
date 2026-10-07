"""Nothing keeps quoting the old offer ($29.99 a month, the first month free)
after the price and trial changed (#66: $49.99/month, 15 days free).

`pitch.stale_offer` finds an old price or trial in text. It is applied where
old wording survives: email drafts (the drafter learns from past emails that
got replies), the playbook learned from calls, the Call Coach's suggested
lines. Saved prep sheets are rewritten when the master sheet changes, and a
School pack's AI-written quiz and Gauntlet are only served if they were
written against today's offer.
"""
import contextlib
import inspect
import os
import sys

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")

import callcoach  # noqa: E402
import pitch  # noqa: E402
import playbook  # noqa: E402


@pytest.mark.parametrize("text", [
    "It's $29.99/month after that.",
    "only $29.99 a month",
    "$29.99 per month, cancel any time",
    "It's $29.99.",                              # looks like a price
    "Your first month free, no card.",
    "the first month is free",
    "Try it free for a month.",
    "Start your free 30-day trial",
    "30 days free, no card",
    "free for 30 days",
    "the first 30 days are free",
])
def test_old_offer_is_found(text):
    assert pitch.stale_offer(text)


@pytest.mark.parametrize("text", [
    "It's $49.99/month after that.",
    "$49.99 a month",
    "The first 15 days are free, no card.",
    "15 days free, then $49.99/month",
    "a 15-day free trial",
    "Laura just paid $800 at the vet for her cat",   # a story, not an offer
    "they spend $1,200 a week on liquor",
    "we count once a month",
    "Owner wants to talk next month",
    "",
])
def test_current_offer_and_ordinary_text_pass(text):
    assert pitch.stale_offer(text) == []


def test_a_figure_the_salesperson_gave_is_theirs():
    # e.g. a bar in the first 10 accounts told its launch price on purpose
    assert pitch.stale_offer("It's $29.99 a month for you.",
                             allowed="tell her she keeps the $29.99 launch price") == []
    assert pitch.stale_offer("It's $29.99 a month for you.", allowed="$29") != []


def test_lint_flags_an_old_price_in_a_draft():
    problems = pitch.lint("quick question", "It's free for 30 days, then $29.99/month.")
    assert any("old trial" in p for p in problems)
    assert any("old price" in p for p in problems)
    assert not any("old" in p for p in pitch.lint(
        "quick question", "The first 15 days are free, then $49.99/month."))


def test_the_master_sheet_states_the_current_offer():
    sheet = pitch.master_sheet()
    assert pitch.PRICE in sheet and pitch.TRIAL_DAYS == 15
    assert pitch.stale_offer(sheet) == []


def test_playbook_drops_an_old_offer_learned_from_calls():
    out = {"sections": [{"title": "Objections", "points": [
        {"text": "When they balk at price, $29.99 a month is less than one bottle.",
         "evidence": ["Olde Town Tavern"]},
        {"text": "Lead with the count taking an hour on Sunday.",
         "evidence": ["Olde Town Tavern"]},
    ]}]}
    cleaned = playbook.clean(out, ["Olde Town Tavern"])
    texts = [p["text"] for s in cleaned["sections"] for p in s["points"]]
    assert texts == ["Lead with the count taking an hour on Sunday."]


def test_the_stored_playbook_never_teaches_an_old_offer():
    pb = {"sections": [{"title": "Objections", "points": [
        {"text": "Say the first month is free.", "evidence": ["A"], "pinned": True},
        {"text": "Ask who does the count.", "evidence": ["A"]},
    ]}]}
    text = playbook.render(pb)
    assert "first month" not in text and "Ask who does the count." in text


def test_call_coach_never_suggests_an_old_offer():
    assert not callcoach.line_ok("It's only $29.99 a month, want to try it?")
    assert not callcoach.line_ok("Your first month is free.")
    assert callcoach.line_ok("The first 15 days are free and there's no card.")


def test_prep_sheets_are_rewritten_when_the_master_sheet_changes():
    import crm
    src = inspect.getsource(crm.lead_brief)
    assert "_brief_fingerprint(ask + " in src and "pitch.master_sheet()" in \
        src[src.index("_brief_fingerprint(ask"):][:80]


def _serve(monkeypatch, pack):
    import school

    class Cur:
        def __init__(self):
            self.n = 0

        def execute(self, sql, params=()):
            self.n += 1

        def fetchone(self):
            from datetime import datetime, timezone
            when = datetime(2026, 10, 5, 2, tzinfo=timezone.utc)
            if self.n == 1:
                return {"id": 7, "started_at": when, "pack": dict(pack)}
            return {"started_at": when, "finished_at": when, "ok": True, "error": None}

    class Conn:
        def cursor(self):
            return Cur()

    @contextlib.contextmanager
    def fake_db():
        yield Conn()

    import database
    monkeypatch.setattr(database, "get_db", fake_db, raising=False)
    return school.latest_pack()["pack"]


def test_a_school_pack_from_the_old_offer_keeps_its_videos_not_its_quiz(monkeypatch):
    old = {"videos": {"daily": [1]}, "quiz": [["q"]] * 6, "gauntlet": [["g"]] * 6}
    served = _serve(monkeypatch, old)
    assert "quiz" not in served and "gauntlet" not in served and served["videos"]
    current = dict(old, offer=pitch.offer_stamp())
    served = _serve(monkeypatch, current)
    assert served["quiz"] and served["gauntlet"]
