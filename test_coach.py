"""The Holdout's referee: the model proposes, apply_turn() decides."""

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
