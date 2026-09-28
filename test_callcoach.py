"""The Call Coach hub: measured habits, the review gate, and what's built across calls.

Everything the model writes about a real call is checked here before it is
shown: quotes must really be in the transcript and said by the right person,
methods must be ones the hub knows, and no suggested line may claim customer
numbers or percentages the company doesn't have.
"""
import sys
import time
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import callcoach  # noqa: E402

CALL = "\n".join([
    "Bar: Hideaway, this is Mike.",
    "Stephan: Hi Mike, it's Stephan, I own 86'd. Um, is this a bad time?",
    "Bar: Kind of, we open in twenty minutes.",
    "Stephan: Oh sorry. So basically 86'd is an iPhone app that counts your bottles and sends "
    "orders to your distributors and you know it saves a ton of time and it's really easy and "
    "the first month is free.",
    "Bar: We use MarginEdge and we're happy with it.",
    "Stephan: Okay, no problem. Let me know if you change your mind.",
    "Bar: Sunday counts still take forever though.",
    "Stephan: Got it, thanks.",
])


def good_review():
    return {
        "headline": "He pitched before he asked anything.",
        "moments": [
            {"quote": "is this a bad time?", "part": "opener", "problem": "an easy exit",
             "why": "hands them the no", "technique": "permission",
             "say_instead": "I know I'm catching you before you open — 30 seconds on why I called?",
             "alternatives": [
                 {"technique": "problem_first", "line": "Quick one — who does your counts, and when?"},
                 {"technique": "made_up", "line": "not a real method"},
                 {"technique": "spin", "line": "Our customers save 40% of their count time."},
             ]},
            {"quote": "I never said this line", "part": "ask", "problem": "x", "why": "y",
             "technique": "micro_ask", "say_instead": "Worth a try on your next count?",
             "alternatives": []},
        ],
        "strengths": [{"quote": "I own 86'd", "why": "straight about who he is", "technique": "none"},
                      {"quote": "We use MarginEdge", "why": "their line, not his", "technique": "none"}],
        "objections": [
            {"their_words": "We use MarginEdge and we're happy with it.",
             "you_said": "Okay, no problem.", "kind": "has_system",
             "better": [{"technique": "label", "line": "Sounds like it's working — what made you pick it?"},
                        {"technique": "no_oriented", "line": "Would it be crazy to see how the count compares?"},
                        {"technique": "laer", "line": "Most bars I talk to say that too."}]},
            {"their_words": "We hate apps", "you_said": "", "kind": "other",
             "better": [{"technique": "laer", "line": "Fair."}]},
        ],
        "missed": [{"their_words": "Sunday counts still take forever though.",
                    "signal": "a pain, said out loud", "ask_this": "What makes Sunday drag?"}],
        "drill": "Say the opener five times without 'bad time'.",
        "focus": "Ask one question before any pitch.",
    }


def test_habits_are_counted_not_guessed():
    m = callcoach.metrics(CALL)
    assert m["rep_words"] > m["their_words"]
    assert m["rep_share"] > 60
    assert m["questions"] == 1                     # "is this a bad time?"
    assert m["their_questions"] == 0
    assert m["longest_monologue"] >= 35
    assert m["fillers"] == 3        # um, basically, you know — "kind of" is theirs
    assert m["first_question_turn"] == 1
    verdict = {h["key"]: h["ok"] for h in callcoach.metric_verdicts(m, talk_seconds=120)}
    assert verdict == {"rep_share": False, "questions": False, "longest_monologue": True,
                       "fillers_per_100": False}
    assert callcoach.metrics("") ["rep_share"] is None
    assert callcoach.metric_verdicts(callcoach.metrics("")) == []


def test_fillers_only_count_the_reps_words():
    m = callcoach.metrics("Bar: um um um you know\nStephan: Hi there.")
    assert m["fillers"] == 0


def test_the_review_keeps_only_what_checks_out():
    r = callcoach.clean_review(good_review(), CALL)
    assert [m["quote"] for m in r["moments"]] == ["is this a bad time?"]   # the invented one is gone
    alts = r["moments"][0]["alternatives"]
    assert [a["technique"] for a in alts] == ["problem_first"]   # unknown method + a "40%" claim dropped
    assert [s["quote"] for s in r["strengths"]] == ["I own 86'd"]   # theirs isn't his strength
    assert len(r["objections"]) == 1                               # "We hate apps" was never said
    ob = r["objections"][0]
    assert ob["kind"] == "has_system" and ob["you_said"] == "Okay, no problem."
    assert [b["technique"] for b in ob["better"]] == ["label", "no_oriented"]   # "most bars I talk to" is out
    assert r["missed"][0]["ask_this"] == "What makes Sunday drag?"
    assert r["focus"] and r["drill"]


def test_a_quote_must_be_said_by_the_right_person():
    out = good_review()
    out["moments"][0]["quote"] = "we open in twenty minutes"      # the bar said it, not him
    out["objections"][0]["their_words"] = "Let me know if you change your mind"   # he said it
    r = callcoach.clean_review(out, CALL)
    assert r["moments"] == [] and r["objections"] == []


def test_quotes_survive_punctuation_case_and_ellipses():
    out = good_review()
    out["moments"][0]["quote"] = "SO BASICALLY 86'd is an iPhone app ... the first month is free"
    r = callcoach.clean_review(out, CALL)
    assert r["moments"] and r["moments"][0]["quote"].startswith("SO BASICALLY")


def test_a_quote_cut_mid_word_is_not_a_quote():
    out = good_review()
    out["moments"][0]["quote"] = "is this a bad ti"
    assert callcoach.clean_review(out, CALL)["moments"] == []


def test_nothing_checked_out_means_no_review():
    assert callcoach.clean_review({"headline": "x", "moments": [], "strengths": [], "objections": [],
                                   "missed": []}, CALL) is None
    assert callcoach.clean_review("nonsense", CALL) is None


def test_suggested_lines_never_claim_what_we_cant_back():
    for bad in ("Bars like yours save hours.", "It cuts counting 50%.", "Our customers love it.",
                "Most bars I talk to count on paper.", "I used to bartend, I get it."):
        assert not callcoach.line_ok(bad), bad
    assert callcoach.line_ok("Who does your counts, and how long do they take?")


def test_schemas_are_strict():
    def walk(node):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert sorted(node["required"]) == sorted(node["properties"])
            for v in node["properties"].values():
                walk(v)
        if node.get("type") == "array":
            walk(node["items"])
    for schema in (callcoach.review_schema(), callcoach.patterns_schema(), callcoach.ask_schema()):
        walk(schema)


def _call(call_id, score, parts, review, started, bar="Hideaway"):
    return {"call_id": call_id, "bar": bar, "when": "Mon", "started_at": started,
            "talk_seconds": 200, "score": score, "parts": parts, "review": review,
            "metrics": callcoach.metrics(CALL), "transcript": CALL}


def test_the_hub_is_built_from_the_calls():
    review = callcoach.clean_review(good_review(), CALL)
    calls = [
        _call("a", 40, {"opener": 5, "discovery": 5, "objections": 15, "ask": 15}, review,
              "2026-09-21T15:00:00+00:00"),
        _call("b", 60, {"opener": 15, "discovery": 10, "objections": 20, "ask": 15}, review,
              "2026-09-28T15:00:00+00:00", bar="Olde Town"),
        _call("c", None, None, None, "2026-09-28T16:00:00+00:00"),
    ]
    h = callcoach.hub(calls)
    assert h["scored"] == 2 and h["reviewed"] == 2 and h["average"] == 50 and h["best"] == 60
    assert h["weakest"] == "discovery"
    assert [t["week"] for t in h["trend"]] == ["2026-09-21", "2026-09-28"]
    assert h["mistakes"] == {"opener": 2}
    # the same better line from two calls is listed once
    lines = [p["line"] for p in h["phrasebook"]["opener"]]
    assert len(lines) == len(set(l.lower() for l in lines)) == 2
    assert h["phrasebook"]["opener"][0]["instead_of"] == "is this a bad time?"
    ob = h["objections"][0]
    assert ob["kind"] == "has_system" and ob["count"] == 2 and len(ob["better"]) == 2
    assert {x["bar"] for x in ob["heard"]} == {"Hideaway", "Olde Town"}
    assert h["best_lines"][0]["score"] == 60
    assert {x["key"] for x in h["habits"]} >= {"rep_share", "questions", "longest_monologue"}


def test_an_empty_hub_is_empty_not_broken():
    h = callcoach.hub([])
    assert h["scored"] == 0 and h["average"] is None and h["habits"] == [] and h["objections"] == []


def test_patterns_need_evidence_from_his_own_lines():
    flat = " ".join(callcoach._norm(t) for rep, t in callcoach.turns(CALL) if rep)
    out = {"habits": [
        {"habit": "Asks for permission to leave", "evidence": ["is this a bad time?"],
         "cost": "c", "fix": "f", "practice": "p"},
        {"habit": "Invented", "evidence": ["words he never said"], "cost": "c", "fix": "f", "practice": "p"}],
        "keep_doing": ["Says who he is"],
        "script": {"opener": {"technique": "permission", "line": "30 seconds on why I called?"},
                   "discovery": [{"technique": "spin", "line": "What happens when an order's wrong?"},
                                 {"technique": "spin", "line": "Bars like yours lose 20%."}],
                   "objections": [{"kind": "has_system", "technique": "label",
                                   "line": "Sounds like it works — what made you pick it?"},
                                  {"kind": "nope", "technique": "label", "line": "x"}],
                   "ask": {"technique": "micro_ask", "line": "Worth it on your next count?"},
                   "voicemail": "Hi, it's Stephan from 86'd — call me back."},
        "goal": "Ask 3 questions on every call."}
    p = callcoach.clean_patterns(out, flat)
    assert [h["habit"] for h in p["habits"]] == ["Asks for permission to leave"]
    assert len(p["script"]["discovery"]) == 1 and len(p["script"]["objections"]) == 1
    assert p["script"]["objections"][0]["label"] == callcoach.OBJECTION_KINDS["has_system"]
    assert p["script"]["ask"]["line"] and p["script"]["voicemail"]


def test_the_methods_are_all_explained():
    t = callcoach.techniques()
    assert len(t) == len(callcoach.TECHNIQUES) >= 10
    assert all(x["name"] and x["source"] and x["what"] and x["example"] for x in t)
    assert all(callcoach.line_ok(x["example"]) for x in t)


def test_a_huge_hostile_transcript_stays_fast():
    junk = "Stephan: " + ("um " * 40000) + ("? " * 40000) + ("a" * 200000) + "\nBar: " + ("." * 300000)
    t0 = time.time()
    callcoach.metrics(junk)
    callcoach.clean_review(good_review(), junk)
    assert time.time() - t0 < 3
