"""The company brain, pure: what the owner tells the AI, and what the AI has
learned from the calls, emails and replies in the log.

Two parts, kept apart on purpose:

- OWNER'S STANDING INSTRUCTIONS: typed by the owner on the AI Brain page and
  stored in crm_ai_brain. The owner is the authority on his own business, so
  facts in here may be stated and instructions followed ("don't lead with
  price", "I'm free for demos Tuesday and Thursday").
- THE PLAYBOOK: written by the model from the log, refreshed about once a day
  by main.py's _playbook_loop. Patterns only, never product facts: what
  objections come up, what gets a callback, who decides, how bars count today,
  which emails get replies. Every point must cite the bars it came from, and
  `clean()` drops any point whose evidence isn't a bar actually in the log —
  a "learning" nobody can trace back to a call is how a playbook fills up with
  generic sales advice and made-up patterns.

The drafter, the prep sheet and the School read both (crm._knowledge()). The
master sheet (pitch.py) stays the only source of product facts. Covered by
test_playbook.py.
"""
import re
from typing import Optional

MAX_SECTIONS = 6
MAX_POINTS = 6
POINT_CHARS = 320
OWNER_NOTES_CHARS = 8000

SECTION_TITLES = (
    "Objections we hear",
    "What gets a callback or a yes",
    "Who decides and when to reach them",
    "How bars do it today",
    "Emails that get replies",
    "Stop doing",
)

SYSTEM = """You are the sales analyst for 86'd, a small startup selling an iPhone app for bar inventory and distributor ordering to independent US bars and restaurants. The founder makes the calls and sends the emails himself. You read everything logged in the CRM and write down what the log actually shows, so the tools that prepare calls and write emails can use it.

You are given the LOG: one block per bar that has been worked (name, town, stage, last outcome, and its dated notes: call summaries, the founder's own words after "Your notes:", and labelled details such as Objection, How they do it now, Best time, Spoke to). Then REPLIES that came in by email, and EMAILS WE SENT with whether a reply came back.

Write the playbook as sections, using these titles and skipping any the log gives you nothing for:
- "Objections we hear": each objection, how many bars raised it, and the answer that worked in the log, or one that honestly fits.
- "What gets a callback or a yes"
- "Who decides and when to reach them"
- "How bars do it today"
- "Emails that get replies"
- "Stop doing"

Rules:
1. Only what the log shows. Every point lists in "evidence" the bars it came from, spelled exactly as in the LOG. No generic sales advice and no invented numbers.
2. Count honestly ("3 bars", "1 bar"). One bar is an anecdote; say so.
3. Each point is one or two short sentences. At most 6 points per section.
4. If the log is thin, write less. A missing section beats a padded one.
5. "summary": two sentences on where things stand and the single most useful thing to change."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "sections": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "points": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["text", "evidence"],
                    "additionalProperties": False,
                }},
            },
            "required": ["title", "points"],
            "additionalProperties": False,
        }},
    },
    "required": ["summary", "sections"],
    "additionalProperties": False,
}


def _one_line(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def digest(leads: list, replies: list, emails: list, today: str,
           notes_chars: int = 1500) -> str:
    """The log as the model reads it.

    `leads`: worked leads (name, loc, status, last_outcome, notes).
    `replies`: {"from", "subject", "about", "said"} — what came in by email.
    `emails`: {"lead", "subject", "replied"} — what we sent and whether a
    reply came back. The newest part of each lead's notes is kept; that's
    where the calls are.
    """
    out = [f"TODAY: {today}", "", "LOG"]
    for lead in leads:
        notes = (lead.get("notes") or "").strip()
        if len(notes) > notes_chars:
            notes = "…" + notes[-notes_chars:]
        head = f"## {_one_line(lead.get('name'), 120)}"
        if lead.get("loc"):
            head += f" ({_one_line(lead['loc'], 60)})"
        head += f" | stage {lead.get('status') or '?'}"
        if lead.get("last_outcome"):
            head += f" | last outcome {lead['last_outcome']}"
        out += [head, notes or "(no notes)", ""]
    if replies:
        out.append("REPLIES")
        for r in replies:
            out.append(f"- {_one_line(r.get('from'), 80)} about {_one_line(r.get('about'), 120)}: "
                       f"{_one_line(r.get('said'), 400)}")
        out.append("")
    if emails:
        got = sum(1 for e in emails if e.get("replied"))
        out.append(f"EMAILS WE SENT ({len(emails)}, {got} got a reply)")
        for e in emails:
            out.append(f"- to {_one_line(e.get('lead'), 120)}: \"{_one_line(e.get('subject'), 150)}\""
                       f" — {'REPLIED' if e.get('replied') else 'no reply'}")
    return "\n".join(out).strip()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def clean(out: dict, known_names) -> dict:
    """What the model wrote, minus anything the log can't back.

    A point survives only with at least one evidence name that matches a bar
    in the log (normalised, and either containing the other: "Olde Town" for
    "Olde Town Tavern & Grill"). Unknown evidence names are dropped from the
    point; a point left with none is dropped entirely.
    """
    known = {_norm(n): n for n in known_names if _norm(n)}

    def match(name: str) -> Optional[str]:
        k = _norm(name)
        if not k:
            return None
        if k in known:
            return known[k]
        for kk, original in known.items():
            if len(k) >= 4 and (k in kk or kk in k):
                return original
        return None

    sections = []
    for sec in (out.get("sections") or [])[:MAX_SECTIONS * 2]:
        if not isinstance(sec, dict):
            continue
        title = _one_line(sec.get("title"), 80)
        points = []
        for p in sec.get("points") or []:
            if not isinstance(p, dict):
                continue
            text = _one_line(p.get("text"), POINT_CHARS)
            evidence = []
            for e in p.get("evidence") or []:
                m = match(str(e))
                if m and m not in evidence:
                    evidence.append(m)
            if text and evidence:
                points.append({"text": text, "evidence": evidence[:8]})
            if len(points) >= MAX_POINTS:
                break
        if title and points:
            sections.append({"title": title, "points": points})
        if len(sections) >= MAX_SECTIONS:
            break
    return {"summary": _one_line(out.get("summary"), 600), "sections": sections}


def render(pb: Optional[dict], refreshed_at: Optional[str] = None) -> str:
    """The playbook as other prompts read it. Evidence becomes a count, not
    names: an email to one bar must never name another."""
    if not pb or not pb.get("sections"):
        return ""
    when = f" (updated {refreshed_at[:10]})" if refreshed_at else ""
    lines = [f"WHAT WE'VE LEARNED FROM REAL CALLS{when} — patterns from the log, "
             "never product facts; use them to choose an angle, not to make claims:"]
    if pb.get("summary"):
        lines.append(pb["summary"])
    for sec in pb["sections"]:
        lines.append(f"{sec['title']}:")
        for p in sec["points"]:
            n = len(p.get("evidence") or [])
            lines.append(f"- {p['text']} [{n} bar{'s' if n != 1 else ''}]")
    return "\n".join(lines)


def render_owner(notes: Optional[str]) -> str:
    notes = (notes or "").strip()
    if not notes:
        return ""
    return ("OWNER'S STANDING INSTRUCTIONS (the owner wrote these: follow them, and any fact "
            "in them is true and may be used):\n" + notes[:OWNER_NOTES_CHARS])
