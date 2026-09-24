"""The cold-call school's refresh: new videos and new practice content every few days.

Runs on its own every SCHOOL_EVERY_DAYS days at SCHOOL_RUN_HOUR in SCHOOL_TZ
(default: every 3 days, 10am Asia/Manila, i.e. while the operator in Iloilo is
asleep after a night of calling US bars). Same polling-loop pattern as the lead
generator: main.py wakes every 15 minutes and asks `is_due()`, and "did it run"
is a database question, so a Render restart or spin-down can delay a refresh
but never double it or lose it. A spun-down free-tier service runs the overdue
refresh the first time it wakes.

What a refresh does:
  1. SEARCH — YouTube, across queries written for THIS job (independent bar and
     restaurant owners, hospitality, SaaS-to-small-business), rotated per run so
     each refresh pulls a different slice. Uses the YouTube Data API when
     YOUTUBE_API_KEY is set (durations, embeddability and full descriptions are
     exact); otherwise reads the public results page, which carries title,
     channel, length and the description snippet. Every candidate is then
     checked embeddable via oEmbed.
  2. FILTER — 3 to 21 minutes only, nothing shown in the last few packs.
  3. VET — Claude reads each candidate's title, channel, length and description
     and scores it for one use case: cold-calling busy, skeptical independent
     bar owners to try an iPhone inventory and ordering app. It rejects generic
     motivation, hype and industry-specific scripts that don't transfer, assigns
     each keeper to a step of the call, and writes what to steal from it for a
     bar call. It works from metadata, not from watching the video, and the UI
     says so rather than implying otherwise.
  4. WRITE — a fresh set of Gauntlet rounds and test questions grounded in the
     86'd playbook, each validated so a malformed item can't break the page.

Pure functions (parsing, filtering, validation, scheduling) are at the top and
covered by test_school.py; network and database calls are at the bottom.
"""

import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

SCHOOL_TZ = os.getenv("SCHOOL_TZ", "Asia/Manila")
SCHOOL_RUN_HOUR = int(os.getenv("SCHOOL_RUN_HOUR", "10"))
SCHOOL_EVERY_DAYS = max(1, int(os.getenv("SCHOOL_EVERY_DAYS", "3")))
MIN_SECS, MAX_SECS, IDEAL_SECS = 180, 21 * 60, 10 * 60
MIN_RELEVANCE = 6
PER_STEP, SHELF, DAILY_POOL = 2, 4, 8

STEPS = ["opener", "reason as a problem", "discovery", "objections", "the ask",
         "gatekeepers and voicemail"]

USE_CASE = (
    "The viewer cold-calls independent US bars (and restaurants with full bars) to get the "
    "owner or bar manager to try 86'd, an iPhone-only app that keeps a permanent price book, "
    "speeds up bottle counts and does one-tap ordering to distributors. The people they call "
    "are busy, mid-prep, skeptical of salespeople and software, loyal to their distributor "
    "reps, often not tech-savvy, and usually decide alone. Calls are short, often answered "
    "by a bartender first, and happen before service.")

# Query bank. Each run takes a rotating slice, always including some that name
# the buyer (bars, restaurants, hospitality) so the pack never goes fully generic.
QUERIES = {
    0: ["cold call opener small business owner", "permission based cold call opener",
        "first 30 seconds cold call", "cold call opener restaurant owner",
        "pattern interrupt cold call opener"],
    1: ["cold call problem statement not pitch", "cold call pitch small business",
        "selling software to restaurant owners", "how to pitch restaurant owners",
        "cold call value proposition in one sentence"],
    2: ["discovery questions cold call", "questions to ask restaurant owners sales",
        "bar owner pain points", "restaurant inventory management problems",
        "sales questions without sounding salesy"],
    3: ["cold call objection we already have a system", "not interested cold call response",
        "send me an email objection cold call", "cold call objection too busy",
        "restaurant owner objections sales"],
    4: ["book the meeting cold call", "asking for the meeting cold call",
        "cold call close for free trial", "cold call next step booking"],
    5: ["get past gatekeeper cold call", "cold call voicemail short",
        "cold calling restaurants tips", "calling bars sales tips"],
    "x": ["live cold call small business", "cold calling restaurants live", "hospitality sales tips",
          "cold call tonality confidence", "cold call role play objections",
          "bar owner interview inventory", "restaurant owner hates sales calls",
          "chris voss tactical empathy sales call", "cold call rejection mindset"],
}


# ── scheduling ────────────────────────────────────────────────────────────────

def is_due(now_local: datetime, last_ok: Optional[datetime]) -> bool:
    """True at/after the run hour when the last good refresh is a cycle old.

    The 2-hour slack means a run that landed at 10:05 is still "a cycle old" at
    10:00 three days later, while a run that slipped to 1pm (service asleep)
    pulls the next one to 1pm rather than skipping a cycle.
    """
    if now_local.hour < SCHOOL_RUN_HOUR:
        return False
    if last_ok is None:
        return True
    return now_local - last_ok.astimezone(now_local.tzinfo) >= timedelta(days=SCHOOL_EVERY_DAYS, hours=-2)


def next_run(now_local: datetime, last_ok: Optional[datetime]) -> datetime:
    if last_ok is None:
        base = now_local
    else:
        base = last_ok.astimezone(now_local.tzinfo) + timedelta(days=SCHOOL_EVERY_DAYS, hours=-2)
    at = base.replace(hour=SCHOOL_RUN_HOUR, minute=0, second=0, microsecond=0)
    return at if at >= base else at + timedelta(days=1)


def queries_for(run_no: int) -> list[tuple[object, str]]:
    """Two queries per step plus three wildcards, rotating each run."""
    out = []
    for step, qs in QUERIES.items():
        n = 3 if step == "x" else 2
        for k in range(n):
            out.append((step, qs[(run_no * n + k) % len(qs)]))
    return out


# ── parsing and filtering ─────────────────────────────────────────────────────

def parse_len(text: str) -> Optional[int]:
    try:
        parts = [int(p) for p in str(text).strip().split(":")]
    except ValueError:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs if parts else None


def parse_iso_duration(d: str) -> Optional[int]:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return None
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def fmt_len(secs: int) -> str:
    return f"{secs // 60}:{secs % 60:02d}"


def parse_search_html(html: str) -> list[dict]:
    """Video results from a YouTube results page. Anything unreadable is skipped."""
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out = []

    def text(node) -> str:
        if not isinstance(node, dict):
            return ""
        if "simpleText" in node:
            return node["simpleText"]
        return "".join(r.get("text", "") for r in node.get("runs", []) if isinstance(r, dict))

    def walk(x):
        if isinstance(x, dict):
            v = x.get("videoRenderer")
            if isinstance(v, dict) and v.get("videoId"):
                secs = parse_len(text(v.get("lengthText", {})))
                desc = " ".join(text(s.get("snippetText", {}))
                                for s in v.get("detailedMetadataSnippets", []) if isinstance(s, dict))
                if secs:
                    out.append({"id": v["videoId"], "title": text(v.get("title", {}))[:160],
                                "channel": text(v.get("ownerText", {}))[:80], "secs": secs,
                                "desc": desc[:400]})
            for y in x.values():
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)

    walk(data)
    return out


def filter_candidates(cands: list[dict], recent_ids: set) -> list[dict]:
    seen, out = set(), []
    for c in cands:
        vid = c.get("id")
        if not vid or vid in seen or vid in recent_ids:
            continue
        if not (MIN_SECS <= int(c.get("secs") or 0) <= MAX_SECS):
            continue
        seen.add(vid)
        out.append(c)
    return out


def validate_verdicts(raw, cands_by_id: dict) -> list[dict]:
    """Claude's per-video verdicts, kept only when they're about a real candidate."""
    out = []
    for v in raw if isinstance(raw, list) else []:
        if not isinstance(v, dict) or v.get("id") not in cands_by_id:
            continue
        try:
            rel = int(v.get("relevance"))
        except (TypeError, ValueError):
            continue
        step = v.get("step")
        step = step if step in (0, 1, 2, 3, 4, 5) else "x"
        why, steal = str(v.get("why") or "").strip(), str(v.get("steal") or "").strip()
        if rel < MIN_RELEVANCE or not why or not steal:
            continue
        c = cands_by_id[v["id"]]
        out.append({"id": c["id"], "title": c["title"], "channel": c["channel"],
                    "len": fmt_len(c["secs"]), "secs": c["secs"], "step": step,
                    "relevance": min(10, rel), "why": why[:220], "steal": steal[:220]})
    return out


def assemble(verdicts: list[dict]) -> dict:
    """Best two per step (relevance first, then closest to 10 minutes), a shelf
    of four more, and a pool of short ones (<= 10:30) for the daily video."""
    rank = lambda v: (-v["relevance"], abs(v["secs"] - IDEAL_SECS))
    used, steps = set(), {}
    for s in range(6):
        pick = sorted((v for v in verdicts if v["step"] == s), key=rank)[:PER_STEP]
        steps[str(s)] = pick
        used.update(v["id"] for v in pick)
    rest = sorted((v for v in verdicts if v["id"] not in used), key=rank)
    shelf = rest[:SHELF]
    used.update(v["id"] for v in shelf)
    daily = [v for v in sorted(verdicts, key=rank) if v["secs"] <= 630][:DAILY_POOL]
    return {"steps": steps, "shelf": shelf, "daily": daily}


def validate_gauntlet(raw) -> list[list]:
    """[who, line, [3 options], best_index, why] — the shape the page renders."""
    out = []
    for g in raw if isinstance(raw, list) else []:
        if not isinstance(g, dict):
            continue
        opts = g.get("options")
        try:
            best = int(g.get("best"))
        except (TypeError, ValueError):
            continue
        if not (isinstance(opts, list) and len(opts) == 3 and all(isinstance(o, str) and o.strip() for o in opts)
                and 0 <= best <= 2 and str(g.get("line") or "").strip()):
            continue
        out.append([str(g.get("who") or "Bar owner")[:60], str(g["line"])[:240],
                    [o.strip()[:240] for o in opts], best, str(g.get("why") or "")[:200]])
    return out


def validate_quiz(raw) -> list[list]:
    """[question, [4 options], answer_index, explanation]."""
    out = []
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        opts = q.get("options")
        try:
            ans = int(q.get("answer"))
        except (TypeError, ValueError):
            continue
        if not (isinstance(opts, list) and len(opts) == 4 and all(isinstance(o, str) and o.strip() for o in opts)
                and 0 <= ans <= 3 and str(q.get("question") or "").strip()):
            continue
        out.append([str(q["question"])[:240], [o.strip()[:200] for o in opts], ans,
                    str(q.get("why") or "")[:220]])
    return out


def vet_prompt(cands: list[dict]) -> tuple[str, str]:
    system = ("You are a sales-training researcher curating a video library for ONE person. "
              + USE_CASE + " Be ruthless: most sales videos are generic, hype, or built for "
              "enterprise SaaS, real estate, insurance or agency cold calling that doesn't "
              "transfer. Reply with JSON only.")
    listing = "\n".join(f'- id={c["id"]} | {fmt_len(c["secs"])} | {c["channel"]} | {c["title"]} | {c.get("desc", "")}'
                        for c in cands)
    steps = "; ".join(f"{i}={s}" for i, s in enumerate(STEPS))
    user = (f"Candidates (id | length | channel | title | description):\n{listing}\n\n"
            "Judge each ONLY from this metadata. For every video worth keeping, score relevance "
            "0-10 to the viewer's exact job (10 = directly about calling bar/restaurant owners or "
            "a tactic that clearly transfers to a 60-second call with a busy owner; below 6 = "
            "generic, motivational, or a technique that only works in another industry). Assign "
            f"the step of the call it teaches ({steps}; or \"x\" for general skill like tone, "
            "mindset or full live calls). Write `why`: one sentence on why THIS video helps with "
            "bar owners specifically. Write `steal`: one concrete line or move to try on the next "
            "bar call, adapted to bars (price book, counts, distributor orders, prep time). "
            "Leave out anything you'd score below 6. Return {\"videos\": [{\"id\": \"...\", "
            "\"relevance\": n, \"step\": 0-5 or \"x\", \"why\": \"...\", \"steal\": \"...\"}]}")
    return system, user


def content_prompt() -> tuple[str, str]:
    import coach
    # The product and the asks come from the master sheet via coach.py, so a
    # refreshed pack can't teach a feature 86'd doesn't have or an ask the
    # company doesn't make (it used to drill "15 minutes Tuesday at 2").
    system = ("You write practice material for one cold caller. " + USE_CASE +
              " What they sell, exactly: " + coach.PRODUCT +
              " The asks they make, one per call: " + coach.ASKS_TEXT + ". "
              "States with no tip credit (CA, NV, WA, OR) get a labour-cost angle; tip-credit "
              "states (AZ, TX, CO) lead with time saved and accurate orders. Never write an "
              "answer that promises something the product doesn't do. Reply with JSON only.")
    user = ("Write 10 fresh Gauntlet rounds and 10 fresh test questions, all specific to calling "
            "independent bars. No generic sales trivia. Gauntlet: something an owner, GM or "
            "bartender actually says, and three replies where exactly one is clearly best and the "
            "others are realistic mistakes. Vary which option is best. Test: four options, one "
            "correct. Return {\"gauntlet\": [{\"who\": \"role, 2-4 words\", \"line\": \"what they "
            "say\", \"options\": [\"a\", \"b\", \"c\"], \"best\": 0-2, \"why\": \"one sentence\"}], "
            "\"quiz\": [{\"question\": \"...\", \"options\": [\"a\",\"b\",\"c\",\"d\"], "
            "\"answer\": 0-3, \"why\": \"one sentence\"}]}")
    return system, user


# ── network ───────────────────────────────────────────────────────────────────

UA = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}


def search_youtube(query: str) -> list[dict]:
    import httpx
    key = os.getenv("YOUTUBE_API_KEY")
    if key:
        r = httpx.get("https://www.googleapis.com/youtube/v3/search", timeout=20, params={
            "key": key, "q": query, "part": "id", "type": "video", "maxResults": 15,
            "videoDuration": "medium", "videoEmbeddable": "true", "relevanceLanguage": "en"})
        r.raise_for_status()
        ids = [i["id"]["videoId"] for i in r.json().get("items", []) if i.get("id", {}).get("videoId")]
        if not ids:
            return []
        r = httpx.get("https://www.googleapis.com/youtube/v3/videos", timeout=20, params={
            "key": key, "id": ",".join(ids), "part": "snippet,contentDetails,status"})
        r.raise_for_status()
        out = []
        for v in r.json().get("items", []):
            secs = parse_iso_duration(v.get("contentDetails", {}).get("duration", ""))
            if secs and v.get("status", {}).get("embeddable", False):
                sn = v.get("snippet", {})
                out.append({"id": v["id"], "title": sn.get("title", "")[:160],
                            "channel": sn.get("channelTitle", "")[:80], "secs": secs,
                            "desc": re.sub(r"\s+", " ", sn.get("description", ""))[:400],
                            "embed_checked": True})
        return out
    r = httpx.get("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query),
                  headers=UA, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return parse_search_html(r.text)[:15]


def embeddable(video_id: str) -> bool:
    import httpx
    try:
        r = httpx.get("https://www.youtube.com/oembed", timeout=10, params={
            "url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
        return r.status_code == 200
    except Exception:
        return False


def build_pack(run_no: int, recent_ids: set, ask) -> dict:
    """Everything a refresh produces. `ask(system, user, max_tokens)` -> dict is
    injected so tests can run this without Claude or a database."""
    log = []
    cands, taken = [], set(recent_ids)
    for step, q in queries_for(run_no):
        # At most 6 per query, so every step of the call gets candidates
        # instead of the first few queries filling the whole list.
        try:
            found = filter_candidates(search_youtube(q), taken)
        except Exception as exc:
            log.append(f"{q}: failed ({str(exc)[:60]})")
            continue
        kept = [c for c in found if c.get("embed_checked") or embeddable(c["id"])][:6]
        taken.update(c["id"] for c in kept)
        cands.extend(kept)
        log.append(f"{q}: {len(found)} in range, {len(kept)} kept")
    cands = cands[:90]
    by_id = {c["id"]: c for c in cands}

    verdicts = []
    for i in range(0, len(cands), 30):         # batches keep each prompt readable
        system, user = vet_prompt(cands[i:i + 30])
        try:
            verdicts += validate_verdicts(ask(system, user, 3500).get("videos"), by_id)
        except Exception as exc:
            log.append(f"vetting batch {i // 30 + 1} failed: {str(exc)[:80]}")
    videos = assemble(verdicts)

    gauntlet, quiz = [], []
    try:
        system, user = content_prompt()
        out = ask(system, user, 4000)
        gauntlet, quiz = validate_gauntlet(out.get("gauntlet")), validate_quiz(out.get("quiz"))
    except Exception as exc:
        log.append(f"content failed: {str(exc)[:80]}")

    return {"run": run_no, "videos": videos, "gauntlet": gauntlet, "quiz": quiz,
            "stats": {"searched": len(queries_for(run_no)), "candidates": len(cands),
                      "kept": len(verdicts)}, "log": log}


def pack_is_usable(pack: dict) -> bool:
    """A pack has to beat the built-in content to replace it."""
    v = pack.get("videos", {})
    filled = sum(1 for s in v.get("steps", {}).values() if s)
    return filled >= 4 and len(v.get("daily", [])) >= 3


# ── database ──────────────────────────────────────────────────────────────────

def init_school_tables():
    from database import get_db
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_school_packs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                ok BOOLEAN NOT NULL DEFAULT FALSE,
                pack JSONB,
                error TEXT
            )""")
        conn.commit()
    print("[school] SCHOOL_TABLES_READY", flush=True)


def _last_ok(cursor) -> Optional[datetime]:
    cursor.execute("SELECT started_at FROM crm_school_packs WHERE ok = TRUE ORDER BY started_at DESC LIMIT 1")
    row = cursor.fetchone()
    return row["started_at"] if row else None


def refresh_if_due(force: bool = False) -> Optional[dict]:
    from database import get_db
    now = datetime.now(ZoneInfo(SCHOOL_TZ))
    with get_db() as conn:
        cursor = conn.cursor()
        if not force and not is_due(now, _last_ok(cursor)):
            return None
        # A run started in the last 20 minutes is still going (or died mid-way):
        # don't stack a second one on top of it.
        cursor.execute("SELECT COUNT(*) AS n FROM crm_school_packs WHERE finished_at IS NULL "
                       "AND started_at > NOW() - INTERVAL '20 minutes'")
        if cursor.fetchone()["n"]:
            return None
        cursor.execute("SELECT COUNT(*) AS n FROM crm_school_packs")
        run_no = cursor.fetchone()["n"]
        cursor.execute("SELECT pack FROM crm_school_packs WHERE ok = TRUE ORDER BY started_at DESC LIMIT 4")
        recent = set()
        for r in cursor.fetchall():
            p = r["pack"] if isinstance(r["pack"], dict) else json.loads(r["pack"] or "{}")
            v = p.get("videos", {})
            for lst in list(v.get("steps", {}).values()) + [v.get("shelf", []), v.get("daily", [])]:
                recent.update(x.get("id") for x in lst or [])
        cursor.execute("INSERT INTO crm_school_packs (started_at) VALUES (NOW()) RETURNING id")
        pack_id = cursor.fetchone()["id"]
        conn.commit()

    print(f"[school] refresh #{run_no} starting (avoiding {len(recent)} recent videos)", flush=True)
    from crm import _ask_claude

    def ask(system, user, max_tokens):
        return _ask_claude(system, user, max_tokens=max_tokens, timeout=120.0)

    ok, pack, error = False, None, None
    try:
        pack = build_pack(run_no, recent, ask)
        ok = pack_is_usable(pack)
        if not ok:
            # Too few fresh keepers (e.g. search blocked): retry without the
            # "not recently shown" rule before giving up on the cycle.
            pack = build_pack(run_no, set(), ask)
            ok = pack_is_usable(pack)
            error = None if ok else "not enough relevant videos found"
    except Exception as exc:
        error = str(exc)[:400]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE crm_school_packs SET finished_at = NOW(), ok = %s, pack = %s, error = %s "
                       "WHERE id = %s", (ok, json.dumps(pack) if pack else None, error, pack_id))
        conn.commit()
    print(f"[school] refresh #{run_no} {'SCHOOL_REFRESH_OK' if ok else 'SCHOOL_REFRESH_FAILED'} "
          f"{(pack or {}).get('stats')} {error or ''}", flush=True)
    return pack


def latest_pack() -> dict:
    from database import get_db
    now = datetime.now(ZoneInfo(SCHOOL_TZ))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, started_at, pack FROM crm_school_packs WHERE ok = TRUE "
                       "ORDER BY started_at DESC LIMIT 1")
        row = cursor.fetchone()
        cursor.execute("SELECT started_at, finished_at, ok, error FROM crm_school_packs "
                       "ORDER BY started_at DESC LIMIT 1")
        last = cursor.fetchone()
    pack = None
    if row:
        pack = row["pack"] if isinstance(row["pack"], dict) else json.loads(row["pack"] or "{}")
        pack.pop("log", None)
    last_ok = row["started_at"] if row else None
    return {
        "pack": pack, "pack_id": row["id"] if row else None,
        "updated_at": last_ok.isoformat() if last_ok else None,
        "next_at": next_run(now, last_ok).isoformat(),
        "running": bool(last and last["finished_at"] is None),
        "last_error": (last or {}).get("error") if last and not last["ok"] and last["finished_at"] else None,
        "tz": SCHOOL_TZ,
    }
