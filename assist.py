"""The Follow-ups tab's AI bar: plain English in, CRM changes out.

Pure — no database, no network, no clock — so everything that decides what
gets written can be tested directly: the prompt, the output schema, the date
table the model resolves "Friday" against, the snapshot of the book it reads,
and `clean_change()`, the gate between what the model proposed and what is
saved. crm.py owns the route, the model call and the writes.

The model proposes; this module disposes. Every name, phone number and email
it wants to write must appear in what the operator typed (a contact may also
come from that lead's own notes). A model filling a blank with a plausible
guess is the failure that matters most in a CRM, because nobody notices until
they ring a number, or ask for a person, that was never real.
"""
import re
from datetime import date, timedelta
from typing import Optional

STATUSES = ("new", "contacted", "warm", "won", "dead")
OUTCOMES = ("answered", "voicemail", "no_answer", "gatekeeper", "callback",
            "not_interested")
KINDS = ("call", "email", "fb")
MAX_CHANGES = 50
FIELD_LIMITS = {"name": 200, "loc": 200, "contact": 200, "phone": 50, "email": 320}
NOTE_LIMIT = 1000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


# Structured outputs: the API guarantees the reply parses against this, so
# the code below never has to guess at a shape. Every property is required
# and nullable rather than optional — null means "not changing this".
SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "question": _nullable({"type": "string"}),
        "changes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "lead": {"type": "string"},
                "name": _nullable({"type": "string"}),
                "loc": _nullable({"type": "string"}),
                "status": _nullable({"type": "string", "enum": list(STATUSES)}),
                "contact": _nullable({"type": "string"}),
                "phone": _nullable({"type": "string"}),
                "email": _nullable({"type": "string"}),
                "followup_date": _nullable({"type": "string", "format": "date"}),
                "clear_followup": {"type": "boolean"},
                "note": _nullable({"type": "string"}),
                "logged": _nullable({
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "outcome": _nullable({"type": "string", "enum": list(OUTCOMES)}),
                        "summary": {"type": "string"},
                        "their_words": {"type": "string"},
                    },
                    "required": ["kind", "outcome", "summary", "their_words"],
                    "additionalProperties": False,
                }),
            },
            "required": ["lead", "name", "loc", "status", "contact", "phone", "email",
                         "followup_date", "clear_followup", "note", "logged"],
            "additionalProperties": False,
        }},
    },
    "required": ["reply", "question", "changes"],
    "additionalProperties": False,
}


SYSTEM = """You keep a salesperson's CRM up to date from what they tell you in plain English. They sell 86'd, an iPhone app that counts bar inventory, to independent bars and restaurants, mostly by cold-calling, and work from a Follow-ups list of bars to get back to.

You are given DATES (today and the two weeks after it, with weekdays), LEADS (every lead in the book), sometimes EARLIER turns of this conversation, and their NEW MESSAGE. Act on the new message. Earlier turns are only context for words like "her", "that one", "yes, the Denver one".

For each lead the message is about, return one entry in "changes". Use null for every field you are not changing.
- name, loc ("City, ST"), contact (the person to ask for), phone, email.
- status: new = never contacted; contacted = reached out, no real interest yet; warm = interested, a callback agreed or a demo booked; won = signed up; dead = said no, already has a system or an app, or asked not to be called again.
- followup_date: the next day to get back to them, YYYY-MM-DD. clear_followup: true removes the follow-up date entirely; otherwise false.
- note: anything else worth keeping, as one line: what they said, a personal detail, a time they gave ("call after 3pm"), a promise made.
- logged: ONLY when the message says a call, email or Facebook message HAPPENED ("called", "left a voicemail", "no answer", "emailed him"). kind; outcome for a call: answered = a person picked up and spoke, voicemail, no_answer = rang out with no message left, gatekeeper = staff answered and the decision maker wasn't in, callback = they asked to be called back or agreed a next step, not_interested = said no or already has a system; a one-sentence summary; their_words = the exact part of the message about this lead, copied word for word. Logging counts as a try and books the next call on its own when nobody was reached, so never log a contact the message doesn't say happened.

Rules:
1. Never invent. Every name, phone number and email you write must appear in the message, or, for a contact, in that lead's own row. If they didn't give it, leave it null.
2. Match a lead by its name, part of it, a nickname, its town, its contact or its phone number. A message that names no lead is about the lead marked OPEN ON SCREEN, if there is one. If more than one lead could fit, change nothing for that part and ask in "question", naming the candidates with their towns.
3. Dates come from DATES only. A weekday means the next one after today; "tomorrow" is the next day; "next week" with no day is 7 days from today; never a date before today.
4. Said no, not interested, already has a system or an app, don't call again -> status dead and clear_followup true. Interested, wants a demo, asked for a callback -> status warm, and the follow-up date they gave.
5. One message can cover many leads. "Everything overdue" or "all of today's" means the leads whose LIST column says overdue or due today.
6. A question that asks nothing to change gets an answer in reply, from LEADS, and no changes.
7. reply: one or two plain sentences saying exactly what you changed, or answering them. No filler, no exclamation marks.
8. Change only what the message asks for. Never change a field just because you could."""


def dates_table(today: date, days: int = 14) -> str:
    """Today and the next two weeks, spelled out.

    Handing the model the calendar is cheaper and more reliable than asking
    it to count weekdays: "Friday" becomes a lookup, not arithmetic.
    """
    lines = []
    for i in range(days + 1):
        d = today + timedelta(days=i)
        tag = " (today)" if i == 0 else " (tomorrow)" if i == 1 else ""
        lines.append(f"{d.isoformat()} {d.strftime('%A')}{tag}")
    return "\n".join(lines)


def list_of(lead: dict, today: str) -> str:
    """Which Follow-ups list a lead is on — the same rule as /queue."""
    due = lead.get("followup_date")
    if not due or lead.get("status") in ("won", "dead"):
        return ""
    if due < today:
        return "overdue"
    if due == today:
        return "due today"
    return ""


def _clip(v, n: int) -> str:
    v = re.sub(r"\s+", " ", str(v or "")).replace("|", "/").strip()
    return v[:n]


def _tries_text(t: Optional[dict]) -> str:
    if not t or not t.get("total"):
        return "0"
    parts = [f"{t[k]} {w}" for k, w in (("call", "call"), ("email", "email"),
                                         ("fb", "message")) if t.get(k)]
    return f"{t['total']} ({', '.join(parts)})" if parts else str(t["total"])


def snapshot(leads: list, tries: dict, today: str,
             focus_id: Optional[str] = None) -> tuple[str, dict]:
    """The book as the model sees it, plus alias -> lead id.

    Short aliases (L1, L2...) instead of ids. Follow-ups first, then worked
    leads, then never-contacted ones, which get no note: theirs is the lead
    generator's bookkeeping and costs tokens without helping anyone match
    "the Denver one".
    """
    def rank(lead):
        on_list = list_of(lead, today)
        if on_list:
            return 0
        return 1 if lead.get("last_touch_at") else 2

    ordered = sorted(leads, key=rank)   # stable: keeps the caller's order within a rank
    back: dict = {}
    lines = ["LEADS (alias | name | where | stage | contact | phone | email | follow-up | "
             "list | last outcome | tries | last touched | latest note)"]
    for i, lead in enumerate(ordered, 1):
        alias = f"L{i}"
        back[alias] = lead["id"]
        worked = bool(lead.get("last_touch_at")) or lead.get("status") != "new"
        notes = [n for n in (lead.get("notes") or "").splitlines() if n.strip()]
        row = " | ".join([
            alias, _clip(lead.get("name"), 80), _clip(lead.get("loc"), 40),
            lead.get("status") or "", _clip(lead.get("contact"), 60),
            _clip(lead.get("phone"), 30), _clip(lead.get("email"), 80),
            lead.get("followup_date") or "", list_of(lead, today),
            lead.get("last_outcome") or "", _tries_text(tries.get(lead["id"])),
            (lead.get("last_touch_at") or "")[:10],
            _clip(notes[-1], 240) if (worked and notes) else "",
        ])
        if focus_id and lead["id"] == focus_id:
            row += " | <- OPEN ON SCREEN"
        lines.append(row)
    return "\n".join(lines), back


def user_message(book: str, dates: str, text: str, history: list) -> str:
    parts = ["DATES", dates, "", book]
    if history:
        parts += ["", "EARLIER IN THIS CONVERSATION (context only)"]
        for turn in history:
            parts.append(f"Them: {_clip(turn.get('you'), 1000)}")
            parts.append(f"You: {_clip(turn.get('ai'), 1000)}")
    parts += ["", f"NEW MESSAGE: {text.strip()}"]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The gate between what the model proposed and what gets saved.

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def grounded(value: str, *sources: str) -> bool:
    """Whether `value` actually appears in one of `sources`, ignoring case
    and spacing. The anti-invention check for names, towns and emails."""
    v = _norm(value)
    return bool(v) and any(v in _norm(s) for s in sources if s)


def phone_grounded(value: str, *sources: str) -> bool:
    """Same idea for a phone number, compared as digits so "(720) 242-9667"
    in the message vouches for "720-242-9667" in the reply."""
    d = _digits(value)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return len(d) >= 7 and any(d in _digits(s) for s in sources if s)


def words_from(text: str, excerpt: str) -> str:
    """The operator's own words for one lead: the model's excerpt if it
    really is a piece of what they typed, otherwise all of it. The verbatim
    copy on a logged call must never be a paraphrase."""
    if excerpt and _norm(excerpt) in _norm(text):
        return excerpt.strip()
    return text.strip()


def clean_change(change: dict, lead: dict, text: str, today: date) -> tuple[dict, list]:
    """What of one proposed change may be written, and what was refused.

    Returns ({field: value}, [reasons]). A value equal to what the lead
    already holds is dropped as a no-op rather than reported. A followup_date
    of None in the result means "clear it".
    """
    out: dict = {}
    problems: list = []
    own = " ".join(str(lead.get(k) or "") for k in ("notes", "contact", "manager_name"))

    def fresh(field, value):
        return (value or "") != (lead.get(field) or "")

    status = change.get("status")
    if status is not None:
        if status in STATUSES:
            if fresh("status", status):
                out["status"] = status
        else:
            problems.append(f"stage {status!r} isn't one the CRM has")

    for field in ("name", "loc", "contact"):
        value = change.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        value = re.sub(r"\s+", " ", value).strip()[:FIELD_LIMITS[field]]
        sources = (text, own) if field == "contact" else (text,)
        if not grounded(value, *sources):
            problems.append(f"{field} {value!r} isn't in what you typed, so I left it")
        elif fresh(field, value):
            out[field] = value

    phone = change.get("phone")
    if isinstance(phone, str) and phone.strip():
        phone = phone.strip()[:FIELD_LIMITS["phone"]]
        if not phone_grounded(phone, text):
            problems.append(f"phone {phone!r} isn't in what you typed, so I left it")
        elif _digits(phone) != _digits(lead.get("phone") or ""):
            out["phone"] = phone

    email = change.get("email")
    if isinstance(email, str) and email.strip():
        email = email.strip()[:FIELD_LIMITS["email"]]
        if not EMAIL_RE.match(email):
            problems.append(f"{email!r} isn't an email address")
        elif not grounded(email, text):
            problems.append(f"email {email!r} isn't in what you typed, so I left it")
        elif email.lower() != (lead.get("email") or "").lower():
            out["email"] = email

    when = change.get("followup_date")
    if isinstance(when, str) and when.strip():
        try:
            d = date.fromisoformat(when.strip()[:10])
        except ValueError:
            problems.append(f"{when!r} isn't a date")
        else:
            if d < today:
                problems.append(f"{d.isoformat()} is in the past, so the follow-up stays")
            elif d > today + timedelta(days=365):
                problems.append(f"{d.isoformat()} is more than a year out")
            elif fresh("followup_date", d.isoformat()):
                out["followup_date"] = d.isoformat()
    elif change.get("clear_followup") and lead.get("followup_date"):
        out["followup_date"] = None

    note = change.get("note")
    if isinstance(note, str) and note.strip():
        out["note"] = re.sub(r"\s+", " ", note).strip()[:NOTE_LIMIT]

    logged = change.get("logged")
    if isinstance(logged, dict) and logged.get("kind") in KINDS:
        outcome = logged.get("outcome")
        out["logged"] = {
            "kind": logged["kind"],
            "outcome": outcome if outcome in OUTCOMES else None,
            "summary": re.sub(r"\s+", " ", str(logged.get("summary") or "")).strip()[:2000],
            "their_words": words_from(text, str(logged.get("their_words") or "")),
        }
    return out, problems


def describe(clean: dict) -> list:
    """What changed, in words, for the screen and the lead's history line."""
    said = []
    labels = {"name": "name", "loc": "town", "contact": "contact", "phone": "phone",
              "email": "email", "status": "stage"}
    if "logged" in clean:
        lg = clean["logged"]
        what = {"call": "call", "email": "email", "fb": "Facebook message"}[lg["kind"]]
        said.append(f"logged {what}" + (f" ({lg['outcome'].replace('_', ' ')})"
                                        if lg.get("outcome") else ""))
    for field, label in labels.items():
        if field in clean:
            said.append(f"{label} → {clean[field]}")
    if "followup_date" in clean:
        said.append(f"follow-up → {clean['followup_date']}" if clean["followup_date"]
                    else "follow-up cleared")
    if "note" in clean:
        said.append("note added")
    return said
