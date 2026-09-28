"""CloudTalk: the calls the operator makes, their transcripts, and a score.

Read from CloudTalk's REST API v1.7 (developers.cloudtalk.io, checked
2026-09-28): HTTP Basic auth with an API Access Key ID + Secret on every host.
  - Call history:   GET https://my.cloudtalk.io/api/calls/index.json
                    (date_from / date_to "YYYY-MM-DD HH:MM:SS" UTC, limit, page;
                    `responseData.data[].Cdr`: id, type, public_external,
                    started_at, answered_at, ended_at, talking_time, billsec)
  - Transcription:  GET https://api.cloudtalk.io/v1/ai/calls/{id}/transcription
                    (limit/offset; `data.segments[]` {start, end, caller, text},
                    `data.callers[]` {type agent|contact, localIdentifier})
  - Summary:        GET https://api.cloudtalk.io/v1/ai/calls/{id}/summary
                    ({callId, summary})
Transcripts and summaries are CloudTalk's Conversation Intelligence: a plan
without it answers 403/404, and calls are then logged and matched but not
scored. Rate limit: 60 requests a minute per company, so a pass is capped.

The network half is `fetch_*`; everything else is pure and tested without
CloudTalk (test_cloudtalk.py). crm.process_cloudtalk runs it.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

CORE = os.getenv("CLOUDTALK_API_BASE", "https://my.cloudtalk.io/api").rstrip("/")
AI = os.getenv("CLOUDTALK_AI_BASE", "https://api.cloudtalk.io/v1").rstrip("/")
PAGE_LIMIT = 100
MAX_PAGES = 5
MIN_TALK_SECONDS = 20          # shorter than this there's nothing to score
TRANSCRIPT_TRIES = 12          # ~2 hours at one look every 10 minutes


def _creds() -> Optional[tuple]:
    key, secret = os.getenv("CLOUDTALK_KEY_ID"), os.getenv("CLOUDTALK_KEY_SECRET")
    return (key, secret) if key and secret else None


def configured() -> bool:
    return _creds() is not None


def _get(url: str, params: Optional[dict] = None, timeout: float = 20.0):
    import httpx
    return httpx.get(url, params=params, auth=_creds(), timeout=timeout,
                     headers={"Accept": "application/json"})


# ── pure: reading what CloudTalk returns ────────────────────────────────────

def _utc(value) -> Optional[str]:
    """CloudTalk's timestamps ("2017-10-04T06:33:37.000Z" or
    "2018-01-10 12:34:56", both UTC) as ISO UTC, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_calls(body: dict) -> tuple[list, int]:
    """(calls, page count) from a call-history page. Each call:
    {call_id, direction, number (10 digits), started_at, ended_at,
    talk_seconds, recorded}. Rows without an id or a number are dropped."""
    data = (body or {}).get("responseData") or {}
    out = []
    for item in data.get("data") or []:
        cdr = (item or {}).get("Cdr") or {}
        digits = re.sub(r"\D", "", str(cdr.get("public_external") or ""))[-10:]
        if not cdr.get("id") or len(digits) != 10:
            continue
        out.append({
            "call_id": str(cdr["id"]),
            "direction": str(cdr.get("type") or ""),
            "number": digits,
            "started_at": _utc(cdr.get("started_at")),
            "ended_at": _utc(cdr.get("ended_at")),
            "talk_seconds": _int(cdr.get("talking_time") or cdr.get("billsec")),
            "recorded": bool(cdr.get("recorded")),
        })
    return out, _int(data.get("pageCount")) or 1


def transcript_text(data: dict, agent: str = "Stephan", contact: str = "Bar") -> str:
    """Segments as "Stephan: …" / "Bar: …" lines, consecutive lines by the
    same speaker joined. Empty when there's nothing said."""
    who = {}
    for c in (data or {}).get("callers") or []:
        if isinstance(c, dict) and c.get("localIdentifier"):
            who[c["localIdentifier"]] = agent if c.get("type") == "agent" else contact
    lines: list = []
    for seg in sorted(((data or {}).get("segments") or []),
                      key=lambda s: float(s.get("start") or 0) if isinstance(s, dict) else 0):
        if not isinstance(seg, dict):
            continue
        text = re.sub(r"\s+", " ", str(seg.get("text") or "")).strip()
        if not text:
            continue
        speaker = who.get(seg.get("caller"), contact)
        if lines and lines[-1][0] == speaker:
            lines[-1][1] += " " + text
        else:
            lines.append([speaker, text])
    return "\n".join(f"{s}: {t}" for s, t in lines)


# ── pure: the score ─────────────────────────────────────────────────────────
#
# Four parts of a cold call to a bar, 0-25 each, the same four the School
# drills (coach.SCRIPT_SKILLS): the opener, discovery, handling pushback, and
# the ask — judged against the company's real asks (coach.ASKS). A call that
# never reached a conversation (wrong number, straight to voicemail, a hang-up
# in the first line) isn't scored at all: a 20/100 for a voicemail would drag
# the average down for something no rep could have done better.

PARTS = ("opener", "discovery", "objections", "ask")

SCORE_RUBRIC = """SCORE THE REP (Stephan), 0-25 for each part, only from what the transcript shows:
- opener: said who he is and why he's calling in the first few lines, in plain words, and
  earned the next thirty seconds. 0 if it was a script dump or an easy exit ("is this a bad time?").
- discovery: asked how they count and order today, listened, found a real pain (the count
  eats a night, running out of a top seller, orders typed at 1am). 0 if he never asked.
- objections: acknowledged the pushback, answered honestly from what 86'd really does,
  never argued or badmouthed their current tool. If there was no pushback, score how well
  he checked for it (12 if he never had to).
- ask: made ONE clear ask from the company's asks and got an answer — the trial on the next
  count, a short call with the founder, or (with a gatekeeper) the decision maker's name and
  when they're in. 0 if the call ended with "let me know" or no ask.
"scorable": false when no real conversation happened (voicemail, wrong number, hung up in
the first line) — then the parts are 0 and nothing is scored.
"did_well": one sentence, the best thing he did, specific to this call.
"fix": one sentence, the one thing to do differently next time, specific to this call.
"moment": the line where the call turned, copied WORD FOR WORD from the transcript."""


def clean_score(out: dict, transcript: str) -> Optional[dict]:
    """The model's grading, checked: each part clamped 0-25, the total
    computed here (never the model's), and the "moment" kept only if it is
    really in the transcript. None when the call isn't scorable."""
    if not isinstance(out, dict) or not out.get("scorable"):
        return None
    parts = {}
    for p in PARTS:
        try:
            parts[p] = max(0, min(25, int(round(float(out.get(p) or 0)))))
        except (TypeError, ValueError):
            parts[p] = 0
    flat = re.sub(r"\s+", " ", transcript or "").lower()
    moment = re.sub(r"\s+", " ", str(out.get("moment") or "")).strip()
    if moment and moment.lower().strip(" .\"'") not in flat:
        moment = ""
    return {"score": sum(parts.values()), "parts": parts,
            "did_well": str(out.get("did_well") or "").strip()[:240],
            "fix": str(out.get("fix") or "").strip()[:240],
            "moment": moment[:300]}


SCORE_SCHEMA_PROPS = {
    "scorable": {"type": "boolean"},
    **{p: {"type": "integer"} for p in PARTS},
    "did_well": {"type": "string"},
    "fix": {"type": "string"},
    "moment": {"type": "string"},
}


def score_line(score: Optional[dict]) -> str:
    """How a score reads in the lead's notes (and so to the playbook and the
    School, which learn from the notes)."""
    if not score:
        return ""
    p = score["parts"]
    bits = [f"Call score {score['score']}/100 (opener {p['opener']}, discovery {p['discovery']}, "
            f"objections {p['objections']}, ask {p['ask']})"]
    if score.get("did_well"):
        bits.append(f"Did well: {score['did_well']}")
    if score.get("fix"):
        bits.append(f"Next time: {score['fix']}")
    return " · ".join(bits)


def minutes(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds or 0)), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


# ── network ─────────────────────────────────────────────────────────────────

def fetch_calls(date_from: datetime, date_to: datetime) -> list:
    """Every call in the window, oldest first. Raises on an auth failure so
    the operator hears about a wrong key; a page that fails otherwise ends
    the listing (the next pass picks the window up again)."""
    fmt = "%Y-%m-%d %H:%M:%S"
    calls: list = []
    for page in range(1, MAX_PAGES + 1):
        resp = _get(f"{CORE}/calls/index.json", {
            "date_from": date_from.astimezone(timezone.utc).strftime(fmt),
            "date_to": date_to.astimezone(timezone.utc).strftime(fmt),
            "limit": PAGE_LIMIT, "page": page})
        if resp.status_code == 401:
            raise PermissionError("CloudTalk rejected the API key (401)")
        if resp.status_code != 200:
            print(f"[cloudtalk] CALLS HTTP {resp.status_code}: {resp.text[:160]}", flush=True)
            break
        batch, pages = parse_calls(resp.json())
        calls += batch
        if page >= pages:
            break
    calls.sort(key=lambda c: c["started_at"] or "")
    return calls


def fetch_transcript(call_id: str) -> tuple[Optional[dict], int]:
    """(the transcription `data` with every segment, HTTP status). None
    until CloudTalk has one — or ever, on a plan without Conversation
    Intelligence (403/404)."""
    data, offset = None, 0
    for _ in range(10):
        resp = _get(f"{AI}/ai/calls/{call_id}/transcription", {"limit": 500, "offset": offset})
        if resp.status_code != 200:
            return data, resp.status_code
        body = resp.json()
        page = body.get("data") or {}
        if data is None:
            data = page
        else:
            data["segments"] = (data.get("segments") or []) + (page.get("segments") or [])
        total = _int((body.get("pagination") or {}).get("total"))
        offset += len(page.get("segments") or [])
        if not page.get("segments") or offset >= total:
            break
    return data, 200


def fetch_summary(call_id: str) -> Optional[str]:
    try:
        resp = _get(f"{AI}/ai/calls/{call_id}/summary")
        if resp.status_code == 200:
            return str(resp.json().get("summary") or "").strip()[:4000] or None
    except Exception as exc:
        print(f"[cloudtalk] SUMMARY {call_id}: {exc}", flush=True)
    return None


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
