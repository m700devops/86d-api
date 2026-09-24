"""The Holdout's referee: the model proposes, apply_turn() decides."""

import json

import coach
from coach import BOSSES, WIN_TRUST, apply_turn, points, turn_prompt


def test_agreeing_early_does_not_win():
    out = apply_turn("dale", 60, 30, [], {"agreed_to_trial": True, "trust_delta": 10})
    assert out["result"] is None


def test_win_needs_trust_and_two_pains():
    out = apply_turn("dale", 50, 60, ["sunday"],
                     {"agreed_to_trial": True, "trust_delta": 15, "pain_found": "prices"})
    assert out["trust"] >= WIN_TRUST and out["found"] == ["sunday", "prices"]
    assert out["result"] == "won"


def test_deltas_are_clamped():
    out = apply_turn("dale", 50, 50, [], {"patience_delta": 999, "trust_delta": -999})
    assert out["patience"] == 60 and out["trust"] == 30


def test_patience_zero_is_a_hang_up():
    out = apply_turn("marco", 10, 20, [], {"patience_delta": -30})
    assert out["patience"] == 0 and out["result"] == "lost"


def test_unknown_or_repeat_pain_ignored():
    out = apply_turn("priya", 40, 20, ["skus"], {"pain_found": "skus"})
    assert out["new_pain"] is None and out["patience"] == 40
    out = apply_turn("priya", 40, 20, [], {"pain_found": "made_up"})
    assert out["found"] == []


def test_new_pain_gives_patience_back():
    out = apply_turn("priya", 40, 20, [], {"pain_found": "texts"})
    assert out["patience"] == 48 and out["new_pain"] == "texts"


def test_garbage_model_output_is_survivable():
    out = apply_turn("nguyen", 50, 50, ["bogus"], {"patience_delta": "lots", "reply": None})
    assert out["patience"] == 50 and out["found"] == [] and out["reply"]


def test_hidden_pains_marked_found_in_prompt():
    system, _ = turn_prompt("dale", [], "hi", 50, 10, ["sunday"], None)
    assert "(ALREADY FOUND)" in system and BOSSES["dale"]["pains"]["prices"] in system


def test_points_and_stars():
    assert points(False, 2, 40, 0, 1, 10) == (35, 0)
    pts, stars = points(True, 4, 80, 20, 3, 7)
    assert stars == 3 and pts > 0
    assert points(True, 1, 70, 10, 2, 15)[1] == 1


def test_guest_owners_play_by_the_same_rules():
    from coach import GUESTS, get_boss
    for gid in GUESTS:
        b = get_boss(gid)
        assert b["level"] and len(b["pains"]) == 3
        out = apply_turn(gid, 50, 60, [], {"agreed_to_trial": True, "trust_delta": 25})
        assert out["result"] is None          # trust ok, but no problems found yet


def test_unknown_owner_is_none():
    from coach import get_boss
    assert get_boss("nobody") is None


def test_challenge_twist_reaches_prompt():
    from coach import CHALLENGES
    system, _ = turn_prompt("dale", [], "hi", 50, 10, [], None, "price_first")
    assert CHALLENGES["price_first"]["prompt"] in system
    system, _ = turn_prompt("dale", [], "hi", 50, 10, [], None, "made_up")
    assert "TODAY'S TWIST" not in system


def _tape(n_mistakes=3, bad_index=None):
    from coach import MISTAKE_KINDS
    lines = [{"role": "rep" if i % 2 == 0 else "owner", "text": f"l{i}"} for i in range(14)]
    kinds = list(MISTAKE_KINDS)
    ms = [{"line": i * 2, "kind": kinds[i], "why": "w", "fix": "f"} for i in range(n_mistakes)]
    if bad_index is not None:
        ms[0]["line"] = bad_index
    return {"owner": "x", "lines": lines, "mistakes": ms}


def test_tape_needs_exactly_three_rep_mistakes():
    from coach import validate_tape
    assert validate_tape(_tape())["mistakes"][0]["line"] == 0
    assert validate_tape(_tape(2)) is None
    assert validate_tape(_tape(bad_index=1)) is None      # an owner line isn't a rep mistake
    assert validate_tape(_tape(bad_index=99)) is None
    assert validate_tape({"lines": "nope"}) is None


def test_tape_scoring_punishes_false_accusations():
    from coach import tape_score
    assert tape_score([0, 2, 4], [0, 2, 4], 20) == {"hits": 3, "false": 0, "missed": 0, "pts": 170, "perfect": True}
    r = tape_score([0, 2, 4], [0, 2, 6], 50)
    assert r["hits"] == 2 and r["false"] == 1 and r["pts"] == 55 and not r["perfect"]
    assert tape_score([0, 2, 4], [1, 3, 5], 90)["pts"] == 0


# ── the School sells the real product, against real objections ──────────────

def test_practice_knows_the_real_price_and_trial():
    import pitch
    assert pitch.PRICE in coach.PRODUCT and "First month free with no credit card" in coach.PRODUCT
    assert "NO Android" in coach.PRODUCT


def test_curveballs_can_come_from_what_prospects_really_said():
    _, user = coach.curveball_prompt("busy", ["already use BevSpot", "the owner does ordering"])
    assert "REALLY SAID" in user and "- already use BevSpot" in user
    _, plain = coach.curveball_prompt("busy")
    assert "REALLY SAID" not in plain


def test_logged_objections_are_read_back_for_practice(monkeypatch):
    import sys
    import types
    from contextlib import contextmanager
    if "database" not in sys.modules:
        stub = types.ModuleType("database")
        stub.get_db = lambda: None
        sys.modules["database"] = stub
    import crm

    class Cur:
        def execute(self, sql, params=()):
            pass

        def fetchall(self):
            return [{"notes": "[2026-09-24] call · attempt 1: said no · Objection: already use "
                              "BevSpot · Next: none\n[2026-09-25] call: x · Objection: too busy."}]

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur())

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_brain_row", lambda: {"playbook": json.dumps({"sections": [
        {"title": "Objections we hear", "points": [{"text": "Price, 3 bars", "evidence": ["A"]}]}]})})
    assert crm._real_objections() == ["already use BevSpot", "too busy", "Price, 3 bars"]


# ── rehearsal: the real call, practised first ────────────────────────────────

LEAD = {"id": "L1", "name": "Barrel House", "loc": "Denver, CO", "contact": "Laura Keene",
        "last_outcome": "gatekeeper",
        "notes": ("[2026-09-20] call — Spoke to: Jake · How they do it now: paper on Sunday night "
                  "· Objection: no time · Your notes: Jake says Laura does the ordering")}


def test_a_rehearsal_starts_with_whoever_picked_up_and_must_reach_the_decider():
    b = coach.lead_boss(LEAD, ["Craft cocktail bar (their website)"], "call 1", ["Owners count Sundays."],
                        kind="bar")
    assert b["opening"].startswith("Barrel House, Jake speaking")
    assert "put through to Laura Keene" in b["brief"]
    assert "REAL FACTS" in b["brief"] and "paper on Sunday night" in b["brief"]
    assert "Owners count Sundays." in b["brief"] and "call 1" in b["brief"]
    assert "paper on Sunday night" in b["pains"]["count"]          # the real pain, hidden
    assert b["name"] == "Laura Keene" and b["patience"] == 60
    real = "\n".join(b["real"])
    for fact in ("Barrel House, Denver, CO (a bar)", "Who decides / who we ask for: Laura Keene",
                 "Who picked up last time: Jake", "Objection: no time",
                 "Craft cocktail bar (their website)", "CO allows a tip credit"):
        assert fact in real, fact


def test_a_callback_is_answered_by_the_decision_maker():
    b = coach.lead_boss({**LEAD, "last_outcome": "callback"})
    assert b["opening"] == "Barrel House, this is Laura." and "asked the rep to call back" in b["brief"]
    assert b["patience"] == 65


def test_a_bar_with_nothing_on_file_still_rehearses_honestly():
    b = coach.lead_boss({"name": "Nowhere Bar"})
    assert b["real"] == ["The bar: Nowhere Bar"]
    assert "a bartender" in b["brief"] and "the owner" in b["brief"]
    assert b["pains"]["count"].startswith("the weekly count")      # the master sheet's pains
    assert coach.lead_boss({**LEAD, "last_outcome": "not_interested"})["patience"] == 50


def test_the_rehearsal_is_won_by_a_real_next_step_under_the_same_referee():
    b = coach.lead_boss(LEAD)
    system, _ = coach.turn_prompt("lead", [], "Hi, is Laura in?", 60, 10, [], None, boss=b)
    assert "downloading 86'd to try on your next count" in system and "REAL FACTS" in system
    early = coach.apply_turn("lead", 60, 30, [], {"agreed_to_trial": True, "trust_delta": 10}, boss=b)
    assert early["result"] is None                                  # no caving to a friendly model
    won = coach.apply_turn("lead", 60, 60, ["count"], {"agreed_to_trial": True, "trust_delta": 15,
                                                       "pain_found": "orders"}, boss=b)
    assert won["result"] == "won"


def test_the_review_keeps_the_real_call_honest():
    b = coach.lead_boss(LEAD)
    _, user = coach.review_prompt("lead", [{"role": "rep", "text": "hi"}], "won", boss=b)
    assert '"cheat_sheet"' in user and "Only these facts about the bar are real" in user
    assert "never an invented detail" in user and "Laura Keene" in user
    _, plain = coach.review_prompt("dale", [{"role": "rep", "text": "hi"}], "lost")
    assert "cheat_sheet" not in plain


def test_labelled_details_are_read_from_notes():
    d = coach.lead_details("Objection: price · Spoke to: Jake\nObjection: no time · Best time: 3pm")
    assert d["Objection"] == ["price", "no time"] and d["Spoke to"] == ["Jake"]
    assert d["Best time"] == ["3pm"]


# ── the asks the company actually makes ──────────────────────────────────────

def test_practice_drills_the_real_asks():
    assert "first month free, no card" in coach.ASKS_TEXT
    _, user = coach.grade_prompt("Owner", "Send me something", "Sure", 5, False)
    assert coach.ASKS_TEXT in user
    import school
    system, _ = school.content_prompt()
    assert coach.PRODUCT in system and coach.ASKS_TEXT in system
    assert "2-9 months" not in system                               # lead-research rules, not calling


# ── game film: real calls in, coaching out, nothing invented ─────────────────

def test_film_prompt_carries_the_real_calls():
    system, user = coach.film_prompt([{"bar": "Barrel House", "when": "Tue Sep 23",
                                       "outcome": "not_interested",
                                       "notes": "Your notes: said they already have a spreadsheet"}])
    assert "Barrel House" in user and "not interested" in user and "spreadsheet" in user
    assert "Only use moments that are in the calls above" in user and coach.ASKS_TEXT in system


def test_film_drops_anything_not_tied_to_a_real_call():
    out = coach.validate_film({
        "working": {"text": "Asking for the owner by name.", "from": "barrel house"},
        "costing": {"text": "Pitching the bartender.", "from": "Made Up Bar"},
        "drills": [
            {"from": "Barrel House", "who": "Laura", "line": "We have a spreadsheet.",
             "better": "Makes sense. Who updates it when a rep changes a price?"},
            {"from": "Nowhere", "who": "x", "line": "y", "better": "z"},
            {"from": "Barrel House", "who": "Jake", "line": "", "better": "z"}]},
        ["Barrel House", "Workhorse Bar"])
    assert out["working"] == {"text": "Asking for the owner by name.", "from": "Barrel House"}
    assert out["costing"] is None
    assert [d["line"] for d in out["drills"]] == ["We have a spreadsheet."]
