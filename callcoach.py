"""The Call Coach hub — pure parts.

The salesman score says HOW a call went; this says what to do about it. Every
recorded conversation (crm_calls, from CloudTalk) gets a line-by-line
breakdown: the exact line that cost something, why, what to say instead, and
the same moment answered through several named selling methods (TECHNIQUES).
Across calls it builds what a coach would keep on the wall: measured habits
(talk share, questions, monologues, fillers — COUNTED here, never the
model's), the lines that worked, every objection heard with the best answers
to it, and the patterns costing the most.

Nothing the model says about a call is shown unless it can be checked: every
quote must really be in the transcript (`clean_review`), every method must be
one of TECHNIQUES, and a suggested line may not claim what the company can't
back (customer numbers, percentages, "bars like yours") — the same rule the
email drafter lives under (pitch.UNBACKED_CLAIMS).

Pure: no network, no database. crm.py runs it. See test_callcoach.py.
"""

import re
from datetime import datetime, timedelta
from typing import Optional

# ── the methods ─────────────────────────────────────────────────────────────
# Each: (name, who it comes from, what it is, a shape of line). Only methods
# with a real, widely taught source; the model must pick from these keys, so a
# suggestion always says which method it is and the page can explain it.

TECHNIQUES = {
    "permission": (
        "Permission-based opener", "modern cold-calling coaches (e.g. Cold Calling Sucks)",
        "Name the interruption honestly, ask for 30 seconds, and let them say no.",
        "I know I'm calling out of the blue — can I take 30 seconds to tell you why, and you tell me if it's worth another minute?"),
    "problem_first": (
        "Problem-first opener", "Josh Braun (\"poke the bear\")",
        "Open with a problem bars like theirs might have, asked as a question — no features, no pitch.",
        "Quick question — when a count's done, who types it into the orders, and how long does that take?"),
    "upfront_contract": (
        "Up-front contract", "Sandler",
        "Agree what the next few minutes are for, and that 'no' is a fine answer.",
        "How about this: I ask a couple of questions, and if it's not a fit you just say so — fair?"),
    "pain_funnel": (
        "Pain funnel", "Sandler",
        "Go down a level each question: tell me more, an example, how long, what it costs, what you've tried.",
        "You said Sunday counts drag — what does that look like? How long does it take? What happens when it runs late?"),
    "spin": (
        "SPIN questions", "Neil Rackham, SPIN Selling",
        "Situation, then Problem, then Implication (what the problem causes), then Need-payoff (what fixing it is worth).",
        "When an order comes in wrong, what does that do to the weekend?"),
    "gap": (
        "Gap selling", "Keenan",
        "Find where they are now and where they want to be; the sale is the gap between the two.",
        "If counts and orders were exactly how you wanted, what would be different from today?"),
    "challenger": (
        "Challenger insight", "Dixon & Adamson, The Challenger Sale",
        "Teach something they hadn't considered about their own work, tailored to them, then take the lead on the next step.",
        "Most of the time a count takes isn't the counting — it's retyping it into orders afterwards. That's the part we take away."),
    "nepq": (
        "Neutral questioning (NEPQ)", "Jeremy Miner",
        "Calm, curious, detached tone; questions that let them find the problem and say it in their own words.",
        "Just curious — how are you finding the way you do counts now?"),
    "label": (
        "Labels & mirrors", "Chris Voss, Never Split the Difference",
        "Name what they seem to feel ('It sounds like…') or repeat their last few words as a question, then go quiet.",
        "It sounds like you've been burned by an app that promised a lot."),
    "no_oriented": (
        "No-oriented & calibrated questions", "Chris Voss",
        "Ask so that 'no' means yes ('Would it be crazy to…?'), or ask 'how'/'what' instead of yes/no.",
        "Would it be a bad idea to try it on just your next count?"),
    "laer": (
        "Acknowledge, explore, respond (LAER)", "classic objection handling",
        "Listen, acknowledge the objection honestly, ask a question to understand it, then answer what they actually meant.",
        "That's fair — what would have to be true for it to be worth a look?"),
    "micro_ask": (
        "One small ask", "modern cold-calling practice",
        "End with ONE low-effort yes/no ask — interest, not a meeting — and wait for the answer.",
        "Worth trying on your next count? The first 15 days are free, no card."),
}
TECHNIQUE_KEYS = tuple(TECHNIQUES)

PARTS = ("opener", "discovery", "objections", "ask", "tone")
PART_LABEL = {"opener": "Opener", "discovery": "Discovery", "objections": "Objections",
              "ask": "The ask", "tone": "Tone & pace"}

OBJECTION_KINDS = {
    "has_system": "Already has a system or app",
    "happy": "Happy with how they do it",
    "no_time": "No time / busy right now",
    "send_info": "Send me an email / info",
    "not_dm": "Not the decision maker",
    "price": "Price / another subscription",
    "not_interested": "Not interested",
    "tried_before": "Tried something before and it didn't stick",
    "timing": "Bad timing / call back later",
    "other": "Something else",
}


def techniques() -> list:
    """The glossary, for the page and the prompts."""
    return [{"key": k, "name": v[0], "source": v[1], "what": v[2], "example": v[3]}
            for k, v in TECHNIQUES.items()]


def techniques_text() -> str:
    return "\n".join(f"- {k}: {v[0]} ({v[1]}) — {v[2]}" for k, v in TECHNIQUES.items())


# ── measured, never guessed ─────────────────────────────────────────────────

FILLERS = ("um", "uh", "erm", "uhh", "umm", "you know", "kind of", "sort of", "basically",
           "literally", "i mean")
_QUESTION_START = ("what", "how", "who", "when", "where", "why", "which", "do", "does", "did",
                   "are", "is", "can", "could", "would", "will", "have", "has", "should")

# Targets are rules of thumb a coach would give, shown as guides, not facts.
TARGETS = {
    "rep_share": (40, 60),         # % of the words that are the rep's
    "questions": 3,                # questions the rep asked, on a call over a minute
    "longest_monologue": 80,       # words in one breath (~30 seconds)
    "fillers_per_100": 2.0,
}


def turns(transcript: str, agent: str = "Stephan") -> list:
    """[(is_rep, text)] from "Speaker: text" lines (cloudtalk.transcript_text)."""
    out = []
    for line in (transcript or "").splitlines():
        head, sep, text = line.partition(":")
        if not sep or len(head) > 40:
            if out:
                out[-1] = (out[-1][0], out[-1][1] + " " + line.strip())
            continue
        out.append((head.strip() == agent, text.strip()))
    return out


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _sentences(text: str) -> list:
    parts, cur = [], []
    for ch in text or "":
        cur.append(ch)
        if ch in ".?!":
            parts.append("".join(cur).strip())
            cur = []
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _is_question(sentence: str) -> bool:
    s = sentence.strip()
    if s.endswith("?"):
        return True
    w = _words(s)
    return bool(w) and w[0] in _QUESTION_START and not s.endswith(".") and not s.endswith("!")


def _count_fillers(words: list) -> int:
    n = 0
    for i, w in enumerate(words):
        for f in FILLERS:
            fw = f.split()
            if words[i:i + len(fw)] == fw:
                n += 1
    return n


def metrics(transcript: str, agent: str = "Stephan") -> dict:
    """What can be COUNTED about a call. Linear in the transcript."""
    ts = turns(transcript, agent)
    rep_words = their_words = rep_q = their_q = longest = fillers = 0
    first_q_turn = None
    rep_turn = 0
    for is_rep, text in ts:
        w = _words(text)
        qs = sum(1 for s in _sentences(text) if _is_question(s))
        if is_rep:
            rep_turn += 1
            rep_words += len(w)
            rep_q += qs
            longest = max(longest, len(w))
            fillers += _count_fillers(w)
            if qs and first_q_turn is None:
                first_q_turn = rep_turn
        else:
            their_words += len(w)
            their_q += qs
    total = rep_words + their_words
    return {
        "rep_words": rep_words, "their_words": their_words,
        "rep_share": round(100 * rep_words / total) if total else None,
        "questions": rep_q, "their_questions": their_q,
        "longest_monologue": longest,
        "fillers": fillers,
        "fillers_per_100": round(100 * fillers / rep_words, 1) if rep_words else 0.0,
        "first_question_turn": first_q_turn,
        "turns": len(ts),
    }


def metric_verdicts(m: dict, talk_seconds: int = 0) -> list:
    """Each measured habit against its guide: [{key, label, value, target, ok, tip}]."""
    if not m or not m.get("rep_words"):
        return []
    lo, hi = TARGETS["rep_share"]
    share = m.get("rep_share")
    out = [{
        "key": "rep_share", "label": "You talked", "value": f"{share}%",
        "target": f"{lo}–{hi}%", "ok": share is not None and lo <= share <= hi,
        "tip": ("You did most of the talking — ask, then stop." if share and share > hi
                else "They did nearly all the talking — lead a little more." if share is not None and share < lo
                else "Good balance."),
    }]
    if (talk_seconds or 0) >= 60 or m.get("turns", 0) >= 8:
        out.append({"key": "questions", "label": "Questions you asked", "value": str(m["questions"]),
                    "target": f"{TARGETS['questions']}+", "ok": m["questions"] >= TARGETS["questions"],
                    "tip": "Good — questions are how you find the pain." if m["questions"] >= TARGETS["questions"]
                    else "Too few questions — discovery is where calls are won."})
    out.append({"key": "longest_monologue", "label": "Longest stretch talking",
                "value": f"{m['longest_monologue']} words", "target": f"under {TARGETS['longest_monologue']}",
                "ok": m["longest_monologue"] <= TARGETS["longest_monologue"],
                "tip": "Short and conversational." if m["longest_monologue"] <= TARGETS["longest_monologue"]
                else "One long pitch loses them — break it with a question."})
    out.append({"key": "fillers_per_100", "label": "Filler words", "value": f"{m['fillers_per_100']} per 100",
                "target": f"under {TARGETS['fillers_per_100']:g}",
                "ok": m["fillers_per_100"] <= TARGETS["fillers_per_100"],
                "tip": "Clean." if m["fillers_per_100"] <= TARGETS["fillers_per_100"]
                else "Pauses beat 'um' — a beat of silence sounds confident."})
    return out


# ── the review: what the model writes, and the gate it goes through ─────────

def _line_schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": ["technique", "line"],
            "properties": {"technique": {"type": "string", "enum": list(TECHNIQUE_KEYS)},
                           "line": {"type": "string"}}}


def review_schema() -> dict:
    line = _line_schema()
    return {"type": "object", "additionalProperties": False,
            "required": ["headline", "moments", "strengths", "objections", "missed", "drill", "focus"],
            "properties": {
                "headline": {"type": "string"},
                "moments": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["quote", "part", "problem", "why", "say_instead", "technique",
                                 "alternatives"],
                    "properties": {
                        "quote": {"type": "string"},
                        "part": {"type": "string", "enum": list(PARTS)},
                        "problem": {"type": "string"},
                        "why": {"type": "string"},
                        "say_instead": {"type": "string"},
                        "technique": {"type": "string", "enum": list(TECHNIQUE_KEYS)},
                        "alternatives": {"type": "array", "items": line}}}},
                "strengths": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["quote", "why", "technique"],
                    "properties": {"quote": {"type": "string"}, "why": {"type": "string"},
                                   "technique": {"type": "string",
                                                 "enum": list(TECHNIQUE_KEYS) + ["none"]}}}},
                "objections": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["their_words", "you_said", "kind", "better"],
                    "properties": {"their_words": {"type": "string"}, "you_said": {"type": "string"},
                                   "kind": {"type": "string", "enum": list(OBJECTION_KINDS)},
                                   "better": {"type": "array", "items": line}}}},
                "missed": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["their_words", "signal", "ask_this"],
                    "properties": {"their_words": {"type": "string"}, "signal": {"type": "string"},
                                   "ask_this": {"type": "string"}}}},
                "drill": {"type": "string"},
                "focus": {"type": "string"},
            }}


def review_system(product: str, asks: str, knowledge: str = "") -> str:
    return f"""You are a world-class cold-calling coach reviewing a REAL recorded call the founder of
86'd (Stephan, the "Stephan:" lines) made to a bar or restaurant. Your job is to make him better
on the next call: specific, honest, practical, never generic.

What he sells: {product}
The company's asks (the only asks to coach toward): {asks}
{knowledge}

THE METHODS you may coach with (use the key):
{techniques_text()}

Write:
- headline: one sentence — the single most important thing about this call.
- moments: up to 6 of HIS lines that cost the most, in call order. For each: "quote" copied WORD FOR
  WORD from one of his lines (a short exact piece is fine), the part of the call, the problem in
  plain words, why it matters to a bar owner, "say_instead" (the one best replacement line, said
  the way a real person talks), the method it uses, and 2-4 "alternatives": the same moment
  answered through OTHER methods, one line each, all different.
- strengths: up to 3 of his lines that worked, copied word for word, and why.
- objections: every pushback THEY gave, "their_words" copied word for word from their lines,
  "you_said" his answer copied word for word ("" if he didn't answer), the kind, and 3-4 better
  answers, each through a different method.
- missed: up to 4 things THEY said (word for word) that were an opening he didn't take — a pain,
  a buying signal, a name, a time — what it signalled, and the question he should have asked.
- drill: one exercise to practise before the next call, specific to what went wrong.
- focus: the one thing to do differently on the next call, in one sentence.

HARD RULES for every line you suggest:
- Only facts from "what he sells" and the company's asks. NEVER customer numbers, percentages,
  testimonials, "bars like yours", "our customers", "most bars I talk to", or a backstory for him.
- Short and spoken: under 35 words, no jargon, no method names inside the line.
- Transcripts come from speech recognition — don't coach him on a word that was obviously misheard.
- If the call was fine at some part, don't invent a problem there. Fewer, truer points beat many."""


def _norm(text: str) -> str:
    return " ".join(_words(text))


_BANNED = ("bars like yours", "our customers", "customers love", "hundreds of", "thousands of",
           "other bars are", "on average", "most bars i talk", "most owners i talk",
           "most of our", "our clients", "clients", "behind the bar myself", "i used to bartend",
           "when i was bartending", "i ran a bar", "i owned a bar")


def line_ok(line: str) -> bool:
    """A suggested line the company can stand behind: no figures it hasn't got
    and no claims about other customers."""
    low = (line or "").lower()
    if not low.strip() or "%" in low or re.search(r"\bpercent\b", low):
        return False
    import pitch
    if pitch.stale_offer(line):       # an old price or trial
        return False
    return not any(b in low for b in _BANNED)


def _in(quote: str, flat: str) -> bool:
    """The quote is really there: every piece between ellipses, normalised."""
    pieces = [_norm(p) for p in re.split(r"\.\.\.|…", quote or "")]
    pieces = [p for p in pieces if p]
    padded = f" {flat} "
    return bool(pieces) and all(f" {p} " in padded for p in pieces)


def _clip(value, n: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:n]


def _lines(items, limit: int = 4) -> list:
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, dict) or it.get("technique") not in TECHNIQUES:
            continue
        line = _clip(it.get("line"), 400)
        if not line_ok(line) or line.lower() in seen:
            continue
        seen.add(line.lower())
        out.append({"technique": it["technique"], "line": line})
    return out[:limit]


def clean_review(out: dict, transcript: str, agent: str = "Stephan") -> Optional[dict]:
    """The model's review, checked. Every quote must be in the right speaker's
    lines; methods must be known; lines must pass `line_ok`. None if nothing
    survives."""
    if not isinstance(out, dict):
        return None
    ts = turns(transcript, agent)
    rep = " ".join(_norm(t) for r, t in ts if r)
    them = " ".join(_norm(t) for r, t in ts if not r)
    moments = []
    for m in out.get("moments") or []:
        if not isinstance(m, dict) or not _in(m.get("quote"), rep):
            continue
        say = _clip(m.get("say_instead"), 400)
        if not line_ok(say):
            say = ""
        alts = [a for a in _lines(m.get("alternatives")) if a["line"].lower() != say.lower()]
        if not say and not alts:
            continue
        moments.append({"quote": _clip(m.get("quote"), 400),
                        "part": m.get("part") if m.get("part") in PARTS else "tone",
                        "problem": _clip(m.get("problem"), 300), "why": _clip(m.get("why"), 300),
                        "say_instead": say,
                        "technique": m.get("technique") if m.get("technique") in TECHNIQUES else None,
                        "alternatives": alts})
    strengths = [{"quote": _clip(s.get("quote"), 400), "why": _clip(s.get("why"), 300),
                  "technique": s.get("technique") if s.get("technique") in TECHNIQUES else None}
                 for s in out.get("strengths") or []
                 if isinstance(s, dict) and _in(s.get("quote"), rep)][:3]
    objections = []
    for o in out.get("objections") or []:
        if not isinstance(o, dict) or not _in(o.get("their_words"), them):
            continue
        said = _clip(o.get("you_said"), 400)
        if said and not _in(said, rep):
            said = ""
        better = _lines(o.get("better"))
        if not better:
            continue
        objections.append({"their_words": _clip(o.get("their_words"), 400), "you_said": said,
                           "kind": o.get("kind") if o.get("kind") in OBJECTION_KINDS else "other",
                           "better": better})
    missed = [{"their_words": _clip(x.get("their_words"), 400), "signal": _clip(x.get("signal"), 300),
               "ask_this": _clip(x.get("ask_this"), 400)}
              for x in out.get("missed") or []
              if isinstance(x, dict) and _in(x.get("their_words"), them)
              and line_ok(x.get("ask_this"))][:4]
    review = {"headline": _clip(out.get("headline"), 300), "moments": moments[:6],
              "strengths": strengths, "objections": objections[:6], "missed": missed,
              "drill": _clip(out.get("drill"), 500), "focus": _clip(out.get("focus"), 300)}
    if not (moments or strengths or objections or missed):
        return None
    return review


# ── across calls ────────────────────────────────────────────────────────────

def _week(at: Optional[str]) -> Optional[str]:
    try:
        d = datetime.fromisoformat(at).date()
    except (TypeError, ValueError):
        return None
    return (d - timedelta(days=d.weekday())).isoformat()


def hub(calls: list) -> dict:
    """Everything the hub shows that is COUNTED from reviewed and scored calls.

    `calls`: dicts with call_id, bar, when (display), started_at, talk_seconds,
    score, parts (dict or None), review (dict or None), metrics (dict or None).
    """
    scored = [c for c in calls if c.get("score") is not None]
    parts_sum = {p: [] for p in ("opener", "discovery", "objections", "ask")}
    weeks: dict = {}
    for c in scored:
        for p in parts_sum:
            v = (c.get("parts") or {}).get(p)
            if isinstance(v, (int, float)):
                parts_sum[p].append(v)
        w = _week(c.get("started_at"))
        if w:
            weeks.setdefault(w, []).append(c["score"])
    parts_avg = {p: round(sum(v) / len(v), 1) if v else None for p, v in parts_sum.items()}
    known = {p: v for p, v in parts_avg.items() if v is not None}
    weakest = min(known, key=known.get) if known else None
    trend = [{"week": w, "avg": round(sum(v) / len(v)), "calls": len(v)}
             for w, v in sorted(weeks.items())]

    # measured habits, averaged over calls that have a transcript
    ms = [c["metrics"] for c in calls if c.get("metrics") and c["metrics"].get("rep_words")]
    habits = {}
    if ms:
        for k in ("rep_share", "questions", "longest_monologue", "fillers_per_100"):
            vals = [m[k] for m in ms if m.get(k) is not None]
            habits[k] = round(sum(vals) / len(vals), 1) if vals else None
    habit_rows = metric_verdicts({**{k: v for k, v in habits.items()},
                                  "rep_words": 1, "turns": 99,
                                  "rep_share": round(habits["rep_share"]) if habits.get("rep_share") is not None else None,
                                  "questions": habits.get("questions") or 0,
                                  "longest_monologue": round(habits.get("longest_monologue") or 0),
                                  "fillers_per_100": habits.get("fillers_per_100") or 0.0},
                                 talk_seconds=999) if ms else []

    reviewed = [c for c in calls if c.get("review")]
    mistakes = {p: 0 for p in PARTS}
    technique_use: dict = {}
    phrasebook = {p: [] for p in PARTS}
    best_lines, objections, missed = [], {}, []
    seen_lines: set = set()
    for c in reviewed:
        r = c["review"]
        src = {"bar": c.get("bar"), "when": c.get("when"), "call_id": c.get("call_id")}
        for m in r.get("moments") or []:
            mistakes[m.get("part") or "tone"] = mistakes.get(m.get("part") or "tone", 0) + 1
            cands = ([{"technique": m.get("technique"), "line": m.get("say_instead")}]
                     if m.get("say_instead") else []) + (m.get("alternatives") or [])
            for a in cands:
                key = (a.get("line") or "").lower()
                if not key or key in seen_lines:
                    continue
                seen_lines.add(key)
                if a.get("technique"):
                    technique_use[a["technique"]] = technique_use.get(a["technique"], 0) + 1
                phrasebook[m.get("part") or "tone"].append({**a, "instead_of": m.get("quote"), **src})
        for s in r.get("strengths") or []:
            best_lines.append({**s, **src, "score": c.get("score")})
        for o in r.get("objections") or []:
            k = o.get("kind") or "other"
            entry = objections.setdefault(k, {"kind": k, "label": OBJECTION_KINDS.get(k, k),
                                              "count": 0, "heard": [], "you_said": [], "better": []})
            entry["count"] += 1
            if o.get("their_words") and len(entry["heard"]) < 6:
                entry["heard"].append({"text": o["their_words"], **src})
            if o.get("you_said") and len(entry["you_said"]) < 4:
                entry["you_said"].append({"text": o["you_said"], **src})
            for b in o.get("better") or []:
                if b["line"].lower() not in {x["line"].lower() for x in entry["better"]}:
                    entry["better"].append({**b, **src})
        for x in r.get("missed") or []:
            missed.append({**x, **src})
    for p in phrasebook:
        phrasebook[p] = phrasebook[p][:14]
    best_lines.sort(key=lambda s: -(s.get("score") or 0))
    return {
        "scored": len(scored), "reviewed": len(reviewed),
        "average": round(sum(c["score"] for c in scored) / len(scored)) if scored else None,
        "best": max((c["score"] for c in scored), default=None),
        "parts": parts_avg, "weakest": weakest, "trend": trend,
        "habits": habit_rows,
        "mistakes": {p: n for p, n in mistakes.items() if n},
        "technique_use": technique_use,
        "phrasebook": {p: v for p, v in phrasebook.items() if v},
        "best_lines": best_lines[:12],
        "objections": sorted(objections.values(), key=lambda e: -e["count"]),
        "missed": missed[:12],
    }


# ── patterns across calls (one model call) ──────────────────────────────────

def patterns_schema() -> dict:
    line = _line_schema()
    return {"type": "object", "additionalProperties": False,
            "required": ["habits", "keep_doing", "script", "goal"],
            "properties": {
                "habits": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["habit", "evidence", "cost", "fix", "practice"],
                    "properties": {"habit": {"type": "string"},
                                   "evidence": {"type": "array", "items": {"type": "string"}},
                                   "cost": {"type": "string"}, "fix": {"type": "string"},
                                   "practice": {"type": "string"}}}},
                "keep_doing": {"type": "array", "items": {"type": "string"}},
                "script": {"type": "object", "additionalProperties": False,
                           "required": ["opener", "discovery", "objections", "ask", "voicemail"],
                           "properties": {
                               "opener": line,
                               "discovery": {"type": "array", "items": line},
                               "objections": {"type": "array", "items": {
                                   "type": "object", "additionalProperties": False,
                                   "required": ["kind", "technique", "line"],
                                   "properties": {"kind": {"type": "string", "enum": list(OBJECTION_KINDS)},
                                                  "technique": {"type": "string", "enum": list(TECHNIQUE_KEYS)},
                                                  "line": {"type": "string"}}}},
                               "ask": line,
                               "voicemail": {"type": "string"}}},
                "goal": {"type": "string"},
            }}


def patterns_system(product: str, asks: str, knowledge: str = "") -> str:
    return f"""You are a cold-calling coach looking across MANY of the founder's real recorded calls
(Stephan, selling 86'd to bars) to find the habits that cost him the most and build him a better
talk track from his own calls.

What he sells: {product}
The company's asks: {asks}
{knowledge}

THE METHODS (use the key):
{techniques_text()}

You get each call's score, measured habits, the review of it and his exact lines. Write:
- habits: up to 4 recurring habits costing him calls, worst first. "evidence": 1-3 of his lines
  copied WORD FOR WORD from the calls, showing it. "cost": what it does to the call. "fix": the
  change. "practice": a 2-minute exercise.
- keep_doing: up to 3 things he does well, each one sentence.
- script: his talk track, built from what worked in HIS calls plus the methods — an opener,
  3-5 discovery questions, one answer per objection kind he actually hears, one ask, and a
  voicemail under 30 words.
- goal: one measurable goal for next week, stated with a number from the data you were given.

Every suggested line: only facts from what he sells; never customer numbers, percentages,
testimonials, "bars like yours" or "most bars I talk to"; under 35 words; spoken, not written."""


def clean_patterns(out: dict, transcripts_flat: str) -> Optional[dict]:
    if not isinstance(out, dict):
        return None
    habits = []
    for h in out.get("habits") or []:
        if not isinstance(h, dict):
            continue
        ev = [_clip(e, 300) for e in h.get("evidence") or [] if _in(e, transcripts_flat)]
        if not ev:
            continue
        habits.append({"habit": _clip(h.get("habit"), 200), "evidence": ev[:3],
                       "cost": _clip(h.get("cost"), 300), "fix": _clip(h.get("fix"), 300),
                       "practice": _clip(h.get("practice"), 400)})
    s = out.get("script") if isinstance(out.get("script"), dict) else {}
    one = lambda x: (_lines([x], 1) or [None])[0] if isinstance(x, dict) else None
    objs = []
    for o in s.get("objections") or []:
        if isinstance(o, dict) and o.get("kind") in OBJECTION_KINDS and o.get("technique") in TECHNIQUES \
                and line_ok(o.get("line")):
            objs.append({"kind": o["kind"], "label": OBJECTION_KINDS[o["kind"]],
                         "technique": o["technique"], "line": _clip(o.get("line"), 400)})
    vm = _clip(s.get("voicemail"), 400)
    script = {"opener": one(s.get("opener")), "discovery": _lines(s.get("discovery"), 5),
              "objections": objs[:len(OBJECTION_KINDS)], "ask": one(s.get("ask")),
              "voicemail": vm if line_ok(vm) else ""}
    return {"habits": habits[:4],
            "keep_doing": [_clip(k, 300) for k in out.get("keep_doing") or [] if k][:3],
            "script": script, "goal": _clip(out.get("goal"), 300)}


def call_digest(c: dict, lines: int = 18) -> str:
    """One call, compressed for the patterns prompt: his lines only, capped."""
    head = f"CALL {c.get('bar')} ({c.get('when')}) score {c.get('score')}"
    m = c.get("metrics") or {}
    if m:
        head += (f" · talked {m.get('rep_share')}% · {m.get('questions')} questions · "
                 f"longest {m.get('longest_monologue')} words")
    r = c.get("review") or {}
    bits = [head]
    if r.get("headline"):
        bits.append("Review: " + r["headline"])
    for mo in (r.get("moments") or [])[:4]:
        bits.append(f"- {mo.get('part')}: \"{mo.get('quote')}\" — {mo.get('problem')}")
    mine = [t for is_rep, t in turns(c.get("transcript") or "") if is_rep][:lines]
    if mine:
        bits.append("His lines: " + " | ".join(_clip(t, 240) for t in mine))
    return "\n".join(bits)


# ── ask the coach ───────────────────────────────────────────────────────────

def ask_schema() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["answer", "lines"],
            "properties": {"answer": {"type": "string"},
                           "lines": {"type": "array", "items": _line_schema()}}}


def ask_system(product: str, asks: str, knowledge: str = "") -> str:
    return f"""You are the founder's cold-calling coach (Stephan, selling 86'd to independent bars). He
asks you anything about calling: what to say to an objection, how to open, how to get past a
bartender, what went wrong on a call. Answer like a sharp coach: direct, practical, specific to
bars, in under 150 words. Then give 2-5 lines he could actually say, each through one of the methods.

What he sells: {product}
The company's asks: {asks}
{knowledge}

THE METHODS (use the key):
{techniques_text()}

You also get what his own calls look like (objections he hears, his habits). Use them. Every line:
only facts from what he sells; never customer numbers, percentages, testimonials, "bars like yours"
or "most bars I talk to"; under 35 words; spoken."""


def clean_ask(out: dict) -> dict:
    return {"answer": _clip((out or {}).get("answer"), 1500),
            "lines": _lines((out or {}).get("lines"), 5)}
