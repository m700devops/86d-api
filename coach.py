"""Cold-call practice — the owners you spar with, and the rules of the game.

Pure on purpose: prompts in, numbers out, no database, no network, no clock.
crm.py owns the routes and the one call to Claude; everything that decides who
won lives here so test_coach.py can check it without either.

The game ("The Holdout") is a call to a tough independent bar owner who has to
end up agreeing to try 86'd. The model plays the owner AND proposes how the
meters move, but it never gets the final say on winning: `apply_turn()` clamps
every number and only honours "yes, I'll try it" once trust and discovered
pains actually clear the bar. Left to itself a model agrees too easily — being
agreeable is the thing it's best at — and a game you can't lose teaches nothing.
"""

from typing import Optional

WIN_TRUST = 70
WIN_PAINS = 2
PAIN_BONUS = 8          # patience back when you find what's actually hurting them

BOSSES = {
    "dale": {
        "name": "Dale", "level": 1, "patience": 70,
        "opening": "Yeah, this is Dale. Who's this?",
        "brief": ("Dale, 58, has run the same independent dive bar in Sacramento, CA for 22 "
                  "years. Gruff, funny, hates salespeople, got burned by a POS company that "
                  "locked him into a contract. Counts bottles himself on paper on Sunday "
                  "nights. Has an iPhone his daughter set up."),
        "pains": {
            "sunday": "Sunday-night counts take him about three hours",
            "shorted": "his distributor shorts or substitutes orders and he only notices days later",
            "prices": "he never notices when a rep quietly raises a price",
        },
    },
    "priya": {
        "name": "Priya", "level": 2, "patience": 60,
        "opening": "This is Priya.",
        "brief": ("Priya, 34, is GM of an eight-month-old independent craft cocktail bar in "
                  "Oakland, CA. Sharp, proud of her Google Sheet system, allergic to "
                  "buzzwords, asks pointed questions back. The team uses iPhones."),
        "pains": {
            "skus": "about 400 SKUs make the sheet slow and error-prone",
            "turnover": "new bartenders never learn the sheet, so counts are inconsistent",
            "texts": "price changes arrive as texts from three different reps and get lost",
        },
    },
    "nguyen": {
        "name": "The Nguyen siblings", "level": 3, "patience": 55,
        "opening": "Hello? — Tuan, I've got it — hello?",
        "brief": ("Linh and Tuan Nguyen bought an existing neighbourhood bar in Reno, NV four "
                  "months ago. They talk over each other. Cash is tight. Tuan uses Android and "
                  "will bring it up; Linh uses an iPhone and does the ordering. They inherited "
                  "the old owner's distributor contacts and no price records at all."),
        "pains": {
            "noprices": "they have no record of what the old owner paid for anything",
            "cash": "over-ordering is burning cash they don't have",
            "chaos": "nobody knows which rep to call for which product",
        },
    },
    "marco": {
        "name": "Marco", "level": 4, "patience": 45,
        "opening": "Marco.",
        "brief": ("Marco, 45, owns two independent bars in Seattle, WA. Former enterprise "
                  "software salesman who names your technique out loud ('nice permission "
                  "opener'), tests you, and respects only directness and real numbers. "
                  "Washington has no tip credit, so his labour is expensive. Uses an iPhone."),
        "pains": {
            "labor": "his managers spend about five paid hours a week on counts at full WA wage",
            "twobars": "he can't compare costs between his two bars",
            "leak": "he suspects pour-cost leakage but can't prove it",
        },
    },
}

# Guest owners rotate through the practice screen every few days (the rotation
# itself is decided in the browser from the date). They are never part of the
# unlock ladder above, so a guest you haven't met yet can't block progress.
GUESTS = {
    "rosa": {
        "name": "Rosa", "patience": 55,
        "opening": "Cocina Rosa, this is Rosa — make it quick, lunch is slammed.",
        "brief": ("Rosa, 49, chef-owner of an independent Mexican restaurant with a full bar in "
                  "San Diego, CA. Warm but mid-lunch-rush, thinks in margins, proud of her "
                  "90-bottle tequila and mezcal list. iPhone."),
        "pains": {
            "agave": "agave prices jumped and she doesn't know which bottles lost money",
            "sundays": "her brother does counts on Sundays and gets them wrong",
            "backbar": "she keeps running out of the popular tequilas mid-weekend",
        },
    },
    "mike": {
        "name": "Big Mike", "patience": 60,
        "opening": "Mike's. Game's on in an hour, what do you need?",
        "brief": ("Big Mike, 52, owns an independent sports bar in Phoenix, AZ. Loud, friendly, "
                  "loyal to his beer and liquor reps who take him to games. Arizona has a tip "
                  "credit, so labour cost isn't his hot button — time and running out is. iPhone."),
        "pains": {
            "gameday": "he runs out of the big sellers on game days",
            "reps": "his reps do his ordering and he suspects they over-order",
            "notime": "he has no time to count before Sunday games",
        },
    },
    "hannah": {
        "name": "Hannah", "patience": 50,
        "opening": "Hi, this is Hannah.",
        "brief": ("Hannah, 38, owns an independent wine and cocktail bar in Portland, OR. "
                  "Analytical, already trialling a competitor inventory app and lukewarm about it. "
                  "Will ask exactly how 86'd is different. Oregon has no tip credit. iPhone."),
        "pains": {
            "setup": "the competitor app took weeks to set up and staff still skip it",
            "wine": "partial wine bottles make her counts guesswork",
            "cost": "she's paying for features she doesn't use",
        },
    },
    "earl": {
        "name": "Earl", "patience": 50,
        "opening": "Yeah, Earl here.",
        "brief": ("Earl, 71, has owned a neighbourhood tavern with a full liquor licence in "
                  "Fresno, CA for 35 years. Old-school, pays reps by check, distrusts apps, but "
                  "his granddaughter set him up with an iPhone and he secretly likes it. Slow, "
                  "dry humour, hates being rushed."),
        "pains": {
            "eyes": "his eyesight makes the paper count sheet hard to read",
            "handover": "he wants his niece to take over the ordering but it's all in his head",
            "overpay": "he suspects one rep has been overcharging him for years",
        },
    },
    "jess": {
        "name": "Jess (bar manager)", "patience": 60,
        "opening": "Hey, this is Jess, the owner's not in — can I help?",
        "brief": ("Jess, 29, bar manager (not owner) at an independent cocktail bar in Las Vegas, "
                  "NV, off-Strip. Does all the counting and ordering and hates it. Can start a "
                  "free trial herself but is scared of wasting the owner's money or looking bad. "
                  "Nevada has no tip credit. iPhone."),
        "pains": {
            "hours": "counting takes her four unpaid-feeling hours a week",
            "blame": "the owner blames her when orders come in wrong",
            "texts": "she orders by text from her personal phone at midnight",
        },
    },
    "dev": {
        "name": "Dev", "patience": 50,
        "opening": "Dev speaking.",
        "brief": ("Dev, 36, owns an independent speakeasy in Austin, TX with 600 bottles, "
                  "many rare. Cocktail nerd, cost-conscious, hates subscriptions on principle. "
                  "Texas has a tip credit. iPhone."),
        "pains": {
            "rare": "rare bottles get used in specials and he doesn't know what they really cost per pour",
            "sixhundred": "counting 600 bottles takes most of a day",
            "allocations": "he misses distributor allocation windows because orders are late",
        },
    },
}

# Rotating house rules. Each changes the prompt and/or the patience the owner
# starts with; the browser enforces the line limits and interruption rate.
CHALLENGES = {
    "none": {"label": "Standard call", "patience": 0, "prompt": ""},
    "short_fuse": {"label": "Short fuse", "patience": -15,
                   "prompt": "You're in a bad mood today and extra impatient."},
    "price_first": {"label": "Price first", "patience": 0,
                    "prompt": "Early in the call, bluntly demand the price before anything else."},
    "speed": {"label": "Speed round", "patience": 0, "prompt": ""},
    "storm": {"label": "Interruption storm", "patience": 0, "prompt": ""},
    "gatekeeper": {"label": "Wrong person", "patience": 0,
                   "prompt": ("The call is first answered by a bartender. Play the bartender until "
                              "the rep politely earns being put through; then play the owner.")},
    "callback": {"label": "The callback", "patience": 5,
                 "prompt": ("The rep spoke to you briefly last week and you said 'call me back'. "
                            "You vaguely remember. Test whether they remember anything about you.")},
}


def get_boss(boss_id: str) -> Optional[dict]:
    b = BOSSES.get(boss_id) or GUESTS.get(boss_id)
    if b and "level" not in b:
        b = {**b, "level": 2}
    return b


PRODUCT = ("86'd: an iPhone-only app (there is NO Android version) for independent bars. "
           "A permanent price book of every product and what they pay, fast bottle counts, "
           "and one-tap ordering to their distributors. There is a free trial.")

LEVELS = {"warm": "curious but guarded", "busy": "short and distracted",
          "hostile": "annoyed, with a tricky objection"}


def clamp(n, lo: int, hi: int) -> int:
    try:
        n = round(float(n))
    except (TypeError, ValueError):
        n = 0
    return max(lo, min(hi, int(n)))


def curveball_prompt(level: str) -> tuple[str, str]:
    feel = LEVELS.get(level, LEVELS["busy"])
    system = ("You write cold-call practice prompts for a rep selling " + PRODUCT +
              " Reply with JSON only.")
    user = ("Invent ONE thing an independent US bar owner or GM might say on a cold call. "
            f"Mood: {feel}. Vary it widely: objections, odd questions, tests, interruptions, "
            "price, trust, an existing app, Android staff, a new owner, loyalty to a "
            "distributor rep. Return {\"who\": \"role + situation, max 8 words\", "
            "\"line\": \"what they say, max 30 words\"}.")
    return system, user


def grade_prompt(who: str, line: str, answer: str, seconds: int, timed_out: bool) -> tuple[str, str]:
    system = ("You are a tough but fair cold-call coach. The rep sells " + PRODUCT +
              " Reply with JSON only.")
    user = (f"Prospect ({who}) said: {line}\n"
            f"Rep replied{' (ran out of time)' if timed_out else ''} after {seconds}s: {answer}\n\n"
            "Score 0-10 on: acknowledging them, staying calm, asking a question that keeps "
            "the call alive, brevity, no feature-dumping, honesty (never claim Android "
            "support), sounding like a person. Return {\"score\": n, \"skill\": one of "
            "\"opener\",\"discovery\",\"objections\",\"ask\" (the skill this moment tested), "
            "\"worked\": \"one sentence\", \"fix\": \"one sentence\", "
            "\"better\": \"a stronger line to say, max 35 words\"}.")
    return system, user


def turn_prompt(boss_id: str, transcript: list[dict], said: str, patience: int,
                trust: int, found: list[str], interrupt: Optional[str],
                challenge: str = "none") -> tuple[str, str]:
    b = get_boss(boss_id)
    rule = CHALLENGES.get(challenge, CHALLENGES["none"])["prompt"]
    pains = "\n".join(f"- {k}: {v}" + (" (ALREADY FOUND)" if k in found else "")
                      for k, v in b["pains"].items())
    system = (
        "You are playing a character in a cold-call training game. Stay fully in character "
        "and never mention the game, meters or rules out loud.\n\n"
        f"CHARACTER: {b['brief']}\n\nTHE CALLER SELLS: {PRODUCT}\n\n"
        "HIDDEN PAINS — never volunteer these. Reveal one only when the rep asks a good, "
        f"specific question that gets near it:\n{pains}\n\n"
        f"Difficulty {b['level']} of 4 (higher = tougher, stingier with trust).\n"
        + (f"TODAY'S TWIST: {rule}\n" if rule else "") +
        "Pushy, vague, feature-dumping, 'is this a bad time', fake flattery or a lie (like "
        "claiming Android support) costs patience and trust. Empathy, a clear reason for "
        "calling, sharp specific questions, honesty and handling your objection well earn "
        "trust. Raise at least one real objection before you can be won. You may agree to "
        f"try 86'd only if trust would be at least {WIN_TRUST} and at least {WIN_PAINS} pains "
        "have been found; otherwise you are not convinced yet. Reply with JSON only.")
    lines = "\n".join(("REP: " if t.get("role") == "rep" else "OWNER: ") + str(t.get("text", ""))[:600]
                      for t in transcript[-30:])
    user = (f"Meters right now: patience {patience}/100, trust {trust}/100.\n"
            + (f"INTERRUPTION happening right now: {interrupt} React to it naturally.\n" if interrupt else "")
            + f"\nCall so far:\n{lines}\nREP: {said}\n\n"
            "Return {\"reply\": \"your spoken words, 1-3 short sentences, no stage directions\", "
            "\"patience_delta\": integer -30..10, \"trust_delta\": integer -20..25, "
            f"\"pain_found\": one of {sorted(b['pains'])} or null, \"agreed_to_trial\": bool, "
            "\"hung_up\": bool, \"coach_tag\": \"2-4 words on what the rep just did\"}")
    return system, user


def apply_turn(boss_id: str, patience: int, trust: int, found: list[str], out: dict) -> dict:
    """The referee. Turns the model's proposal into the game's actual state."""
    b = get_boss(boss_id)
    found = [f for f in found if f in b["pains"]]
    patience = clamp(patience, 0, 100) + clamp(out.get("patience_delta"), -30, 10)
    trust = clamp(clamp(trust, 0, 100) + clamp(out.get("trust_delta"), -20, 25), 0, 100)
    new_pain = out.get("pain_found")
    if new_pain in b["pains"] and new_pain not in found:
        found = found + [new_pain]
        patience += PAIN_BONUS
    else:
        new_pain = None
    patience = clamp(patience, 0, 100)
    won = bool(out.get("agreed_to_trial")) and trust >= WIN_TRUST and len(found) >= WIN_PAINS
    lost = not won and (bool(out.get("hung_up")) or patience <= 0)
    return {
        "reply": str(out.get("reply") or "…")[:600],
        "coach_tag": str(out.get("coach_tag") or "")[:60],
        "patience": patience, "trust": trust, "found": found, "new_pain": new_pain,
        "result": "won" if won else ("lost" if lost else None),
    }


def review_prompt(boss_id: str, transcript: list[dict], result: str) -> tuple[str, str]:
    b = get_boss(boss_id)
    system = "You are a tough cold-call coach. The rep sells " + PRODUCT + " Reply with JSON only."
    lines = "\n".join(("REP: " if t.get("role") == "rep" else "OWNER: ") + str(t.get("text", ""))[:600]
                      for t in transcript[-40:])
    user = (f"Practice call to {b['name']}. Outcome: {result}.\n\n{lines}\n\n"
            "Score 0-10 each. Return {\"opener\": n, \"discovery\": n, \"objections\": n, "
            "\"ask\": n, \"turning_point\": \"the moment the call turned, quoted, one sentence\", "
            "\"redo\": \"one line to say differently next time, max 30 words\"}.")
    return system, user


def points(won: bool, level: int, trust: int, patience: int, pains: int, lines: int) -> tuple[int, int]:
    """(points, stars). Winning fast with every pain found is the 3-star run."""
    if not won:
        return pains * 15 + trust // 2, 0
    pts = max(0, 100 + level * 50 + trust + patience * 2 + pains * 25 - lines * 3)
    stars = 3 if (lines <= 8 and pains == 3) else (2 if lines <= 12 else 1)
    return pts, stars


SCRIPT_SKILLS = ("opener", "discovery", "objections", "ask")


def script_prompt(skill: str, draft: str) -> tuple[str, str]:
    system = ("You are a tough cold-call coach helping a rep write their OWN words (not a "
              "canned script) for calling independent bar owners. The rep sells " + PRODUCT +
              " Reply with JSON only.")
    user = (f"The rep's draft {skill} line: {draft}\n\nKeep their voice. Score it 0-10, "
            "say in one sentence what to keep and one sentence what to cut or change, and "
            "give a tightened version under 40 words that still sounds like them. "
            "Return {\"score\": n, \"keep\": \"...\", \"change\": \"...\", \"tight\": \"...\"}.")
    return system, user


# ── Tape Doctor ──────────────────────────────────────────────────────────────
# A recorded call with exactly three rep mistakes hidden in it. You find them
# against the clock; false accusations cost more than misses, because on a
# real call "fixing" a line that was working is how good calls go bad.

MISTAKE_KINDS = {
    "bad_time": "asked if it's a bad time / gave an easy exit",
    "feature_dump": "listed features instead of asking or naming a problem",
    "no_reason": "never said why they were calling",
    "talked_over": "kept talking instead of letting the owner answer",
    "missed_pain": "ignored a pain the owner just mentioned",
    "argued": "argued with an objection instead of acknowledging it",
    "overpromise": "promised something untrue (e.g. Android support)",
    "weak_ask": "vague close like 'let me know' or 'can I send info'",
    "closed_question": "a yes/no question where an open one was needed",
    "fake_rapport": "fake flattery or small talk that wastes their time",
    "rushed_ask": "asked for the meeting before earning it",
    "attacked_rep": "badmouthed their distributor rep or current tool",
}

TAPE_PICKS = 3


def tape_prompt(subtle: bool) -> tuple[str, str]:
    system = ("You write training tapes for cold callers selling " + PRODUCT +
              " Reply with JSON only.")
    kinds = "\n".join(f"- {k}: {v}" for k, v in MISTAKE_KINDS.items())
    user = (
        "Write a realistic 14-18 line cold call between a REP and an independent US bar "
        "owner (invent the bar, city and owner). Most rep lines should be GOOD. Plant "
        f"exactly {TAPE_PICKS} rep mistakes, each on a different rep line, each a different "
        f"kind from this list:\n{kinds}\n\n"
        + ("Make the mistakes SUBTLE: plausible lines a decent rep might say, not cartoonish. "
           "Also include one rep line that sounds risky but is actually good.\n" if subtle else
           "Make the mistakes clear but realistic.\n") +
        "Return {\"owner\": \"name, bar, city\", \"lines\": [{\"role\": \"rep\"|\"owner\", "
        "\"text\": \"...\"}], \"mistakes\": [{\"line\": index into lines (0-based), "
        "\"kind\": one of the kinds, \"why\": \"one sentence\", "
        "\"fix\": \"what to say instead, max 30 words\"}]}")
    return system, user


def validate_tape(out: dict) -> Optional[dict]:
    """Keep only a tape the game can be scored on. None means ask again."""
    lines = out.get("lines")
    if not isinstance(lines, list) or not 8 <= len(lines) <= 30:
        return None
    clean = []
    for ln in lines:
        if not isinstance(ln, dict) or ln.get("role") not in ("rep", "owner"):
            return None
        clean.append({"role": ln["role"], "text": str(ln.get("text") or "")[:400]})
    seen, mistakes = set(), []
    for m in out.get("mistakes") or []:
        try:
            i = int(m.get("line"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < len(clean) and clean[i]["role"] == "rep" and i not in seen \
                and m.get("kind") in MISTAKE_KINDS:
            seen.add(i)
            mistakes.append({"line": i, "kind": m["kind"], "label": MISTAKE_KINDS[m["kind"]],
                             "why": str(m.get("why") or "")[:300],
                             "fix": str(m.get("fix") or "")[:300]})
    if len(mistakes) != TAPE_PICKS:
        return None
    return {"owner": str(out.get("owner") or "A bar owner")[:120], "lines": clean,
            "mistakes": sorted(mistakes, key=lambda m: m["line"])}


def tape_score(mistake_lines: list[int], picks: list[int], seconds_left: int) -> dict:
    """+40 per hit, -25 per false accusation, time bonus only for a clean sweep."""
    real, chosen = set(mistake_lines), set(picks)
    hits, false = len(real & chosen), len(chosen - real)
    pts = max(0, hits * 40 - false * 25)
    perfect = hits == len(real) and false == 0
    if perfect:
        pts += 30 + max(0, seconds_left)
    return {"hits": hits, "false": false, "missed": len(real) - hits, "pts": pts, "perfect": perfect}
