"""Sales CRM — leads and activity counters.

Deliberately self-contained: its own tables (`crm_leads`, `crm_counters`), its
own Pydantic models, its own auth. Nothing here touches the inventory, scan,
order or billing tables, and nothing in those paths reads from here — the CRM
is an internal sales tool that happens to share a process and a database with
the product API, not part of the product.

Auth is a single shared key in the `X-CRM-Key` header, checked against the
`CRM_API_KEY` env var. That is appropriate for one operator running their own
pipeline and nothing more: there are no accounts, no per-user scoping, and
anyone holding the key can read and write every lead. If this ever grows past
one person, it needs real auth rather than more keys.
"""

import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from database import get_db
from helpers import generate_id, now_iso

crm_router = APIRouter(prefix="/v1/crm", tags=["crm"])

LeadStatus = Literal["new", "contacted", "warm", "won", "dead"]

# Status lives in Pydantic rather than a CHECK constraint on the column: the
# pipeline's stages are a product decision that will change, and changing a
# Literal is a deploy while changing a CHECK is a migration.
VALID_STATUSES = ("new", "contacted", "warm", "won", "dead")

TOUCH_TICKER_START = 5000

# What the daily counters go back to at rollover. Stored as columns rather than
# constants because "auto-reset each calendar day" is meaningless without a
# target, and the target is the kind of number that gets retuned weekly — a
# PATCH should be able to change it without a migration.
DEFAULT_DAILY_CALLS = 20
DEFAULT_DAILY_EMAILS = 20
DEFAULT_DAILY_FB = 20


def _reset_tz() -> timezone:
    """Timezone the daily counters roll over in.

    Defaults to UTC, which is almost certainly wrong for a sales day: on Render
    the process runs in UTC, so an unset CRM_TIMEZONE rolls the daily counts
    over at 7pm US Eastern — mid-shift for the bars this pipeline sells to.
    Set CRM_TIMEZONE to a real zone name (e.g. America/New_York).
    """
    name = os.getenv("CRM_TIMEZONE", "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"[crm] CRM_TIMEZONE={name!r} is not a known zone — falling back to UTC", flush=True)
        return timezone.utc


def _today() -> str:
    """Today's calendar date in the reset timezone, as YYYY-MM-DD."""
    return datetime.now(_reset_tz()).strftime("%Y-%m-%d")


# ============== AUTH ==============

def require_crm_key(x_crm_key: Optional[str] = Header(default=None)):
    """Gate every CRM endpoint on the shared key.

    An unset CRM_API_KEY is a 503, never an open door: the failure mode of
    "no key configured means no check" would silently publish the whole
    pipeline the first time the env var was missed on a deploy.
    """
    expected = os.getenv("CRM_API_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail={
            "error": "service_unavailable",
            "message": "CRM is not configured on this server (CRM_API_KEY unset)",
        })
    if not x_crm_key or not secrets.compare_digest(x_crm_key, expected):
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "message": "Missing or invalid X-CRM-Key",
        })
    return True


# ============== SCHEMA ==============

def init_crm_tables():
    """Create the CRM tables if they aren't there.

    Called from main.py's lifespan after init_db(). Same CREATE TABLE IF NOT
    EXISTS / information_schema-gated pattern the rest of the app uses, so it's
    safe to run on every boot.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_leads (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                loc TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                contact TEXT,
                phone TEXT,
                email TEXT,
                call_date TEXT,
                email_date TEXT,
                followup_date TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Attribution + call-routing columns. Gated individually so this is
        # safe on a database that already has the base table.
        for col, col_type in [
            ("matched_user_id", "TEXT"),      # the signup this lead became
            ("matched_at", "TEXT"),
            ("match_method", "TEXT"),         # email | domain | name
            ("tz_offset_hours", "INTEGER"),   # for the call-window hint
            ("source", "TEXT"),               # 'leadgen' | 'manual'
            ("last_touch_at", "TEXT"),
            ("attempts", "INTEGER DEFAULT 0"),      # dials made, for the cadence
            ("last_outcome", "TEXT"),
            ("opening_hours", "TEXT"),              # raw OSM string, per-venue call timing
            ("opener", "TEXT"),                     # one true thing to open the call with
            # What makes one lead worth calling before another.
            ("lead_score", "INTEGER"),              # the generator's fit score
            ("email_kind", "TEXT"),                 # personal | owner | role | unknown
            ("manager_name", "TEXT"),               # only ever from the venue's own site
            ("manager_role", "TEXT"),               # the title printed next to it
            ("manager_source", "TEXT"),             # the page it was read off
            ("manager_seen_at", "TEXT"),            # when — names go stale, see below
            ("tz_name", "TEXT"),                    # IANA zone; the offset alone ignores DST
            ("queued_email_at", "TEXT"),            # denormalised so every list can show it
            ("venue_facts", "TEXT"),                # attributable facts for the call
            ("call_brief", "TEXT"),                 # the talking points written from them
        ]:
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'crm_leads' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE crm_leads ADD COLUMN {col} {col_type}")

        # Mail waiting for a better hour. A separate table rather than a flag on
        # the lead: a queued email has its own subject, body, recipient and
        # failure state, and none of that belongs on a lead row.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_scheduled_emails (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                to_addr TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                send_at TEXT NOT NULL,          -- UTC, ISO 8601
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sched_due "
                       "ON crm_scheduled_emails(status, send_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sched_lead "
                       "ON crm_scheduled_emails(lead_id)")

        # Undo for a mis-logged call. The whole row as it was, written before a
        # touch changes it, so putting it back is a restore rather than a guess
        # at which fields to unwind.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_lead_undo (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                action TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                counters_spent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                restored_at TEXT,
                touch_id TEXT
            )
        """)
        # Which touch this undo reverses. Nullable: rows written before this
        # column existed have no pairing to record, and the undo path falls back
        # for those rather than refusing to run.
        cursor.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'crm_lead_undo' AND column_name = 'touch_id'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE crm_lead_undo ADD COLUMN touch_id TEXT")
            print("[crm] migrated crm_lead_undo: added touch_id TEXT", flush=True)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_undo_lead ON crm_lead_undo(lead_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_undo_created ON crm_lead_undo(created_at DESC)")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_leads_status ON crm_leads(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_leads_followup ON crm_leads(followup_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_leads_created ON crm_leads(created_at)")

        # Every dial, so "which hour actually connects" is answerable from real
        # data instead of from my assumptions about when bars are quiet.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_touches (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                outcome TEXT,
                connected BOOLEAN NOT NULL DEFAULT FALSE,
                attempt INTEGER,
                local_hour INTEGER,
                weekday INTEGER,
                at TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_touches_at ON crm_touches(at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_touches_lead ON crm_touches(lead_id)")

        # Single row, pinned to id=1 by a CHECK — the counters are one global
        # scoreboard, and a second row would silently become a second truth.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_counters (
                id INTEGER PRIMARY KEY,
                touch_ticker_remaining INTEGER NOT NULL DEFAULT %s,
                touch_ticker_last_action TEXT,
                downloads_confirmed INTEGER NOT NULL DEFAULT 0,
                daily_calls_remaining INTEGER NOT NULL DEFAULT %s,
                daily_emails_remaining INTEGER NOT NULL DEFAULT %s,
                daily_fb_remaining INTEGER NOT NULL DEFAULT %s,
                daily_calls_quota INTEGER NOT NULL DEFAULT %s,
                daily_emails_quota INTEGER NOT NULL DEFAULT %s,
                daily_fb_quota INTEGER NOT NULL DEFAULT %s,
                daily_reset_date TEXT,
                updated_at TEXT,
                CONSTRAINT crm_counters_single_row CHECK (id = 1)
            )
        """, (TOUCH_TICKER_START, DEFAULT_DAILY_CALLS, DEFAULT_DAILY_EMAILS, DEFAULT_DAILY_FB,
              DEFAULT_DAILY_CALLS, DEFAULT_DAILY_EMAILS, DEFAULT_DAILY_FB))

        cursor.execute("""
            INSERT INTO crm_counters (id, daily_reset_date, updated_at)
            VALUES (1, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (_today(), now_iso()))

        conn.commit()

        # Deliberately loud and distinctive: this is the line to grep for in
        # Render's logs to confirm the CRM schema landed on a deploy.
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('crm_leads', 'crm_counters')"
        )
        found = sorted(row["table_name"] for row in cursor.fetchall())
        print(f"[crm] CRM_TABLES_READY tables={found} timezone={os.getenv('CRM_TIMEZONE', 'UTC')} "
              f"api_key_set={bool(os.getenv('CRM_API_KEY'))}", flush=True)


# ============== MODELS ==============

class LeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    loc: Optional[str] = Field(default=None, max_length=200)
    status: LeadStatus = "new"
    contact: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=50)
    email: Optional[str] = Field(default=None, max_length=320)
    call_date: Optional[str] = Field(default=None, max_length=32)
    email_date: Optional[str] = Field(default=None, max_length=32)
    followup_date: Optional[str] = Field(default=None, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=20000)


class LeadUpdate(BaseModel):
    """Every field optional — a PATCH only writes what it names.

    `None` is indistinguishable from "absent" in Pydantic without extra work,
    so clearing a field is done by sending an empty string rather than null;
    the writer below normalises "" back to NULL.
    """
    name: Optional[str] = Field(default=None, max_length=200)
    loc: Optional[str] = Field(default=None, max_length=200)
    status: Optional[LeadStatus] = None
    contact: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=50)
    email: Optional[str] = Field(default=None, max_length=320)
    call_date: Optional[str] = Field(default=None, max_length=32)
    email_date: Optional[str] = Field(default=None, max_length=32)
    followup_date: Optional[str] = Field(default=None, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=20000)


class CountersUpdate(BaseModel):
    """Absolute sets and relative deltas, in one body.

    Deltas exist because the common operations are "one more touch" and "one
    fewer call left", and a read-modify-write from the page would lose a
    concurrent increment. Deltas apply as `col = col + n` inside the same
    statement, so they can't race. When a field gets both an absolute and a
    delta in the same request, the absolute is applied first and the delta
    on top of it.
    """
    touch_ticker_remaining: Optional[int] = Field(default=None, ge=0)
    touch_ticker_delta: Optional[int] = None
    touch_ticker_last_action: Optional[str] = Field(default=None, max_length=500)

    downloads_confirmed: Optional[int] = Field(default=None, ge=0)
    downloads_confirmed_delta: Optional[int] = None

    daily_calls_remaining: Optional[int] = Field(default=None, ge=0)
    daily_calls_delta: Optional[int] = None
    daily_emails_remaining: Optional[int] = Field(default=None, ge=0)
    daily_emails_delta: Optional[int] = None
    daily_fb_remaining: Optional[int] = Field(default=None, ge=0)
    daily_fb_delta: Optional[int] = None

    daily_calls_quota: Optional[int] = Field(default=None, ge=0)
    daily_emails_quota: Optional[int] = Field(default=None, ge=0)
    daily_fb_quota: Optional[int] = Field(default=None, ge=0)

    # Force the daily counters back to their quotas now, without waiting for
    # the date to roll — the "I'm starting fresh" button.
    reset_daily: Optional[bool] = None


LEAD_COLUMNS = (
    "id", "name", "loc", "status", "contact", "phone", "email",
    "call_date", "email_date", "followup_date", "notes",
    "created_at", "updated_at",
    "matched_user_id", "matched_at", "match_method", "tz_offset_hours",
    "source", "last_touch_at", "attempts", "last_outcome",
    "opening_hours", "opener",
    "lead_score", "email_kind", "tz_name", "queued_email_at", "venue_facts",
    "manager_name", "manager_role", "manager_source", "manager_seen_at",
)

# How long to wait before the next dial, by attempt number. Spread across days
# so successive tries land on different shifts and different managers.
#
# This exists because persistence is the single biggest lever in cold calling
# and the one most easily lost: before it, a voicemail only came back if the
# caller remembered to set a follow-up by hand, so most leads died at attempt
# one. Most connects happen somewhere around the third to sixth try.
CADENCE_DAYS = [1, 2, 4, 7, 14]
MAX_ATTEMPTS = len(CADENCE_DAYS) + 1     # after this many dials with no contact, stop

# Outcomes that mean a human was actually reached.
CONNECTED_OUTCOMES = {"answered", "gatekeeper", "callback", "not_interested"}

# Every outcome a touch can carry — the quick-outcome buttons' data-outcome
# values, TouchLogged's Literal, and what DEBRIEF_SYSTEM/QUICK_ADD_SYSTEM are
# now asked for directly (see _apply_call_notes). One set so all three ways
# of logging a call agree on the vocabulary.
TOUCH_OUTCOMES = {"answered", "voicemail", "gatekeeper", "not_interested", "callback"}

# Columns a PATCH is allowed to write. `id`, `created_at` and `updated_at` are
# not in here on purpose — an allowlist beats filtering a denylist when the
# values are being interpolated into a SQL fragment.
LEAD_WRITABLE = (
    "name", "loc", "status", "contact", "phone", "email",
    "call_date", "email_date", "followup_date", "notes",
)


from phones import normalize_us_phone, format_us_phone, format_us_phone_dashed


def phone_digits(phone: Optional[str]) -> str:
    """Ten bare digits for the dialer, or "" when the number can't be trusted.

    Deliberately returns nothing rather than a best guess: an empty cell is a
    visible problem, whereas a plausible-looking wrong number gets dialled.
    See phones.py for what is rejected and why.
    """
    return normalize_us_phone(phone) or ""


# Zone names and the working day inside them. The whole call list is organised
# around this: a zone entering its dinner rush is a zone to stop calling, and
# the next one west is an hour behind and still fine.
ZONE_LABELS = {-5: "Eastern", -6: "Central", -7: "Mountain", -8: "Pacific"}


def zone_state(offset: Optional[int]) -> dict:
    """What's happening in this timezone right now, and whether to call it."""
    if offset is None:
        return {"offset": None, "label": "Unknown", "local_time": "",
                "state": "unknown", "headline": "No timezone on these",
                "rank": 5, "callable": True}
    local = datetime.now(timezone.utc) + timedelta(hours=offset)
    hour = local.hour
    label = ZONE_LABELS.get(offset, f"UTC{offset}")
    if hour < 11:
        state, headline, rank, ok = "closed", "Closed — nobody there yet", 3, False
    elif hour < CALL_WINDOW_START:
        state, headline, rank, ok = "opening", "Opening up — worth a try", 2, True
    elif hour < CALL_WINDOW_END:
        state, headline, rank, ok = "good", "CALL NOW — quiet before service", 0, True
    elif hour < 21:
        state, headline, rank, ok = "rush", "Dinner rush — skip for now", 4, False
    else:
        state, headline, rank, ok = "late", "Too late — they're slammed", 4, False
    return {"offset": offset, "label": label, "local_time": local.strftime("%-I:%M%p").lower(),
            "state": state, "headline": headline, "rank": rank, "callable": ok}


def _record_touch(cursor, lead, kind: str, outcome: Optional[str], attempt: int,
                  tz_offset: Optional[int]) -> str:
    """Log one dial with the local hour, so connect rates can be read by hour.

    Returns the touch id so the undo record can point at THIS touch — see
    `_attach_touch`.
    """
    local = datetime.now(timezone.utc) + timedelta(hours=tz_offset or 0)
    touch_id = generate_id()
    cursor.execute("""
        INSERT INTO crm_touches (id, lead_id, kind, outcome, connected, attempt,
                                 local_hour, weekday, at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (touch_id, lead["id"], kind, outcome,
          outcome in CONNECTED_OUTCOMES, attempt,
          local.hour, local.weekday(), now_iso()))
    return touch_id


def _attach_touch(cursor, undo_id: str, touch_id: str) -> None:
    """Bind an undo record to the touch it reverses.

    The snapshot is taken before the touch is logged, so the pairing can only be
    made afterwards. Without it the undo had to guess, and guessed "the newest
    touch on this lead" — so undoing a call that was followed by an email marked
    the EMAIL undone and left the call in the connect rates.
    """
    cursor.execute("UPDATE crm_lead_undo SET touch_id = %s WHERE id = %s",
                   (touch_id, undo_id))


# Columns the undo restores. Everything a touch can change, and nothing it
# can't — id and created_at are identity, not state.
UNDO_COLUMNS = (
    "status", "contact", "phone", "email", "call_date", "email_date",
    "followup_date", "notes", "last_touch_at", "attempts", "last_outcome",
    "updated_at",
)


def _snapshot(cursor, lead, action: str, counters_spent: int = 1) -> str:
    """Store the row as it is now, before a touch changes it.

    Recorded as the whole row rather than a list of what to unwind: a touch
    stamps a date, bumps a counter, moves the status, sets a follow-up and
    appends a note, and each of those has a different "undo" depending on what
    the row already held. A snapshot has one.
    """
    import json as _json
    undo_id = generate_id()
    cursor.execute("""
        INSERT INTO crm_lead_undo (id, lead_id, action, snapshot, counters_spent, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (undo_id, lead["id"], action,
          _json.dumps({k: lead.get(k) for k in UNDO_COLUMNS}),
          counters_spent, now_iso()))
    return undo_id


def _cadence(attempt: int, outcome: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """(days until the next try, status to force) for a call that didn't land.

    Only applies when nobody was reached. A connect always beats the schedule —
    if a human said "call me Tuesday" that's what the follow-up should be, not
    whatever the ladder says.
    """
    if outcome in ("answered", "callback", "not_interested"):
        return None, None
    if attempt >= MAX_ATTEMPTS:
        return None, "dead"
    return CADENCE_DAYS[min(attempt - 1, len(CADENCE_DAYS) - 1)], None


def _lead_row(row) -> dict:
    lead = {k: row[k] for k in LEAD_COLUMNS}
    lead["phone_digits"] = phone_digits(lead.get("phone"))
    lead["phone_pretty"] = format_us_phone(lead["phone_digits"])
    # What the COPY button and click-to-copy actually hand the clipboard.
    # CloudTalk's paste box rejects a bare 10-digit string with no
    # separators, so the dashed form is what's copyable, not phone_digits.
    lead["phone_dial"] = format_us_phone_dashed(lead["phone_digits"])
    # Surfaced rather than hidden: a lead whose number didn't validate should
    # look wrong on screen, not quietly get dialled.
    lead["phone_ok"] = bool(lead["phone_digits"])

    # Why this lead is where it is in the list. Shown on the row so the order
    # is legible rather than mysterious — a list that reorders itself for
    # reasons you can't see is one you stop trusting.
    reasons = []
    if lead.get("manager_name"):
        reasons.append(f"ask for {lead['manager_name']}")
    if lead.get("email_kind") == "personal":
        reasons.append("direct email")
    elif lead.get("email_kind") == "owner":
        reasons.append("owner inbox")
    lead["why"] = " · ".join(reasons)

    # A name read off a website ages. The row carries how old it is so the
    # caller asks "is Dave still there?" rather than "can I speak to Dave?".
    seen = lead.get("manager_seen_at")
    lead["manager_age_days"] = None
    if seen:
        try:
            when = datetime.fromisoformat(str(seen).replace("Z", "+00:00"))
            # A value with no offset parses fine and then explodes on the
            # subtraction (TypeError, not ValueError). Every row goes through
            # here on the way to the call list, so one naive timestamp — an
            # older row, a hand-entered date — used to 500 the whole calling
            # screen rather than losing one lead's age badge. Read it as UTC.
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            lead["manager_age_days"] = max(
                0, (datetime.now(timezone.utc) - when).days)
        except (ValueError, TypeError):
            pass
    return lead


def _blank_to_none(value):
    """An empty string from the page means "clear this", not "store ''"."""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


# ============== LEADS ==============

@crm_router.get("/leads", response_model=dict)
def list_leads(status: Optional[str] = None, q: Optional[str] = None,
               limit: int = 200, offset: int = 0,
               _: bool = Depends(require_crm_key)):
    """The whole pipeline: every lead, at every stage, searchable.

    This is the one view that shows a lead AFTER it has been worked. The call
    list deliberately hides anything touched — that's what stops the same bar
    being rung twice — and follow-ups only show what's due. Without this, a
    lead you spoke to on Tuesday and didn't set a follow-up for is invisible,
    which is how warm leads quietly die.
    """
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_status",
            "message": f"status must be one of {', '.join(VALID_STATUSES)}",
        })
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where, params = ["1=1"], []
    if status:
        where.append("status = %s"); params.append(status)
    if q and q.strip():
        # Name, town, contact, email or phone — whichever the operator happens
        # to remember. Digits match the phone with its punctuation ignored, so
        # searching "6157429095" finds "+1-615-742-9095".
        term = f"%{q.strip().lower()}%"
        digits = re.sub(r"\D", "", q)
        clause = ("(LOWER(name) LIKE %s OR LOWER(COALESCE(loc,'')) LIKE %s "
                  "OR LOWER(COALESCE(contact,'')) LIKE %s "
                  "OR LOWER(COALESCE(email,'')) LIKE %s")
        params += [term, term, term, term]
        if digits:
            clause += " OR REGEXP_REPLACE(COALESCE(phone,''), '[^0-9]', '', 'g') LIKE %s"
            params.append(f"%{digits}%")
        where.append(clause + ")")

    sql_where = " AND ".join(where)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) AS n FROM crm_leads WHERE {sql_where}", params)
        matching = cursor.fetchone()["n"]
        cursor.execute(
            f"""SELECT * FROM crm_leads WHERE {sql_where}
                 ORDER BY COALESCE(last_touch_at, updated_at) DESC, created_at DESC
                 LIMIT %s OFFSET %s""",
            params + [limit, offset])
        leads = [_lead_row(row) for row in cursor.fetchall()]

        # Always the totals for the whole pipeline, not for the current filter:
        # the counts are the tab labels, and a tab that renumbers itself when
        # you click it is unreadable.
        cursor.execute("SELECT status, COUNT(*) AS n FROM crm_leads GROUP BY status")
        by_status = {r["status"]: r["n"] for r in cursor.fetchall()}
        cursor.execute("SELECT COUNT(*) AS n FROM crm_leads")
        everything = cursor.fetchone()["n"]

    for lead in leads:
        lead["window"] = _call_window(lead.get("tz_offset_hours"),
                                      lead.get("opening_hours"), lead.get("tz_name"))
    return {"leads": leads, "count": len(leads), "matching": matching,
            "offset": offset, "limit": limit,
            "counts": {**{k: by_status.get(k, 0) for k in VALID_STATUSES},
                       "all": everything}}


@crm_router.get("/leads/{lead_id}", response_model=dict)
def get_lead(lead_id: str, _: bool = Depends(require_crm_key)):
    """One lead, everything on it. What the edit form loads."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})
    lead = _lead_row(row)
    lead["window"] = _call_window(row.get("tz_offset_hours"),
                                  row.get("opening_hours"), row.get("tz_name"))
    return {"lead": lead}


@crm_router.post("/leads", response_model=dict, status_code=201)
def create_lead(data: LeadCreate, _: bool = Depends(require_crm_key)):
    lead_id = generate_id()
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crm_leads (id, name, loc, status, contact, phone, email,
                                   call_date, email_date, followup_date, notes,
                                   created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            lead_id, data.name.strip(), _blank_to_none(data.loc), data.status,
            _blank_to_none(data.contact), _blank_to_none(data.phone), _blank_to_none(data.email),
            _blank_to_none(data.call_date), _blank_to_none(data.email_date),
            _blank_to_none(data.followup_date), _blank_to_none(data.notes),
            now, now,
        ))
        row = cursor.fetchone()
        conn.commit()
        return {"lead": _lead_row(row)}


@crm_router.patch("/leads/{lead_id}", response_model=dict)
def update_lead(lead_id: str, data: LeadUpdate, _: bool = Depends(require_crm_key)):
    fields = data.model_dump(exclude_unset=True)
    updates = {k: _blank_to_none(v) for k, v in fields.items() if k in LEAD_WRITABLE}

    if not updates:
        raise HTTPException(status_code=422, detail={
            "error": "empty_update",
            "message": "No writable fields in request body",
        })
    # A cleared name would leave a nameless row in the pipeline.
    if "name" in updates and not updates["name"]:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_name",
            "message": "name cannot be empty",
        })

    # Column names come from LEAD_WRITABLE, never from the request, so this
    # fragment can't carry user input; the values stay parameterised.
    assignments = ", ".join(f"{col} = %s" for col in updates)
    params = list(updates.values()) + [now_iso(), lead_id]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE crm_leads SET {assignments}, updated_at = %s WHERE id = %s RETURNING *",
            params,
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Lead not found",
            })
        conn.commit()
        return {"lead": _lead_row(row)}


@crm_router.delete("/leads/{lead_id}", response_model=dict)
def delete_lead(lead_id: str, _: bool = Depends(require_crm_key)):
    """Hard delete — the CRM's rows are working notes, not records to preserve.

    The candidate that produced this lead is retired at the same time. Without
    that, the generator would cheerfully re-promote the same restaurant on a
    later run and it would reappear on the call list — the exact duplicate call
    deleting it was meant to prevent.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE crm_lead_candidates SET status='rejected', "
            "reject_reason='lead deleted by hand' WHERE promoted_lead_id = %s",
            (lead_id,),
        )
        cursor.execute("DELETE FROM crm_leads WHERE id = %s", (lead_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        if not deleted:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Lead not found",
            })
        return {"success": True, "deleted_id": lead_id}


@crm_router.post("/leads/{lead_id}/email-sent", response_model=dict)
def mark_email_sent(lead_id: str, _: bool = Depends(require_crm_key)):
    """Stamp today onto whichever of email_date / followup_date is still empty.

    First email fills email_date, the follow-up fills followup_date. When both
    are already stamped there is nothing this endpoint is defined to do, so it
    changes nothing and returns field_set: null rather than guessing which one
    to overwrite — the caller can see it was a no-op and decide.
    """
    today = _today()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT email_date, followup_date FROM crm_leads WHERE id = %s FOR UPDATE", (lead_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Lead not found",
            })

        if not row["email_date"]:
            field = "email_date"
        elif not row["followup_date"]:
            field = "followup_date"
        else:
            field = None

        if field is None:
            cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
            conn.commit()
            return {"lead": _lead_row(cursor.fetchone()), "field_set": None}

        cursor.execute(
            f"UPDATE crm_leads SET {field} = %s, updated_at = %s WHERE id = %s RETURNING *",
            (today, now_iso(), lead_id),
        )
        updated = cursor.fetchone()
        conn.commit()
        return {"lead": _lead_row(updated), "field_set": field, "date": today}


# ============== COUNTERS ==============

COUNTER_COLUMNS = (
    "touch_ticker_remaining", "touch_ticker_last_action", "downloads_confirmed",
    "daily_calls_remaining", "daily_emails_remaining", "daily_fb_remaining",
    "daily_calls_quota", "daily_emails_quota", "daily_fb_quota",
    "daily_reset_date", "updated_at",
)


def _counters_row(row) -> dict:
    return {k: row[k] for k in COUNTER_COLUMNS}


def _load_counters_locked(cursor) -> dict:
    """Read the counter row, rolling the daily fields over first if the date moved.

    Takes the row lock before deciding, so two requests arriving on the same
    morning can't both perform the reset and double-apply whatever else they
    were carrying.
    """
    cursor.execute("SELECT * FROM crm_counters WHERE id = 1 FOR UPDATE")
    row = cursor.fetchone()
    if not row:
        # First call on a database whose init predates this table, or a row
        # someone deleted by hand. Recreate rather than 500.
        cursor.execute("""
            INSERT INTO crm_counters (id, daily_reset_date, updated_at)
            VALUES (1, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """, (_today(), now_iso()))
        cursor.execute("SELECT * FROM crm_counters WHERE id = 1 FOR UPDATE")
        row = cursor.fetchone()

    today = _today()
    if row["daily_reset_date"] != today:
        cursor.execute("""
            UPDATE crm_counters
               SET daily_calls_remaining  = daily_calls_quota,
                   daily_emails_remaining = daily_emails_quota,
                   daily_fb_remaining     = daily_fb_quota,
                   daily_reset_date       = %s,
                   updated_at             = %s
             WHERE id = 1
            RETURNING *
        """, (today, now_iso()))
        row = cursor.fetchone()
    return row


@crm_router.get("/counters", response_model=dict)
def get_counters(_: bool = Depends(require_crm_key)):
    with get_db() as conn:
        cursor = conn.cursor()
        row = _load_counters_locked(cursor)
        conn.commit()
        return {"counters": _counters_row(row)}


@crm_router.patch("/counters", response_model=dict)
def update_counters(data: CountersUpdate, _: bool = Depends(require_crm_key)):
    """Partial update. Absolutes are set, deltas are added, both are clamped at 0.

    The clamp is why deltas don't just run as bare `col = col + n`: a ticker
    decremented past zero should sit at zero, not go negative and read as a
    surplus.
    """
    fields = data.model_dump(exclude_unset=True)

    absolute = {
        "touch_ticker_remaining": fields.get("touch_ticker_remaining"),
        "downloads_confirmed": fields.get("downloads_confirmed"),
        "daily_calls_remaining": fields.get("daily_calls_remaining"),
        "daily_emails_remaining": fields.get("daily_emails_remaining"),
        "daily_fb_remaining": fields.get("daily_fb_remaining"),
        "daily_calls_quota": fields.get("daily_calls_quota"),
        "daily_emails_quota": fields.get("daily_emails_quota"),
        "daily_fb_quota": fields.get("daily_fb_quota"),
    }
    deltas = {
        "touch_ticker_remaining": fields.get("touch_ticker_delta"),
        "downloads_confirmed": fields.get("downloads_confirmed_delta"),
        "daily_calls_remaining": fields.get("daily_calls_delta"),
        "daily_emails_remaining": fields.get("daily_emails_delta"),
        "daily_fb_remaining": fields.get("daily_fb_delta"),
    }

    with get_db() as conn:
        cursor = conn.cursor()
        # Rolls the day over first, so a delta sent at 00:00:01 applies to
        # today's fresh quota rather than to yesterday's leftovers.
        row = _load_counters_locked(cursor)

        updates: dict = {}
        for col, value in absolute.items():
            if value is not None:
                updates[col] = value
        if fields.get("reset_daily"):
            base_quota = {
                "daily_calls_remaining": "daily_calls_quota",
                "daily_emails_remaining": "daily_emails_quota",
                "daily_fb_remaining": "daily_fb_quota",
            }
            for remaining_col, quota_col in base_quota.items():
                updates[remaining_col] = updates.get(quota_col, row[quota_col])
        for col, delta in deltas.items():
            if delta is not None:
                current = updates.get(col, row[col])
                updates[col] = max(0, current + delta)

        if "touch_ticker_last_action" in fields:
            updates["touch_ticker_last_action"] = _blank_to_none(
                fields["touch_ticker_last_action"]
            )

        if not updates:
            # Nothing to write, but the read above may have rolled the day
            # over, which is a real change worth committing.
            conn.commit()
            return {"counters": _counters_row(row)}

        # Column names come from the fixed dicts above, never from the request.
        assignments = ", ".join(f"{col} = %s" for col in updates)
        params = list(updates.values()) + [now_iso()]
        cursor.execute(
            f"UPDATE crm_counters SET {assignments}, updated_at = %s WHERE id = 1 RETURNING *",
            params,
        )
        updated = cursor.fetchone()
        conn.commit()
        return {"counters": _counters_row(updated)}


# ============== ATTRIBUTION ==============
#
# The gap this closes: the counters measured effort (touches, calls, emails)
# and the only outcome was a hand-typed download count, while the product
# database knew exactly who signed up and who paid. Nothing joined the two, so
# "did the bar I called in March become a customer?" had no answer.
#
# Matching is deliberately conservative and records HOW it matched, because a
# wrong attribution is worse than none — it would send the next 5,000 touches
# at the wrong city.

def _norm(text: Optional[str]) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _email_domain(email: Optional[str]) -> str:
    if not email or "@" not in email:
        return ""
    domain = email.split("@")[-1].lower().strip()
    # A shared mailbox provider says nothing about which business this is.
    if domain in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                  "aol.com", "icloud.com", "me.com", "live.com", "msn.com",
                  "comcast.net", "verizon.net", "att.net"}:
        return ""
    return domain


def rematch_attribution() -> dict:
    """Join crm_leads to users. Safe to re-run; only fills in blanks."""
    matched = {"email": 0, "domain": 0, "name": 0}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, email, business_name, created_at FROM users WHERE deleted_at IS NULL"
        )
        users = [dict(r) for r in cursor.fetchall()]

        by_email = {(u["email"] or "").lower(): u for u in users if u["email"]}
        by_domain: dict = {}
        for u in users:
            d = _email_domain(u["email"])
            if d:
                by_domain.setdefault(d, []).append(u)
        by_name: dict = {}
        for u in users:
            n = _norm(u.get("business_name"))
            if len(n) >= 4:            # "bar" would match half the world
                by_name.setdefault(n, []).append(u)

        cursor.execute("SELECT * FROM crm_leads WHERE matched_user_id IS NULL")
        leads = [dict(r) for r in cursor.fetchall()]

        for lead in leads:
            user = None
            method = None

            hit = by_email.get((lead.get("email") or "").lower())
            if hit:
                user, method = hit, "email"

            if not user:
                d = _email_domain(lead.get("email"))
                # Only when the domain identifies exactly one account — two
                # accounts on one domain can't be told apart from here.
                if d and len(by_domain.get(d, [])) == 1:
                    user, method = by_domain[d][0], "domain"

            if not user:
                n = _norm(lead.get("name"))
                if len(n) >= 4 and len(by_name.get(n, [])) == 1:
                    user, method = by_name[n][0], "name"

            if user:
                matched[method] += 1
                cursor.execute("""
                    UPDATE crm_leads
                       SET matched_user_id=%s, matched_at=%s, match_method=%s, updated_at=%s
                     WHERE id=%s
                """, (user["id"], now_iso(), method, now_iso(), lead["id"]))

        conn.commit()
    total = sum(matched.values())
    return {"matched": total, "by_method": matched}


@crm_router.post("/attribution/rematch", response_model=dict)
def attribution_rematch(_: bool = Depends(require_crm_key)):
    return rematch_attribution()


# ============== FUNNEL ==============

@crm_router.get("/funnel", response_model=dict)
def funnel(_: bool = Depends(require_crm_key)):
    """Signups, trials, conversion, activation and what outreach produced.

    Everything here is computed from data already being collected; none of it
    required new tracking. Activation is the number worth staring at: a bar
    that signs up and never finishes a first count never converts, and nothing
    surfaced that before.
    """
    today = _today()
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT subscription_status AS status, COUNT(*) AS n
              FROM users WHERE deleted_at IS NULL GROUP BY subscription_status
        """)
        by_status = {r["status"] or "unknown": r["n"] for r in cursor.fetchall()}
        total_users = sum(by_status.values())

        cursor.execute("""
            SELECT SUBSTRING(created_at, 1, 10) AS day, COUNT(*) AS n
              FROM users
             WHERE deleted_at IS NULL AND created_at >= %s
             GROUP BY 1 ORDER BY 1
        """, ((datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),))
        signups_30d = [{"day": r["day"], "count": r["n"]} for r in cursor.fetchall()]

        # Trials, bucketed by how long is left — the save-motion worklist.
        cursor.execute("""
            SELECT trial_ends_at FROM users
             WHERE deleted_at IS NULL AND subscription_status = 'trial'
               AND trial_ends_at IS NOT NULL
        """)
        buckets = {"expired": 0, "0-3_days": 0, "4-7_days": 0, "8plus_days": 0}
        for row in cursor.fetchall():
            try:
                ends = datetime.fromisoformat(row["trial_ends_at"].replace("Z", "+00:00"))
                days = (ends - datetime.now(timezone.utc)).days
            except (ValueError, AttributeError):
                continue
            if days < 0:
                buckets["expired"] += 1
            elif days <= 3:
                buckets["0-3_days"] += 1
            elif days <= 7:
                buckets["4-7_days"] += 1
            else:
                buckets["8plus_days"] += 1

        paying = sum(n for s, n in by_status.items() if s in ("active", "past_due"))
        ever_trialed = total_users
        conversion = round(100.0 * paying / ever_trialed, 1) if ever_trialed else 0.0

        # Activation: signed up, but did they ever finish a count or send an order?
        cursor.execute("""
            SELECT COUNT(DISTINCT u.id) AS n
              FROM users u
              JOIN inventory_sessions s ON s.user_id = u.id AND s.status = 'completed'
             WHERE u.deleted_at IS NULL
        """)
        completed_count = cursor.fetchone()["n"]
        cursor.execute("""
            SELECT COUNT(DISTINCT u.id) AS n
              FROM users u JOIN inventory_sessions s ON s.user_id = u.id
             WHERE u.deleted_at IS NULL
        """)
        started_count = cursor.fetchone()["n"]

        # Pipeline side.
        cursor.execute("SELECT status, COUNT(*) AS n FROM crm_leads GROUP BY status")
        leads_by_status = {r["status"]: r["n"] for r in cursor.fetchall()}
        cursor.execute("SELECT COUNT(*) AS n FROM crm_leads WHERE matched_user_id IS NOT NULL")
        leads_converted = cursor.fetchone()["n"]
        cursor.execute("""
            SELECT
              COUNT(*) FILTER (WHERE call_date IS NOT NULL) AS called,
              COUNT(*) FILTER (WHERE email_date IS NOT NULL) AS emailed,
              COUNT(*) FILTER (WHERE call_date IS NOT NULL AND matched_user_id IS NOT NULL) AS called_won,
              COUNT(*) FILTER (WHERE email_date IS NOT NULL AND matched_user_id IS NOT NULL) AS emailed_won
              FROM crm_leads
        """)
        ch = cursor.fetchone()

        def rate(won, total):
            return round(100.0 * won / total, 1) if total else 0.0

        return {
            "as_of": today,
            "users": {
                "total": total_users,
                "by_status": by_status,
                "paying": paying,
                "trial_to_paid_pct": conversion,
            },
            "signups_30d": signups_30d,
            "trials_ending": buckets,
            "activation": {
                "signed_up": total_users,
                "started_a_count": started_count,
                "completed_a_count": completed_count,
                "never_started_pct": rate(total_users - started_count, total_users),
                "started_but_never_finished": max(0, started_count - completed_count),
            },
            "pipeline": {
                "by_status": leads_by_status,
                "total": sum(leads_by_status.values()),
                "converted_to_signup": leads_converted,
            },
            "channels": {
                "called": ch["called"], "called_won": ch["called_won"],
                "called_win_pct": rate(ch["called_won"], ch["called"]),
                "emailed": ch["emailed"], "emailed_won": ch["emailed_won"],
                "emailed_win_pct": rate(ch["emailed_won"], ch["emailed"]),
            },
        }


# ============== USERS (app customers) ==============

# The three subscription_status values billing_webhook (main.py) ever writes.
# Kept here rather than imported from main — main.py imports crm_router from
# this module, so importing back would be circular.
USER_STATUSES = ("trial", "active", "canceled")

# App Store review accounts and our own QA/dev accounts, matched by shape
# rather than an exact list — a new one of these lands with every build
# submitted for review, and they'd otherwise silently pad "signups" forever.
# `appreview444@icloud.com` / `applereview@my86d.com` are Apple's reviewers;
# `test+<timestamp>@86d.com` and `test-reconnect-2026@example.com` are ours;
# `phase3-verify@86d.com` is a verification pass. `86d.com` (not the real
# `my86d.com`) and `example.com` are never a real bar's domain. This is a
# view filter only — it never touches the row, so nothing here is destructive.
TEST_EMAIL_PATTERN = r"(^test[-+.]|app.*review|@86d\.com$|@example\.com$|-verify@)"


@crm_router.get("/users", response_model=dict)
def list_users(status: Optional[str] = None, q: Optional[str] = None,
               limit: int = 100, offset: int = 0,
               _: bool = Depends(require_crm_key)):
    """Everyone who actually downloaded the app and made an account — the
    other side of the pipeline tab, which is everyone who HASN'T yet. A lead
    turns into a row here the moment attribution matches it to a signup, but
    most rows never touched the pipeline at all: an organic download signs up
    with no call or email behind it.
    """
    if status is not None and status not in USER_STATUSES:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_status",
            "message": f"status must be one of {', '.join(USER_STATUSES)}",
        })
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where, params = ["deleted_at IS NULL", "email !~* %s"], [TEST_EMAIL_PATTERN]
    if status:
        where.append("subscription_status = %s"); params.append(status)
    if q and q.strip():
        # Business name, manager name, the account holder's own name, or
        # email — whichever the operator happens to remember.
        term = f"%{q.strip().lower()}%"
        where.append(
            "(LOWER(email) LIKE %s OR LOWER(COALESCE(business_name,'')) LIKE %s "
            "OR LOWER(COALESCE(manager_name,'')) LIKE %s OR LOWER(COALESCE(name,'')) LIKE %s)")
        params += [term, term, term, term]
    sql_where = " AND ".join(where)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) AS n FROM users WHERE {sql_where}", params)
        matching = cursor.fetchone()["n"]

        cursor.execute(f"""
            SELECT u.id, u.email, u.name, u.business_name, u.manager_name,
                   u.subscription_status, u.subscription_tier,
                   u.trial_started_at, u.trial_ends_at, u.created_at,
                   (SELECT COUNT(*) FROM locations
                     WHERE user_id = u.id AND deleted_at IS NULL) AS location_count,
                   (SELECT COUNT(*) FROM inventory_sessions
                     WHERE user_id = u.id AND status = 'completed') AS sessions_completed,
                   (SELECT MAX(started_at) FROM inventory_sessions
                     WHERE user_id = u.id) AS last_active_at
              FROM users u
             WHERE {sql_where}
             ORDER BY u.created_at DESC
             LIMIT %s OFFSET %s
        """, params + [limit, offset])
        users = [dict(r) for r in cursor.fetchall()]

        # Always the totals for the whole customer base, not the current
        # filter — same reasoning as the pipeline tabs: a count that
        # renumbers itself when you click it is unreadable. Test accounts
        # stay excluded here too, or the tab counts would disagree with the
        # rows actually shown under them.
        cursor.execute("""
            SELECT subscription_status, COUNT(*) AS n FROM users
             WHERE deleted_at IS NULL AND email !~* %s GROUP BY subscription_status
        """, (TEST_EMAIL_PATTERN,))
        by_status = {(r["subscription_status"] or "trial"): r["n"] for r in cursor.fetchall()}
        cursor.execute("SELECT COUNT(*) AS n FROM users WHERE deleted_at IS NULL AND email !~* %s",
                       (TEST_EMAIL_PATTERN,))
        everything = cursor.fetchone()["n"]

        # Which lead this account came from, if attribution has matched one —
        # a left join done in Python rather than SQL, the same shape as
        # rematch_attribution, so an unmatched user (most of them; plenty of
        # signups never went through a call at all) needs no special-casing.
        ids = [u["id"] for u in users]
        leads_by_user = {}
        if ids:
            cursor.execute(
                "SELECT matched_user_id, id, name, loc, status FROM crm_leads "
                "WHERE matched_user_id = ANY(%s)", (ids,))
            for r in cursor.fetchall():
                leads_by_user[r["matched_user_id"]] = dict(r)

    for u in users:
        u["subscription_status"] = u["subscription_status"] or "trial"
        lead = leads_by_user.get(u["id"])
        u["lead"] = {"id": lead["id"], "name": lead["name"], "loc": lead["loc"],
                     "status": lead["status"]} if lead else None

    return {"users": users, "count": len(users), "matching": matching,
            "offset": offset, "limit": limit,
            "counts": {**{k: by_status.get(k, 0) for k in USER_STATUSES}, "all": everything}}


@crm_router.delete("/users/{user_id}", response_model=dict)
def delete_user(user_id: str, _: bool = Depends(require_crm_key)):
    """Soft delete — sets the same `deleted_at` the product API already
    checks everywhere a user matters (login, registration's email-exists
    check, the funnel, this list). That makes it safe without any special
    handling here: a deleted user can't log in, frees its email for reuse,
    and every other query on `users` already filters `deleted_at IS NULL`.
    A hard DELETE would also fail outright the moment the account has any
    `locations` — that table's `user_id` is a real foreign key.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET deleted_at=%s, updated_at=%s "
            "WHERE id=%s AND deleted_at IS NULL",
            (now_iso(), now_iso(), user_id),
        )
        deleted = cursor.rowcount > 0
        conn.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Customer not found",
        })
    return {"success": True, "deleted_id": user_id}


# ============== TODAY'S CALL QUEUE ==============

# Bars are shut in the morning and slammed at night. Early afternoon is when
# somebody who can make a decision is there and not busy.
CALL_WINDOW_START = 14   # 2pm local
CALL_WINDOW_END = 17     # 5pm local


# Where the person doing the calling actually is. Iloilo is UTC+8, which puts
# the whole US calling day in the middle of their night — US Eastern afternoon
# is around 2-4am in the Philippines. That is not something to hide behind a
# venue-local clock: every time this screen shows, it shows the operator's own
# time too, so "call now" can be weighed against "it is 3am".
OPERATOR_TZ = os.getenv("CRM_OPERATOR_TZ", "Asia/Manila")

# How late a scheduled email may go out before the point of scheduling it is
# lost. Ninety minutes is roughly the width of the windows themselves: inside
# that it still lands somewhere near the quiet hour, past it you are emailing
# into service.
STALE_AFTER_MINUTES = int(os.getenv("CRM_EMAIL_STALE_MINUTES", "90"))


def _operator_tz():
    try:
        return ZoneInfo(OPERATOR_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _venue_now(tz_name: Optional[str], tz_offset: Optional[int]) -> datetime:
    """Local time at the venue, preferring the IANA zone over the raw offset.

    The offset is standard time. From March to November that is an hour behind
    the actual local clock everywhere except Arizona, and an hour is the whole
    width of the pre-open window — enough to call a bar while it's still locked
    or miss it entirely.
    """
    if tz_name:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now(timezone.utc) + timedelta(hours=tz_offset or 0)


def _call_window(tz_offset: Optional[int], hours: Optional[str] = None,
                 tz_name: Optional[str] = None) -> dict:
    """When to ring THIS venue, from its own opening hours where we have them.

    The blanket 2-5pm this replaced was wrong for a large share of the list:
    real harvested data has bars opening at 4pm and nightclubs at 9pm, and a
    2pm dial to either reaches an empty room. See callwindow.py.
    """
    from callwindow import call_window as venue_window
    if tz_offset is None and not tz_name:
        return {"known": False, "good_now": True, "local_time": None,
                "hint": "", "window": None, "state": "unknown"}
    local = _venue_now(tz_name, tz_offset)
    w = venue_window(hours, local)
    out = {"known": w["known"], "good_now": w["good_now"],
           # 12-hour throughout. "13:45 there" is a small tax on every glance,
           # and this screen is glanced at constantly.
           "local_time": local.strftime("%-I:%M%p").lower(), "hint": w["headline"],
           "window": w.get("window"), "windows": w.get("windows"),
           "starts_in": w.get("starts_in"), "state": w["state"]}
    # The same moment on the operator's clock. Without it, "best at 2:00pm"
    # means nothing to somebody thirteen hours away deciding whether to stay up.
    if w.get("starts_in") is not None:
        when = datetime.now(_operator_tz()) + timedelta(minutes=w["starts_in"])
        out["starts_at_yours"] = when.strftime("%-I:%M%p").lower()
    return out


# How ringable each window state is, worst-to-best, in one place. The call
# list, calling mode and anything added later have to agree: a venue sorted
# second in one view and hidden in another is the same venue, and the operator
# has no way to tell which screen is lying. `unknown` (no timezone on the row
# at all) ranks with `good` because _call_window hands it good_now=True — it
# gets the generic afternoon window rather than no window.
WINDOW_RANK = {"good": 0, "unknown": 0, "early": 1, "generic": 1, "late": 2,
               "shut_today": 3, "permanently_closed": 4}


@crm_router.get("/queue", response_model=dict)
def call_queue(limit: int = 50, _: bool = Depends(require_crm_key)):
    """The work for today, in the order it should be worked.

    Overdue follow-ups come first: a warm lead you said you'd call back and
    didn't is the most expensive thing in the pipeline, and nothing surfaced
    `followup_date` before this.
    """
    today = _today()
    with get_db() as conn:
        cursor = conn.cursor()

        def fetch(where: str, params: tuple, order: str):
            cursor.execute(
                f"SELECT * FROM crm_leads WHERE {where} ORDER BY {order} LIMIT %s",
                params + (limit,),
            )
            out = []
            for row in cursor.fetchall():
                lead = _lead_row(row)
                lead["call_window"] = _call_window(row.get("tz_offset_hours"), row.get("opening_hours"))
                out.append(lead)
            return out

        def total(where: str, params: tuple) -> int:
            """The real number, not how many fit in the page.

            `len(rows)` after a LIMIT is not a count: with the cap at 50 an
            operator with four hundred overdue follow-ups is told they have
            fifty, which is the difference between a bad week and a crisis.
            """
            cursor.execute(f"SELECT COUNT(*) AS n FROM crm_leads WHERE {where}", params)
            return cursor.fetchone()["n"]

        OVERDUE_WHERE = ("followup_date IS NOT NULL AND followup_date < %s "
                         "AND status NOT IN ('won','dead')")
        TODAY_WHERE = "followup_date = %s AND status NOT IN ('won','dead')"
        NEVER_WHERE = "call_date IS NULL AND status = 'new'"

        overdue = fetch(OVERDUE_WHERE, (today,), "followup_date ASC")
        due_today = fetch(TODAY_WHERE, (today,), "updated_at ASC")
        never_called = fetch(NEVER_WHERE, (), "created_at ASC")

        counts = {
            "overdue": total(OVERDUE_WHERE, (today,)),
            "due_today": total(TODAY_WHERE, (today,)),
            "never_called": total(NEVER_WHERE, ()),
        }
        return {
            "as_of": today,
            "overdue": overdue,
            "due_today": due_today,
            "never_called": never_called,
            "counts": counts,
            "shown": {"overdue": len(overdue), "due_today": len(due_today),
                      "never_called": len(never_called)},
            "limit": limit,
        }


class TouchLogged(BaseModel):
    kind: Literal["call", "email", "fb"]
    outcome: Optional[Literal["answered", "voicemail", "gatekeeper", "not_interested", "callback"]] = None
    followup_in_days: Optional[int] = Field(default=None, ge=0, le=365)
    note: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[LeadStatus] = None


@crm_router.post("/leads/{lead_id}/touch", response_model=dict)
def log_touch(lead_id: str, data: TouchLogged, _: bool = Depends(require_crm_key)):
    """One call that does everything logging a touch should do.

    Before this, working a lead meant four separate actions — stamp the date,
    move the status, set a follow-up, decrement the counter — and the counter
    was the only one anybody remembered. Doing it in one transaction is what
    keeps the activity numbers honest against the pipeline.
    """
    today = _today()
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE", (lead_id,))
        lead = cursor.fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Lead not found"})

        attempt = (lead["attempts"] or 0) + 1 if data.kind == "call" else (lead["attempts"] or 0)
        sets = ["updated_at = %s", "last_touch_at = %s"]
        params: list = [now, now]

        if data.kind == "call":
            sets.append("call_date = %s"); params.append(today)
            sets.append("attempts = %s"); params.append(attempt)
        elif data.kind == "email" and not lead["email_date"]:
            sets.append("email_date = %s"); params.append(today)
        if data.outcome:
            sets.append("last_outcome = %s"); params.append(data.outcome)

        # An explicit follow-up always wins; otherwise the ladder decides.
        cadence_days, forced_status = _cadence(attempt, data.outcome) if data.kind == "call" else (None, None)
        follow_days = data.followup_in_days if data.followup_in_days is not None else cadence_days
        if follow_days is not None:
            follow = (datetime.now(_reset_tz()) + timedelta(days=follow_days)).strftime("%Y-%m-%d")
            sets.append("followup_date = %s"); params.append(follow)

        new_status = data.status or forced_status
        if new_status is None and data.outcome:
            # A call that reached a human has, at minimum, contacted them.
            new_status = {"not_interested": "dead", "callback": "warm",
                          "answered": "contacted", "voicemail": "contacted",
                          "gatekeeper": "contacted"}.get(data.outcome)
        if new_status and lead["status"] == "new":
            sets.append("status = %s"); params.append(new_status)
        elif new_status and new_status in ("warm", "won", "dead"):
            sets.append("status = %s"); params.append(new_status)

        note_text = data.note
        if forced_status == "dead" and not note_text:
            note_text = f"No contact after {attempt} attempts — retired by the cadence."
        if note_text:
            stamped = f"[{today}] {data.kind}"
            if data.outcome:
                stamped += f" · {data.outcome}"
            if data.kind == "call":
                stamped += f" · attempt {attempt}"
            stamped += f": {note_text}"
            sets.append("notes = COALESCE(notes || E'\\n', '') || %s")
            params.append(stamped)

        undo_id = _snapshot(cursor, lead, f"log {data.kind}")
        _attach_touch(cursor, undo_id, _record_touch(
            cursor, lead, data.kind, data.outcome, attempt, lead.get("tz_offset_hours")))
        params.append(lead_id)
        cursor.execute(f"UPDATE crm_leads SET {', '.join(sets)} WHERE id = %s RETURNING *", params)
        updated = cursor.fetchone()

        # Same transaction as the lead update: the scoreboard and the pipeline
        # can't drift apart if they move together.
        # Roll the day over first. Without this, a touch made after the CRM-local
        # midnight but before anyone opens the counters spends YESTERDAY's
        # remaining count, and the next counters read resets to quota and erases
        # it — the day's activity numbers then under-report the work done.
        _load_counters_locked(cursor)
        counter_col = {"call": "daily_calls_remaining",
                       "email": "daily_emails_remaining",
                       "fb": "daily_fb_remaining"}[data.kind]
        cursor.execute(f"""
            UPDATE crm_counters
               SET {counter_col} = GREATEST(0, {counter_col} - 1),
                   touch_ticker_remaining = GREATEST(0, touch_ticker_remaining - 1),
                   touch_ticker_last_action = %s,
                   updated_at = %s
             WHERE id = 1
            RETURNING *
        """, (data.kind, now))
        counters = cursor.fetchone()
        conn.commit()
        return {"lead": _lead_row(updated), "undo_id": undo_id,
                "counters": _counters_row(counters) if counters else None}


# ============== SENDING MAIL ==============
#
# The Email button used to be a mailto: link, which hands the job to whatever
# mail client the browser happens to have registered and then loses track of
# it: nothing comes back to say what was sent, or whether it was sent at all,
# so the pipeline can't count it and the lead can't be marked. Sending from
# the server over SMTP closes that loop — the message goes out from the real
# mailbox, and the same transaction stamps the lead and spends the counter.

# What the model is allowed to say about the product. Everything here is
# either a fact from the repo or an env var the operator sets — the model gets
# no licence to invent a feature, a price, a statistic or, above all, a URL.
# A cold email with a made-up link is worse than no email.
COMPANY_NAME = os.getenv("COMPANY_NAME", "86'd")
COMPANY_WEBSITE = os.getenv("COMPANY_WEBSITE", "https://my86d.com")
COMPANY_APP_URL = os.getenv("COMPANY_APP_URL", "")
COMPANY_BLURB = os.getenv("COMPANY_BLURB", "").strip()

DEFAULT_BLURB = (
    "86'd is an iPhone app that counts a bar's inventory by camera. You point "
    "the phone at a bottle, it identifies the bottle, you tap in the count on a "
    "number pad, and at the end it builds the order for each distributor and "
    "emails it to them. Par levels, prices and which distributor supplies each "
    "bottle are set once per bottle and remembered, so a re-count only asks for "
    "the number. It keeps order history and spend by distributor. It is a "
    "subscription, billed monthly, with a free trial."
)


def _draft_system(lead, sender_name: str) -> str:
    """The brief the drafting model works from. Facts only."""
    facts = [f"Product: {COMPANY_NAME}", COMPANY_BLURB or DEFAULT_BLURB]
    links = []
    if COMPANY_WEBSITE:
        links.append(f"Website: {COMPANY_WEBSITE}")
    if COMPANY_APP_URL:
        links.append(f"App Store listing: {COMPANY_APP_URL}")
    facts.append("\n".join(links) if links
                 else "NO LINKS ARE AVAILABLE. Do not include any URL.")

    about = [f"The recipient is {lead['name']}"]
    if lead.get("loc"):
        about.append(f"in {lead['loc']}")
    who = lead.get("contact") or lead.get("manager_name")
    if who:
        role = lead.get("manager_role") or "the manager"
        about.append(f"— the contact there is {who} ({role})")
    if lead.get("opener"):
        about.append(f". Something true about the venue, from their own website: "
                     f"{lead['opener']}")

    return f"""You write a single short sales email for a salesperson to send from their own mailbox.

{chr(10).join(facts)}

{' '.join(about)}.

The sender is {sender_name}. Sign off as them.

Return ONLY a JSON object: {{"subject": "...", "body": "..."}}

Rules, in order of importance:
1. NEVER invent a fact. No URL that is not listed above, no price, no
   percentage, no customer count, no feature that is not described above. If
   the salesperson asks for a link you have not been given, leave it out and
   say nothing about it.
2. Write it as one person emailing another. Plain text, no marketing voice, no
   "I hope this email finds you well", no bullet-point feature lists, no
   exclamation marks.
3. Short. Four sentences or so in the body. A bar manager reads this on their
   phone between deliveries.
4. The subject line is specific and lowercase-ish, like a person typed it —
   not a headline and not in Title Case.
5. Do what the salesperson asked for in their instruction. If they say include
   the website, include it. If they say keep it short, cut it further.
6. Plain text only: no HTML, no markdown, no asterisks for bold."""


BRIEF_SYSTEM = """You write two or three short notes for a salesperson about to phone a bar.

They sell 86'd, an iPhone app that counts bar inventory by camera and builds the distributor order.

You will be given a list of FACTS about the venue. Every fact says where it came from.

Return ONLY a JSON object: {"points": ["...", "..."]}

Rules:
1. Use ONLY the facts given. Invent nothing — no cuisine, no size, no age, no
   owner, no claim of any kind that is not in the list. If the facts are thin,
   return fewer points. Two good notes beat four padded ones.
2. Each point is one short line a person can glance at mid-dial. No preamble.
3. Say what a fact MEANS for this pitch, not just the fact again. "Open 7 days
   until 2am" on its own is already on the screen; "that's a lot of pours to
   count by hand on a Sunday night" is the note worth having.
4. Where a fact came from their website, you may say so ("their site says").
   Where it came from a map, do not state it as certain — make it a question
   worth asking.
5. No greeting, no sign-off, no exclamation marks, no sales language."""


@crm_router.get("/leads/{lead_id}/brief", response_model=dict)
def lead_brief(lead_id: str, refresh: bool = False,
               _: bool = Depends(require_crm_key)):
    """What's worth knowing about this venue before the phone rings.

    The facts are extracted, never generated, and each carries its source — a
    brief that asserts something wrong is the moment the person on the other
    end decides you're reading a script. The talking points on top are written
    only from those facts, and cached, because they don't change between
    dials and nobody should wait on a model with a phone in their hand.
    """
    import venue

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})

    facts = venue.loads(row.get("venue_facts"))
    lines = venue.facts_to_lines(facts)

    cached = row.get("call_brief")
    if cached and not refresh:
        try:
            points = json.loads(cached)
        except (TypeError, ValueError):
            points = []
        return {"facts": lines, "points": points, "cached": True}

    if not lines or not os.getenv("ANTHROPIC_API_KEY"):
        # Facts alone are still worth showing — they're the part that had to
        # be true anyway.
        return {"facts": lines, "points": [], "cached": False}

    described = "\n".join(f"- {l['text']} (from {l['source']})" for l in lines)
    where = row.get("loc") or ""
    try:
        out = _ask_claude(
            BRIEF_SYSTEM,
            f"Venue: {row['name']}" + (f", {where}" if where else "")
            + f"\n\nFacts:\n{described}",
            max_tokens=350)
        points = [str(p).strip()[:220] for p in (out.get("points") or [])][:3]
    except HTTPException:
        # A model that's down must not take the facts down with it.
        return {"facts": lines, "points": [], "cached": False}

    if points:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE crm_leads SET call_brief = %s WHERE id = %s",
                           (json.dumps(points)[:4000], lead_id))
            conn.commit()
    return {"facts": lines, "points": points, "cached": False}


@crm_router.get("/leads/{lead_id}/send-slots", response_model=dict)
def send_slots(lead_id: str, _: bool = Depends(require_crm_key)):
    """Good times to land an email at this venue, in both clocks.

    The rush that makes a badly-timed email useless is the VENUE's rush, so
    every slot is worked out in the venue's local time — then shown in the
    operator's as well, because they are half a day away and "2pm Tuesday"
    tells them nothing about whether they'll be awake for it.

    The windows are the same ones the call list uses. An email is gentler than
    a phone call, but the reasoning holds: read it while setting up or in the
    afternoon lull, not at half past seven with three tickets on the rail.
    """
    from callwindow import (LUNCH_WINDOW, PRE_OPEN_MINUTES, opens_at,
                            parse_opening_hours, service_of)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})

    venue_now = _venue_now(row.get("tz_name"), row.get("tz_offset_hours"))
    op_tz = _operator_tz()
    schedule = parse_opening_hours(row.get("opening_hours"))
    lunch = service_of(row.get("opening_hours")) == "lunch"

    def at(day_offset: int, minutes: int) -> datetime:
        base = (venue_now + timedelta(days=day_offset)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return base + timedelta(minutes=minutes)

    slots = []
    seen = set()

    def add(when: datetime, label: str, why: str):
        # Never offer a time that has already gone by.
        if when <= venue_now + timedelta(minutes=2):
            return
        key = when.isoformat(timespec="minutes")
        if key in seen:
            return
        seen.add(key)
        utc = when.astimezone(timezone.utc)
        slots.append({
            "send_at": utc.isoformat(),
            "label": label,
            "why": why,
            "venue_time": when.strftime("%a %-I:%M%p").lower(),
            "your_time": utc.astimezone(op_tz).strftime("%a %-I:%M%p").lower(),
        })

    # Today and the next few days, so "tomorrow morning" still works on a
    # venue that's shut tomorrow.
    for day in range(0, 5):
        weekday = (venue_now + timedelta(days=day)).weekday()
        if schedule is not None and not schedule.get(weekday):
            continue                       # they're shut that day
        open_min = opens_at(schedule, weekday)
        when_word = "today" if day == 0 else (
            "tomorrow" if day == 1 else (venue_now + timedelta(days=day)).strftime("%A"))
        if open_min is not None:
            add(at(day, max(0, open_min - PRE_OPEN_MINUTES)),
                f"{when_word}, before they open",
                "staff are in setting up and nobody has ordered yet")
        if lunch or open_min is None:
            add(at(day, LUNCH_WINDOW[0]), f"{when_word} afternoon",
                "the lull after lunch, when the manager does the ordering")
        if len(slots) >= 4:
            break

    return {
        "slots": slots[:4],
        "venue_now": venue_now.strftime("%a %-I:%M%p").lower(),
        "your_now": datetime.now(op_tz).strftime("%a %-I:%M%p").lower(),
        "venue_zone": row.get("tz_name") or "",
        "your_zone": OPERATOR_TZ,
    }


class DraftRequest(BaseModel):
    brief: str = Field(min_length=1, max_length=2000)
    # Present on a revision: the draft on screen right now, which the model
    # edits rather than replacing from scratch.
    subject: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, max_length=20000)


@crm_router.post("/leads/{lead_id}/draft-email", response_model=dict)
def draft_lead_email(lead_id: str, data: DraftRequest,
                     _: bool = Depends(require_crm_key)):
    """Turn a sentence of intent into a subject and a body.

    Called twice in a typical send: once to write the thing, then again with
    the current draft attached to change it ("shorter", "mention the free
    trial", "he asked about price"). The revision path carries the draft so a
    tweak edits what is on screen instead of starting over and losing the bit
    that was already right.
    """
    import mailer

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})
    lead = _lead_row(row)

    sender_name = (os.getenv("SPACEMAIL_FROM_NAME")
                   or (mailer.sender() or "").split("@")[0] or "me")
    system = _draft_system(lead, sender_name)

    if data.subject or data.body:
        ask = (f"Here is the current draft.\n\nSubject: {data.subject or ''}\n\n"
               f"{data.body or ''}\n\n---\n\nChange it as follows, keeping "
               f"everything else as it is: {data.brief}")
    else:
        ask = f"Write the email. What it needs to say: {data.brief}"

    out = _ask_claude(system, ask, max_tokens=900)
    subject = str(out.get("subject") or "").strip()[:200]
    body = str(out.get("body") or "").strip()[:20000]
    if not subject or not body:
        raise HTTPException(status_code=502, detail={
            "error": "draft_incomplete",
            "message": "The draft came back empty — try saying it a different way."})
    return {"subject": subject, "body": body}


class OutgoingEmail(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    to: Optional[str] = Field(default=None, max_length=320)
    # UTC ISO 8601. Present means "hold it until then" rather than send now.
    send_at: Optional[str] = Field(default=None, max_length=40)


@crm_router.get("/mail/status", response_model=dict)
def mail_status(_: bool = Depends(require_crm_key)):
    """Whether the server can send, so the page knows which button to show."""
    import mailer
    return {"configured": mailer.is_configured(), "from": mailer.sender(),
            "host": mailer.HOST, "port": mailer.PORT,
            # The page hides the "draft it for me" box rather than offering a
            # button that can only fail.
            "ai": bool(os.getenv("ANTHROPIC_API_KEY"))}


@crm_router.post("/leads/{lead_id}/send-email", response_model=dict)
def send_lead_email(lead_id: str, data: OutgoingEmail,
                    _: bool = Depends(require_crm_key)):
    """Send from the real mailbox, then record it as a touch in one go.

    The send happens BEFORE the database work on purpose. A message that went
    out but wasn't recorded is a lead you might email twice; a database row
    saying "sent" for a message that never left is a follow-up you will wait
    for forever. The first is recoverable by looking at the sent folder. The
    second isn't recoverable at all.
    """
    import mailer

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
        lead = cursor.fetchone()
    if not lead:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})

    to = (data.to or lead["email"] or "").strip()
    if not mailer.valid_address(to):
        raise HTTPException(status_code=422, detail={
            "error": "no_address",
            "message": f"No usable email address for {lead['name']}."})

    if data.send_at:
        return _queue_email(lead, to, data)

    try:
        sent = mailer.send(to, data.subject, data.body)
    except mailer.MailNotConfigured as exc:
        raise HTTPException(status_code=503, detail={
            "error": "mail_not_configured", "message": str(exc)})
    except mailer.MailFailed as exc:
        raise HTTPException(status_code=502, detail={
            "error": "mail_failed", "message": str(exc)})

    today = _today()
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        undo_id = _record_email_sent(cursor, lead_id, to, data.subject, today, now)
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
        updated = cursor.fetchone()
        cursor.execute("SELECT * FROM crm_counters WHERE id = 1")
        counters = cursor.fetchone()
        conn.commit()

    return {"lead": _lead_row(updated), "undo_id": undo_id, "sent": sent,
            "counters": _counters_row(counters) if counters else None}


def _queue_email(lead, to: str, data) -> dict:
    """Hold an approved email until the venue's quiet hour.

    Nothing is stamped on the lead beyond the badge: the touch happens when
    the mail actually goes, not when it was written. Until then the bar is
    still on the call list and still callable, which is right — scheduling a
    note for Tuesday is not a reason to stop ringing them today.
    """
    try:
        when = datetime.fromisoformat(data.send_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail={
            "error": "bad_time",
            "message": "That send time isn't a date I can read."})
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    if when <= now:
        raise HTTPException(status_code=422, detail={
            "error": "bad_time", "message": "That time has already passed."})
    if when > now + timedelta(days=90):
        raise HTTPException(status_code=422, detail={
            "error": "bad_time",
            "message": "That's more than three months out — pick something sooner."})

    queue_id = generate_id()
    with get_db() as conn:
        cursor = conn.cursor()
        # One pending email per lead. Queueing a second replaces the first
        # rather than silently sending the same bar two emails on a timer.
        cursor.execute(
            "UPDATE crm_scheduled_emails SET status = 'replaced' "
            " WHERE lead_id = %s AND status = 'pending'", (lead["id"],))
        replaced = cursor.rowcount
        cursor.execute("""
            INSERT INTO crm_scheduled_emails
                (id, lead_id, to_addr, subject, body, send_at, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
        """, (queue_id, lead["id"], to, data.subject.strip(), data.body,
              when.isoformat(), now_iso()))
        cursor.execute("UPDATE crm_leads SET queued_email_at = %s WHERE id = %s",
                       (when.isoformat(), lead["id"]))
        conn.commit()

    op = when.astimezone(_operator_tz())
    venue = when.astimezone(timezone.utc) + timedelta(
        hours=lead.get("tz_offset_hours") or 0)
    if lead.get("tz_name"):
        try:
            venue = when.astimezone(ZoneInfo(lead["tz_name"]))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return {
        "queued": True, "id": queue_id, "to": to, "replaced": replaced,
        "send_at": when.isoformat(),
        "venue_time": venue.strftime("%a %-I:%M%p").lower(),
        "your_time": op.strftime("%a %-I:%M%p").lower(),
    }


@crm_router.get("/scheduled", response_model=dict)
def list_scheduled(_: bool = Depends(require_crm_key)):
    """Mail waiting to go, and anything that tried and failed.

    Failures matter more than the pending list: an email that silently didn't
    send is a follow-up you believe happened. They stay here until dismissed.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT q.*, l.name AS lead_name, l.loc, l.tz_name, l.tz_offset_hours
              FROM crm_scheduled_emails q
              JOIN crm_leads l ON l.id = q.lead_id
             WHERE q.status IN ('pending', 'failed')
             ORDER BY q.send_at ASC
             LIMIT 100
        """)
        rows = [dict(r) for r in cursor.fetchall()]

    op_tz = _operator_tz()
    out = []
    for row in rows:
        try:
            when = datetime.fromisoformat(row["send_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        venue = when + timedelta(hours=row.get("tz_offset_hours") or 0)
        if row.get("tz_name"):
            try:
                venue = when.astimezone(ZoneInfo(row["tz_name"]))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        out.append({
            "id": row["id"], "lead_id": row["lead_id"], "name": row["lead_name"],
            "loc": row["loc"], "to": row["to_addr"], "subject": row["subject"],
            "status": row["status"], "attempts": row["attempts"],
            "error": row["last_error"],
            "venue_time": venue.strftime("%a %-I:%M%p").lower(),
            "your_time": when.astimezone(op_tz).strftime("%a %-I:%M%p").lower(),
            "overdue": row["status"] == "pending" and when < datetime.now(timezone.utc),
        })
    return {"queued": [q for q in out if q["status"] == "pending"],
            "failed": [q for q in out if q["status"] == "failed"],
            "count": len(out)}


@crm_router.delete("/scheduled/{queue_id}", response_model=dict)
def cancel_scheduled(queue_id: str, _: bool = Depends(require_crm_key)):
    """Call it back before it goes. Also how a failure is dismissed."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE crm_scheduled_emails SET status = 'cancelled' "
            " WHERE id = %s AND status IN ('pending', 'failed') RETURNING lead_id",
            (queue_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Nothing pending under that id — it may have already gone."})
        cursor.execute("""
            UPDATE crm_leads SET queued_email_at = NULL
             WHERE id = %s AND NOT EXISTS (
                SELECT 1 FROM crm_scheduled_emails
                 WHERE lead_id = %s AND status = 'pending')
        """, (row["lead_id"], row["lead_id"]))
        conn.commit()
    return {"cancelled": True}


def run_due_emails(limit: int = 20) -> dict:
    """Send whatever is due. Called on a timer; safe to call at any moment.

    Each row is claimed with a conditional UPDATE before the send, so two
    workers — or one worker and a restart mid-flight — can't send the same
    email twice. Sending twice is the failure that matters here: the recipient
    sees it, and no amount of tidying up afterwards unsends it.
    """
    import mailer

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    sent = failed = skipped = 0
    while sent + failed + skipped < limit:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE crm_scheduled_emails
                   SET status = 'sending', attempts = attempts + 1
                 WHERE id = (
                    SELECT id FROM crm_scheduled_emails
                     WHERE status = 'pending' AND send_at <= %s
                     ORDER BY send_at ASC
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED)
                RETURNING *
            """, (now,))
            job = cursor.fetchone()
            conn.commit()
        if not job:
            break

        # How late is it? On Render's free tier this process sleeps after ~15
        # minutes idle and only wakes on a request, so a 2pm send can surface
        # at 6pm — landing "I know you're quiet right now" mail in the middle
        # of service. That is the exact harm scheduling exists to prevent, so
        # a badly-late email is held back and reported rather than fired.
        try:
            due = datetime.fromisoformat(job["send_at"].replace("Z", "+00:00"))
            late_minutes = (now_dt - due).total_seconds() / 60
        except ValueError:
            late_minutes = 0
        if late_minutes > STALE_AFTER_MINUTES:
            skipped += 1
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE crm_scheduled_emails SET status = 'failed', last_error = %s
                     WHERE id = %s
                """, (f"Not sent: it came due {int(late_minutes)} minutes ago and the "
                      f"window has passed. Reschedule it rather than landing "
                      f"mid-service.", job["id"]))
                cursor.execute("""
                    UPDATE crm_leads SET queued_email_at = NULL
                     WHERE id = %s AND NOT EXISTS (
                        SELECT 1 FROM crm_scheduled_emails
                         WHERE lead_id = %s AND status = 'pending')
                """, (job["lead_id"], job["lead_id"]))
                conn.commit()
            print(f"[crm] held back a {int(late_minutes)}min-late email to "
                  f"{job['to_addr']} — window gone", flush=True)
            continue

        try:
            mailer.send(job["to_addr"], job["subject"], job["body"])
        except Exception as exc:
            failed += 1
            # Left as 'failed' rather than retried forever: a bad address or a
            # rejected login will not fix itself, and the operator needs to
            # see it rather than have it quietly loop.
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE crm_scheduled_emails SET status = 'failed', last_error = %s "
                    " WHERE id = %s", (str(exc)[:400], job["id"]))
                # Take the "queued" badge off: nothing is queued any more, and
                # a row still reading QUEUED next to an email that failed an
                # hour ago is the page telling a comfortable lie. The red bar
                # above the list is the honest signal.
                cursor.execute("""
                    UPDATE crm_leads SET queued_email_at = NULL
                     WHERE id = %s AND NOT EXISTS (
                        SELECT 1 FROM crm_scheduled_emails
                         WHERE lead_id = %s AND status = 'pending')
                """, (job["lead_id"], job["lead_id"]))
                conn.commit()
            print(f"[crm] scheduled email to {job['to_addr']} failed: {exc}", flush=True)
            continue

        sent += 1
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE crm_scheduled_emails SET status = 'sent', sent_at = %s "
                " WHERE id = %s", (now_iso(), job["id"]))
            try:
                _record_email_sent(cursor, job["lead_id"], job["to_addr"],
                                   job["subject"], _today(), now_iso())
            except Exception as exc:
                # The mail is already gone; a bookkeeping failure must not make
                # it look unsent. Say so loudly and keep the 'sent' status.
                print(f"[crm] sent {job['id']} but could not stamp the lead: {exc}",
                      flush=True)
            conn.commit()
        print(f"[crm] scheduled email sent to {job['to_addr']}", flush=True)

    if sent or failed or skipped:
        print(f"[crm] SCHEDULED_EMAILS sent={sent} failed={failed} "
              f"window_missed={skipped}", flush=True)
    return {"sent": sent, "failed": failed, "window_missed": skipped}


def _record_email_sent(cursor, lead_id: str, to: str, subject: str,
                       today: str, now: str) -> str:
    """Everything that happens to a lead once mail has actually gone out.

    Shared by the send-now path and the scheduled worker so a queued email
    lands in the pipeline identically to one sent by hand — same stamp, same
    note, same counter, same undo.
    """
    if True:
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE", (lead_id,))
        lead = cursor.fetchone()
        undo_id = _snapshot(cursor, lead, "log email")

        note = f"[{today}] email to {to}: {subject.strip()}"
        sets = ["updated_at = %s", "last_touch_at = %s", "email_date = %s",
                "last_outcome = %s",
                "notes = COALESCE(notes || E'\n', '') || %s"]
        params: list = [now, now, today, "emailed", note]
        # A lead we've now written to is no longer untouched, but emailing
        # somebody is not the same as reaching them: the status only moves off
        # 'new' so it leaves the call list, never to 'warm'.
        if lead["status"] == "new":
            sets.append("status = %s"); params.append("contacted")
        # Learned a better address? Keep it.
        if to.lower() != (lead["email"] or "").lower():
            sets.append("email = %s"); params.append(to)

        _attach_touch(cursor, undo_id, _record_touch(
            cursor, lead, "email", "emailed", lead["attempts"] or 0,
            lead.get("tz_offset_hours")))
        # Whatever was queued has now gone, so the badge comes off.
        sets.append("queued_email_at = NULL")
        params.append(lead_id)
        cursor.execute(f"UPDATE crm_leads SET {', '.join(sets)} WHERE id = %s", params)

        # Same rollover as /touch above — and it matters more here, because a
        # scheduled send fires on its own with nobody watching the screen.
        _load_counters_locked(cursor)
        cursor.execute("""
            UPDATE crm_counters
               SET daily_emails_remaining = GREATEST(0, daily_emails_remaining - 1),
                   touch_ticker_remaining = GREATEST(0, touch_ticker_remaining - 1),
                   touch_ticker_last_action = 'email',
                   updated_at = %s
             WHERE id = 1
        """, (now,))
        return undo_id


# ============== UNDO ==============
#
# Logging a call takes a lead off the list, which is the point — it's what
# makes it impossible to ring the same restaurant twice, and what makes the
# shrinking list a progress bar. But it also means a misclick is destructive
# and invisible: the row is simply gone, and nothing on the screen says where
# it went. So every touch is reversible, and the reversal is exact.

@crm_router.get("/undo", response_model=dict)
def recent_touches(limit: int = 15, _: bool = Depends(require_crm_key)):
    """What was just worked, newest first, each still putting-back-able.

    Deliberately a list rather than only a toast: the moment someone notices
    they logged the wrong row is often several calls later, and a five-second
    undo that has already faded is not a safety net.
    """
    limit = max(1, min(limit, 100))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.id, u.lead_id, u.action, u.created_at, u.restored_at,
                   l.name, l.loc, l.phone, l.status, l.last_outcome
              FROM crm_lead_undo u
              JOIN crm_leads l ON l.id = u.lead_id
             WHERE u.restored_at IS NULL
             ORDER BY u.created_at DESC
             LIMIT %s
        """, (limit,))
        rows = [dict(r) for r in cursor.fetchall()]
    for row in rows:
        row["phone_digits"] = phone_digits(row.get("phone"))
    return {"touches": rows, "count": len(rows)}


@crm_router.post("/undo/{undo_id}", response_model=dict)
def undo_touch(undo_id: str, _: bool = Depends(require_crm_key)):
    """Put a lead back exactly as it was, and refund what the touch spent.

    The counters are refunded because they are a record of calls actually
    made. A call that didn't happen shouldn't show up in the day's numbers, or
    the scoreboard stops meaning anything — which is the same reason `touch`
    spends them in one transaction in the first place.
    """
    import json as _json
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM crm_lead_undo WHERE id = %s FOR UPDATE", (undo_id,))
        undo = cursor.fetchone()
        if not undo:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Nothing to undo under that id"})
        if undo["restored_at"]:
            raise HTTPException(status_code=409, detail={
                "error": "already_restored",
                "message": "That one has already been put back"})

        try:
            snapshot = _json.loads(undo["snapshot"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=500, detail={
                "error": "bad_snapshot",
                "message": "That undo record is unreadable — edit the lead by hand"})

        # Only columns we wrote, so a snapshot taken by an older build can't
        # inject a column name into the SQL.
        fields = [c for c in UNDO_COLUMNS if c in snapshot]
        sets = ", ".join(f"{c} = %s" for c in fields)
        params = [snapshot[c] for c in fields]
        params.append(undo["lead_id"])
        cursor.execute(
            f"UPDATE crm_leads SET {sets} WHERE id = %s RETURNING *", params)
        restored = cursor.fetchone()
        if not restored:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "That lead has since been deleted"})

        # The dial log is history, not state: the call was logged and then
        # unlogged, and both of those happened. Marking it rather than deleting
        # it keeps the connect-rate numbers honest about what was actually
        # dialled — see /dialstats.
        # The touch this undo was recorded against. Falling back to the newest
        # one covers undo rows written before touch_id existed; for anything
        # written since, the pairing is exact.
        if undo.get("touch_id"):
            cursor.execute(
                "UPDATE crm_touches SET outcome = 'undone', connected = FALSE WHERE id = %s",
                (undo["touch_id"],))
        else:
            cursor.execute("""
                UPDATE crm_touches SET outcome = 'undone', connected = FALSE
                 WHERE id = (SELECT id FROM crm_touches WHERE lead_id = %s
                              ORDER BY at DESC LIMIT 1)
            """, (undo["lead_id"],))

        if undo["counters_spent"]:
            kind = (undo["action"] or "").split()[-1]
            counter_col = {"call": "daily_calls_remaining",
                           "email": "daily_emails_remaining",
                           "fb": "daily_fb_remaining"}.get(kind)
            if counter_col:
                # And before a refund: crediting yesterday's number back is the
                # same erasure, and leaves a call that didn't happen in the day's
                # totals — which is exactly what the undo exists to take out.
                _load_counters_locked(cursor)
                cursor.execute(f"""
                    UPDATE crm_counters
                       SET {counter_col} = {counter_col} + %s,
                           touch_ticker_remaining = touch_ticker_remaining + %s,
                           updated_at = %s
                     WHERE id = 1
                """, (undo["counters_spent"], undo["counters_spent"], now_iso()))

        cursor.execute("UPDATE crm_lead_undo SET restored_at = %s WHERE id = %s",
                       (now_iso(), undo_id))
        conn.commit()
    return {"lead": _lead_row(restored), "restored": True}


# ============== SUPPRESSION (do-not-call) ==============

class SuppressionCreate(BaseModel):
    kind: Literal["email", "phone", "domain", "name"]
    value: str = Field(min_length=1, max_length=320)
    reason: Optional[str] = Field(default=None, max_length=500)


@crm_router.get("/suppressions", response_model=dict)
def list_suppressions(_: bool = Depends(require_crm_key)):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_suppressions ORDER BY created_at DESC")
        return {"suppressions": [dict(r) for r in cursor.fetchall()]}


@crm_router.post("/suppressions", response_model=dict, status_code=201)
def add_suppression(data: SuppressionCreate, _: bool = Depends(require_crm_key)):
    """Never contact this again — a do-not-call, a competitor, a bad fit.

    Checked at promote time, so a suppressed venue can never re-enter the
    pipeline through the lead generator either.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crm_suppressions (id, kind, value, reason, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (kind, LOWER(value)) DO UPDATE SET reason = EXCLUDED.reason
            RETURNING *
        """, (generate_id(), data.kind, data.value.strip(), data.reason, now_iso()))
        row = cursor.fetchone()
        # Retire anything already in the pipeline that this now covers.
        # The column is chosen from a literal map rather than interpolated from
        # data.kind. It was already safe — Pydantic pins kind to a Literal and
        # the tuple check narrowed it further — but a column name reaching an
        # f-string from a request body is one widened Literal away from being an
        # injection, and there is no reason to depend on a guard here.
        SUPPRESS_SQL = {
            "email": "UPDATE crm_leads SET status='dead', updated_at=%s WHERE LOWER(email) = LOWER(%s)",
            "phone": "UPDATE crm_leads SET status='dead', updated_at=%s WHERE LOWER(phone) = LOWER(%s)",
        }
        statement = SUPPRESS_SQL.get(data.kind)
        if statement:
            cursor.execute(statement, (now_iso(), data.value.strip()))
        conn.commit()
        return {"suppression": dict(row)}


# ============== LEAD GENERATOR ==============

@crm_router.get("/leadgen/today", response_model=dict)
def leadgen_today(_: bool = Depends(require_crm_key)):
    """Today's sourced leads, ordered for dialling."""
    today = _today()
    with get_db() as conn:
        cursor = conn.cursor()
        # won/dead excluded: a lead suppressed or closed since this morning
        # must not still be sitting in the call list. Calling someone who asked
        # not to be contacted is the one mistake this list must never cause.
        cursor.execute("""
            SELECT * FROM crm_leads
             WHERE source = 'leadgen' AND SUBSTRING(created_at, 1, 10) = %s
               AND status NOT IN ('won', 'dead')
             ORDER BY created_at ASC
        """, (today,))
        rows = cursor.fetchall()
        leads = []
        for row in rows:
            lead = _lead_row(row)
            lead["call_window"] = _call_window(row.get("tz_offset_hours"), row.get("opening_hours"))
            leads.append(lead)
        # Ring the ones whose local afternoon it is right now, first.
        leads.sort(key=lambda l: (not l["call_window"]["good_now"], l["name"]))
        return {"date": today, "count": len(leads), "leads": leads}


@crm_router.get("/leadgen/health", response_model=dict)
def leadgen_health(_: bool = Depends(require_crm_key)):
    """Is the generator actually working? Built to make silence impossible.

    `stale` is the field that matters: no successful run in over 36 hours means
    the daily list has quietly stopped, which is exactly the failure that would
    otherwise go unnoticed until a morning with nothing to call.
    """
    from leadgen import pool_depth, DAILY_TARGET, MAX_ACTIVE, BUCKET_TARGET
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leadgen_runs ORDER BY started_at DESC LIMIT 10")
        runs = [dict(r) for r in cursor.fetchall()]
        cursor.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE enabled AND last_harvested_at IS NULL) AS unharvested
              FROM crm_leadgen_cities
        """)
        cities = cursor.fetchone()

    last_ok = next((r for r in runs if r["ok"]), None)
    stale = True
    hours_since = None
    if last_ok and last_ok.get("finished_at"):
        try:
            when = datetime.fromisoformat(last_ok["finished_at"].replace("Z", "+00:00"))
            hours_since = round((datetime.now(timezone.utc) - when).total_seconds() / 3600, 1)
            stale = hours_since > 36
        except ValueError:
            pass

    depth = pool_depth()
    warnings = []
    # A full list is a normal, healthy resting state, not a fault — so it is
    # reported as a note and never as a warning, and it suppresses the
    # "running low" warnings that would otherwise contradict it.
    at_cap = depth["at_capacity"]
    if stale and not at_cap:
        warnings.append("No successful run in the last 36 hours — the daily list has stopped.")
    if not at_cap:
        if depth["qualified"] < DAILY_TARGET and depth["headroom"] > DAILY_TARGET:
            warnings.append(
                f"Pool has {depth['qualified']} qualified leads, under one day's target "
                f"({DAILY_TARGET}) — tomorrow's list may come up short.")
        elif depth["days_of_runway"] < 3:
            warnings.append(f"Only {depth['days_of_runway']} days of leads banked.")
        if cities["unharvested"] == 0:
            warnings.append("Every city has been harvested at least once — add more territory.")
    stuck = [r for r in runs if r["phase"] == "running"]
    if len(stuck) > 1:
        warnings.append(
            f"{len(stuck)} runs are still marked in-progress — the process was probably "
            "restarted mid-run. They're reconciled automatically on the next run.")

    # Thin tabs are the failure the operator actually feels — a full-looking
    # total with an empty Eastern lunch tab is a morning with nothing to call —
    # so they get their own warning rather than hiding inside the total.
    thin = [b for b in depth["buckets"] if b["count"] < BUCKET_TARGET // 2]
    if thin and not at_cap:
        worst = ", ".join(f"{b['service']} {ZONE_LABELS.get(b['zone'], b['zone'])} "
                          f"({b['count']})" for b in sorted(thin, key=lambda b: b["count"])[:3])
        warnings.append(
            f"Some tabs are nearly empty: {worst}. The generator fills the "
            "emptiest first, but it needs territory in those zones.")

    if at_cap:
        note = (f"Every tab is full — {BUCKET_TARGET} leads in each of "
                f"{len(depth['buckets'])} service/timezone combinations "
                f"({depth['active_leads']} total). Generation is paused until "
                "you work some off.")
    else:
        note = (f"{depth['headroom']} spots open across "
                f"{len(depth['buckets'])} tabs (target {BUCKET_TARGET} each) — "
                "the emptiest get filled first at the next run.")

    return {
        "healthy": (at_cap or not stale) and not warnings,
        "stale": stale,
        "at_capacity": at_cap,
        "note": note,
        "hours_since_last_success": hours_since,
        "pool": depth,
        "cities": {"total": cities["total"], "unharvested": cities["unharvested"]},
        "warnings": warnings,
        "recent_runs": runs,
    }


@crm_router.post("/leadgen/run", response_model=dict)
def leadgen_run(target: Optional[int] = None, max_cities: int = 4,
                max_enrich: int = 120, _: bool = Depends(require_crm_key)):
    """Run the pipeline now, and WAIT for it. Same path the daily scheduler takes.

    Minutes, not seconds — it crawls venue sites. For the operator-facing
    "fill the list, I want to call now" path use /leadgen/fill, which does the
    same work without holding a request open.
    """
    from leadgen import run_daily, DAILY_TARGET
    return run_daily(target=target or DAILY_TARGET, max_cities=max_cities,
                     max_enrich=max_enrich)


# A run takes minutes (it crawls venue websites), which is far too long to hold
# an HTTP request open — so the operator-facing fill starts a thread and the
# page polls. The lock stops a second press, or a second tab, starting a
# parallel run that would crawl the same candidates twice.
_fill_lock = threading.Lock()
_fill_thread: Optional[threading.Thread] = None


def _fill_running() -> bool:
    global _fill_thread
    return _fill_thread is not None and _fill_thread.is_alive()


def _run_fill(target: int, max_cities: int, max_enrich: int) -> None:
    from leadgen import run_daily
    try:
        run_daily(target=target, max_cities=max_cities, max_enrich=max_enrich)
    except Exception as exc:
        # run_daily records its own failures; this only catches a crash before
        # it could. Never let it kill the thread silently.
        print(f"[crm] fill run crashed: {exc}", flush=True)


@crm_router.post("/leadgen/fill", response_model=dict)
def leadgen_fill(max_cities: int = 2, max_enrich: int = 200,
                 _: bool = Depends(require_crm_key)):
    """Start filling the call list now, in the background.

    This exists because the daily schedule is the wrong master for someone who
    has just sat down with a phone. The list refilling at 6pm is no use at 2pm
    to an operator who is ready to work: an empty screen at the moment you
    decide to call is the one state this tool must never be in.

    Returns immediately. Poll GET /leadgen/fill for progress.
    """
    from leadgen import DAILY_TARGET
    global _fill_thread
    with _fill_lock:
        if _fill_running():
            return {"started": False, "running": True,
                    "note": "A fill is already running — leads appear as they land."}
        _fill_thread = threading.Thread(
            target=_run_fill, args=(DAILY_TARGET, max_cities, max_enrich),
            daemon=True, name="leadgen-fill")
        _fill_thread.start()
    return {"started": True, "running": True,
            "note": "Filling the list. It crawls venue sites, so give it a few minutes."}


@crm_router.get("/leadgen/fill", response_model=dict)
def leadgen_fill_status(_: bool = Depends(require_crm_key)):
    """Is a fill in flight, and what did the last one do?"""
    from leadgen import bucket_deficits
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT phase, ok, started_at, finished_at, promoted, enriched,
                   qualified, errors
              FROM crm_leadgen_runs
             ORDER BY started_at DESC LIMIT 1
        """)
        last = cursor.fetchone()
        cursor.execute("""
            SELECT COUNT(*) AS n FROM crm_leads
             WHERE status = 'new' AND last_touch_at IS NULL
        """)
        waiting = cursor.fetchone()["n"]
    return {
        "running": _fill_running(),
        "waiting_to_be_called": waiting,
        "room_left": sum(bucket_deficits().values()),
        "last_run": dict(last) if last else None,
    }


# The restaurant liquor gate got stricter after a real harvested pizzeria
# with no alcohol program qualified through the old LIQUOR_HINTS regex (see
# leadgen._restaurant_pours). Rechecking already-banked/promoted restaurant
# rows means re-crawling their sites — the same "too slow for one request"
# problem /leadgen/fill has — so it gets its own lock, background thread and
# poll endpoint rather than reusing _fill_lock, which is specifically about
# not double-running the harvest/enrich/promote pipeline.
_recheck_lock = threading.Lock()
_recheck_thread: Optional[threading.Thread] = None
_recheck_last: Optional[dict] = None


def _recheck_running() -> bool:
    global _recheck_thread
    return _recheck_thread is not None and _recheck_thread.is_alive()


def _run_recheck(limit: int) -> None:
    global _recheck_last
    from leadgen import recheck_restaurant_leads
    try:
        result = recheck_restaurant_leads(limit=limit)
    except Exception as exc:
        result = {"error": str(exc)}
        print(f"[crm] restaurant recheck crashed: {exc}", flush=True)
    with _recheck_lock:
        _recheck_last = result


@crm_router.post("/leadgen/recheck-restaurants", response_model=dict)
def leadgen_recheck_restaurants(limit: int = 200, _: bool = Depends(require_crm_key)):
    """One-time correction for restaurant rows a since-tightened liquor gate
    would now reject: re-crawls each one and applies the current rule.

    Not part of the daily run and not run automatically on boot — unlike
    _reconcile_bad_emails()/_reconcile_timezones(), this can't be recomputed
    from stored data alone, so it means re-fetching every restaurant-tagged
    candidate's site. Triggered from the Lead engine panel when wanted, not
    on every deploy. Returns immediately; poll GET for the result.
    """
    global _recheck_thread
    with _recheck_lock:
        if _recheck_running():
            return {"started": False, "running": True,
                    "note": "A recheck is already running."}
        _recheck_thread = threading.Thread(
            target=_run_recheck, args=(limit,),
            daemon=True, name="leadgen-recheck-restaurants")
        _recheck_thread.start()
    return {"started": True, "running": True,
            "note": "Rechecking restaurant leads against the current liquor gate."}


@crm_router.get("/leadgen/recheck-restaurants", response_model=dict)
def leadgen_recheck_restaurants_status(_: bool = Depends(require_crm_key)):
    """Is a recheck in flight, and what did the last one find?"""
    with _recheck_lock:
        last = _recheck_last
    return {"running": _recheck_running(), "last_run": last}


class CityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    state: Optional[str] = Field(default=None, max_length=40)
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_m: int = Field(default=12000, ge=1000, le=50000)


@crm_router.get("/leadgen/cities", response_model=dict)
def list_cities(_: bool = Depends(require_crm_key)):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM crm_leadgen_cities
             ORDER BY last_harvested_at ASC NULLS FIRST, name ASC
        """)
        return {"cities": [dict(r) for r in cursor.fetchall()]}


@crm_router.post("/leadgen/cities", response_model=dict, status_code=201)
def add_city(data: CityCreate, _: bool = Depends(require_crm_key)):
    """Add territory. Geocodes the name when coordinates aren't supplied."""
    from leadgen import geocode_city
    lat, lon = data.lat, data.lon
    if lat is None or lon is None:
        located = geocode_city(data.name, data.state)
        if not located:
            raise HTTPException(status_code=422, detail={
                "error": "geocode_failed",
                "message": f"Couldn't locate {data.name}. Pass lat/lon explicitly.",
            })
        lat, lon = located

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crm_leadgen_cities (id, name, state, lat, lon, radius_m, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (LOWER(name), LOWER(COALESCE(state, ''))) DO NOTHING
            RETURNING *
        """, (generate_id(), data.name.strip(), data.state, lat, lon,
              data.radius_m, now_iso()))
        row = cursor.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=409, detail={
                "error": "already_exists", "message": "That city is already on the list"})
        return {"city": dict(row)}


@crm_router.get("/leadgen/export.csv")
def export_csv(scope: str = "today", _: bool = Depends(require_crm_key)):
    """Today's list as CSV, for a dialer or a spreadsheet.

    Deliberately a plain download rather than an integration: every auto-dialer
    takes a CSV, and a file can't break when someone changes dialer.
    """
    import csv, io
    from fastapi.responses import StreamingResponse

    today = _today()
    with get_db() as conn:
        cursor = conn.cursor()
        if scope == "today":
            cursor.execute("""
                SELECT * FROM crm_leads
                 WHERE source='leadgen' AND SUBSTRING(created_at,1,10)=%s
                   AND status NOT IN ('won', 'dead')
                 ORDER BY created_at
            """, (today,))
        elif scope == "queue":
            cursor.execute("""
                SELECT * FROM crm_leads
                 WHERE status NOT IN ('won','dead')
                   AND (followup_date <= %s OR call_date IS NULL)
                 ORDER BY followup_date ASC NULLS LAST
            """, (today,))
        else:
            cursor.execute("SELECT * FROM crm_leads ORDER BY created_at DESC")
        rows = cursor.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Phone", "Email", "Location", "Status",
                     "Last called", "Follow-up", "Local time", "Notes"])
    for row in rows:
        window = _call_window(row.get("tz_offset_hours"), row.get("opening_hours"))
        writer.writerow([
            row["name"], row["phone"] or "", row["email"] or "", row["loc"] or "",
            row["status"], row["call_date"] or "", row["followup_date"] or "",
            window.get("local_time") or "", (row["notes"] or "").replace("\n", " | "),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="86d-leads-{scope}-{today}.csv"'},
    )


# ============== THE CALL LIST ==============
#
# One screen, one job. Everything sourced and not yet worked, grouped by
# timezone, with the zone that's callable right now first.
#
# A lead leaves this list the moment it's been dealt with — a logged call, a
# debrief, a status change, a delete. Nothing has to be tidied up by hand, and
# the same restaurant can't be called twice because it simply isn't there any
# more. The list shrinking IS the progress bar.

@crm_router.get("/calllist", response_model=dict)
def call_list(_: bool = Depends(require_crm_key)):
    """Every unworked lead, split by service then by timezone.

    Two levels because they answer two different questions. The service split
    answers "it's 11am, who is even open?" — a bar that doesn't unlock until
    four is unreachable now and belongs behind a different tab. The timezone
    split answers "who's in their window right now?", and as the afternoon
    rolls west it moves through Eastern, Central, Mountain, Pacific.

    Venues with no hours in OpenStreetMap sit under dinner rather than lunch.
    Filing them under lunch would send late-morning calls to bars that don't
    open until four; under dinner they get the generic afternoon window, which
    is where they'd have been called anyway.
    """
    from callwindow import service_of, ZONE_OFFSETS
    from leadgen import BUCKET_TARGET
    today = _today()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM crm_leads
             WHERE status = 'new' AND last_touch_at IS NULL
               AND phone IS NOT NULL AND phone <> ''
             ORDER BY COALESCE(attempts, 0) ASC, created_at DESC
        """)
        rows = cursor.fetchall()

        cursor.execute("""
            SELECT COUNT(*) AS n FROM crm_leads
             WHERE SUBSTRING(COALESCE(last_touch_at, ''), 1, 10) = %s
        """, (today,))
        done_today = cursor.fetchone()["n"]

    buckets: dict = {"lunch": {}, "dinner": {}}
    usable = 0
    for row in rows:
        lead = _lead_row(row)
        # A number that didn't validate is never offered for dialling.
        if not lead["phone_ok"]:
            continue
        usable += 1
        lead["call_window"] = _call_window(row.get("tz_offset_hours"),
                                           row.get("opening_hours"),
                                           row.get("tz_name"))
        lead["hours_known"] = bool(row.get("opening_hours"))
        service = service_of(row.get("opening_hours"))
        offset = row.get("tz_offset_hours")
        buckets[service].setdefault(offset if offset in ZONE_OFFSETS else None,
                                    []).append(lead)


    def _call_order(lead: dict):
        """Which of two leads to ring first.

        Whether they're reachable right now comes first — the best lead in the
        list is worth nothing while its doors are locked. After that it's how
        far the call can get: a name to ask for beats a direct mailbox, which
        beats a shared inbox, and the generator's fit score breaks the rest.
        Fewest attempts stays ahead of score so an untried lead beats a fourth
        swing at one that never answers.
        """
        # A shared info@ box is the worst of the four, below an address we
        # couldn't classify — an unclassified one is usually the venue's own
        # mailbox (oshaughnessyspub@gmail.com), which somebody there actually
        # reads.
        kind_rank = {"personal": 0, "owner": 1, "unknown": 2, "role": 3}
        return (
            WINDOW_RANK.get(lead["call_window"].get("state"), 2),
            0 if lead.get("manager_name") else 1,
            kind_rank.get(lead.get("email_kind"), 2),
            lead.get("attempts") or 0,
            -(lead.get("lead_score") or 0),
            lead["name"],
        )

    def build_zones(by_zone: dict) -> list:
        # Every zone appears, empty or not. The sub-tabs have to be in the same
        # place every time — a row of tabs that reshuffles itself as leads are
        # worked off is a row you have to re-read before every click.
        for offset in ZONE_OFFSETS:
            by_zone.setdefault(offset, [])
        zones = []
        for offset, leads in by_zone.items():
            zone = zone_state(offset)
            leads.sort(key=_call_order)
            ready = sum(1 for l in leads if l["call_window"]["good_now"])
            zone.update({"leads": leads, "count": len(leads), "callable_now": ready,
                         "target": BUCKET_TARGET,
                         "short_by": max(0, BUCKET_TARGET - len(leads))})
            if not leads:
                zone.update({"headline": "Empty — the generator refills this first",
                             "callable": False, "rank": 6})
                zones.append(zone)
                continue
            if ready:
                zone.update({"headline": f"{ready} ready to call now", "state": "good",
                             "rank": 0, "callable": True})
            else:
                # The soonest window, by minutes from now — not by string. A
                # lexicographic min over "9:00pm-11:00pm" and "11:00am-11:45am"
                # picks the 11am one as "first", which is both wrong and looks
                # like a typo on the screen.
                waits = [(l["call_window"].get("starts_in"), l["call_window"].get("window"))
                         for l in leads if l["call_window"].get("state") == "early"
                         and l["call_window"].get("starts_in") is not None]
                soonest = min(waits)[1] if waits else ""
                zone.update({"headline": (f"None open yet — first window {soonest}"
                                          if soonest else "Nothing ringable here right now"),
                             "callable": False})
            zones.append(zone)
        # Fixed east-to-west order, never re-sorted by how good each one looks
        # right now. Two reasons: the tabs stay where the hand expects them,
        # and east-to-west IS the order the afternoon moves — Eastern hits its
        # window first, then Central, and by the time Eastern is in the dinner
        # rush Pacific is just opening. The "call this one" marker moves; the
        # tabs don't.
        order = {off: i for i, off in enumerate(ZONE_OFFSETS)}
        zones.sort(key=lambda z: order.get(z["offset"], 99))
        best = max(zones, key=lambda z: z["callable_now"], default=None)
        for zone in zones:
            zone["recommended"] = bool(best and zone is best and zone["callable_now"])
        return zones

    services = []
    for key, label, blurb in [
        ("lunch", "Open for lunch", "Doors open by 11:30am — reachable late morning"),
        ("dinner", "Dinner only", "Don't open until later, plus venues with no listed hours"),
    ]:
        zones = build_zones(buckets[key])
        services.append({
            "key": key, "label": label, "blurb": blurb, "zones": zones,
            "count": sum(z["count"] for z in zones),
            "callable_now": sum(z["callable_now"] for z in zones),
        })

    ready = [(svc, z) for svc in services for z in svc["zones"] if z["callable_now"]]
    ready.sort(key=lambda sz: -sz[1]["callable_now"])
    if ready:
        svc, zone = ready[0]
        focus = (f"Call {zone['label']} now — {zone['callable_now']} ready "
                 f"({svc['label'].lower()})")
    else:
        focus = "Nothing in a calling window right now."

    return {
        "date": today, "focus": focus, "done_today": done_today,
        "remaining": usable, "services": services,
        # Kept so anything still reading the old shape doesn't break.
        "zones": services[0]["zones"] + services[1]["zones"],
    }


@crm_router.get("/now", response_model=dict)
def call_now(limit: int = 60, _: bool = Depends(require_crm_key)):
    """One list: who to ring, right now, in order. No tabs, no decisions.

    The service and timezone tabs are the right way to UNDERSTAND the list —
    they say why a venue is reachable or isn't. They are the wrong way to WORK
    it. Sitting down to call, the question isn't "which of eight tabs holds
    somebody who's open"; it's "who do I dial first", and answering that by
    clicking around eight tabs reading clocks is work the screen should have
    already done.

    So this flattens all eight cells into a single queue and sorts it by
    whether each venue is in a calling window this minute — 30 minutes before
    the doors open, and the 2-4pm lull — then by how far the call can get: a
    name to ask for, then a direct mailbox, then fit.

    It crosses timezones freely on purpose. At any given moment the Eastern
    bars setting up and the Pacific ones in their lull are both good calls, and
    which zone they're in doesn't matter once you know it's their quiet half
    hour.
    """
    limit = max(1, min(limit, 200))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM crm_leads
             WHERE status = 'new' AND last_touch_at IS NULL
               AND phone IS NOT NULL AND phone <> ''
        """)
        rows = cursor.fetchall()
        cursor.execute("""
            SELECT COUNT(*) AS n FROM crm_leads
             WHERE SUBSTRING(COALESCE(last_touch_at, ''), 1, 10) = %s
        """, (_today(),))
        done_today = cursor.fetchone()["n"]

    # Every unworked lead the query returned, before any window or phone
    # filtering. This is "is there anything on the list at all", which is a
    # different question from "is anyone reachable this minute".
    unworked = len(rows)

    # Three buckets, and NOTHING is discarded. The window decides the order and
    # the label on a lead; it must never decide whether the lead is on screen at
    # all. This used to be an if/elif with no else, so every venue that was past
    # its window or shut today fell off the end of the loop — and outside US
    # afternoons that is most of the list, which is how pressing "Ready to start
    # calling" emptied a screen with four hundred leads banked behind it. The
    # tab view never did this: it ranked these same rows and showed them. Two
    # screens disagreeing about whether a lead exists is worse than either
    # answer on its own.
    ready, soon, rest = [], [], []
    for row in rows:
        lead = _lead_row(row)
        if not lead["phone_ok"]:
            continue
        window = _call_window(row.get("tz_offset_hours"), row.get("opening_hours"),
                              row.get("tz_name"))
        lead["call_window"] = window
        lead["zone"] = ZONE_LABELS.get(row.get("tz_offset_hours"), "—")
        if window["good_now"]:
            ready.append(lead)
        elif window.get("state") == "early":
            soon.append(lead)
        else:
            # late, shut_today, permanently_closed. `late` is the big one and it
            # is NOT a closed venue: it means the quiet half hour has passed,
            # not that the doors have. Those calls are still answerable, just
            # noisier, and at 3am in Iloilo a noisier call beats no call.
            rest.append(lead)

    def reach(lead):
        kind_rank = {"personal": 0, "owner": 1, "unknown": 2, "role": 3}
        return (0 if lead.get("manager_name") else 1,
                kind_rank.get(lead.get("email_kind"), 2),
                lead.get("attempts") or 0,
                -(lead.get("lead_score") or 0),
                lead["name"])

    ready.sort(key=reach)
    # The nearly-ready ones are ordered by the clock instead: the point of
    # showing them is "this is what you're waiting for and how long".
    soon.sort(key=lambda l: (l["call_window"].get("starts_in") or 9999, *reach(l)))
    # Still-open-but-past-the-lull first, then shut today, then permanently
    # closed, and within each the same reach order as the ready pile. Ringable
    # ones rise to the top of the section on their own; nothing needs hiding to
    # keep them there.
    rest.sort(key=lambda l: (WINDOW_RANK.get(l["call_window"].get("state"), 2),
                             *reach(l)))

    # Whoever the operator should dial first, whatever bucket they landed in.
    # There is always one while a dialable lead exists, so the focus card can
    # always put a number under the headline — outside US afternoons `ready` is
    # empty and that card used to go blank, which reads as "no work" to someone
    # who deliberately stayed up for this.
    queue = ready + soon + rest
    dialable = len(queue)
    next_best = queue[0] if queue else None

    op_now = datetime.now(_operator_tz())
    if ready:
        headline = f"Call {ready[0]['name']} — {len(ready)} ready now"
    elif soon:
        wait = soon[0]["call_window"].get("starts_in") or 0
        pretty = f"{wait // 60}h {wait % 60}m" if wait >= 60 else f"{wait} min"
        headline = (f"No perfect window for {pretty} — "
                    f"{soon[0]['call_window'].get('starts_at_yours', '')} your time. "
                    f"{dialable} still on the list below.")
    elif rest:
        # Don't say "every venue left is shut" over a pile of venues that are
        # open. `late` means the quiet half hour has gone, and saying otherwise
        # sent the operator to bed with callable leads on the screen.
        open_now = sum(1 for l in rest if l["call_window"].get("state") == "late")
        if open_now:
            headline = (f"Past everyone's quiet hour — {open_now} still open, "
                        f"{rest[0]['name']} first")
        else:
            headline = f"Every venue is shut right now — {len(rest)} waiting for tomorrow"
    elif unworked and not dialable:
        # Leads exist but not one carries a number phones.py will pass. That is
        # a generator problem, not a clock problem, and telling the operator to
        # wait for a window would be a lie.
        headline = f"{unworked} leads on the list, none with a dialable number"
    elif unworked == 0:
        headline = "The list is empty — fill it now and start calling."
    else:
        headline = "Nothing to call right now."

    return {
        "headline": headline,
        "ready": ready[:limit],
        "soon": soon[:12],
        # Everything the window says isn't ideal this minute, still ordered and
        # still dialable. Capped at `limit` like `ready` — but `rest_count` is
        # the real total, so the page can say how many it isn't showing instead
        # of implying the list ends here.
        "rest": rest[:limit],
        "ready_count": len(ready),
        "soon_count": len(soon),
        "rest_count": len(rest),
        # The single best lead across all three buckets, for the focus card.
        "next": next_best,
        # Unworked leads that phones.py will actually let us dial. Distinct from
        # unworked_total: a list of 400 rows with no valid numbers is empty for
        # calling purposes but must not trigger a fill, because filling won't
        # fix it.
        "dialable_total": dialable,
        # Whether there is ANY unworked lead, reachable this minute or not.
        # An empty list and a list of venues that are simply shut right now look
        # identical from ready_count alone, and they need opposite responses:
        # one is "go and fetch more", the other is "wait, they open at four".
        # Only the first should start a fill.
        "unworked_total": unworked,
        "list_empty": unworked == 0,
        "done_today": done_today,
        "operator": {
            "zone": OPERATOR_TZ,
            "local_time": op_now.strftime("%-I:%M%p").lower(),
            "date": op_now.strftime("%a %-d %b"),
        },
    }


# ============== AI DEBRIEF ==============
#
# After a call, type what happened in plain words and the model turns it into
# the fields. The point is that nobody should have to remember which box the
# follow-up date goes in, or re-type a contact's name into a form, while the
# next call is already waiting.

class Debrief(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["call", "email", "fb"] = "call"


DEBRIEF_SYSTEM = """You turn a salesperson's rough notes from a phone call into structured CRM fields.

They sell 86'd, an iPhone app for bar inventory, to independent bars and restaurants.

Return ONLY a JSON object with these keys (omit any you cannot determine — never guess):
  "status": one of "new","contacted","warm","won","dead"
  "outcome": one of "answered","voicemail","gatekeeper","not_interested","callback" —
    what SPECIFICALLY happened on this call, not to be confused with status. A voicemail and
    an actual conversation can both land status "contacted", and outcome is the only field
    that still tells them apart — getting this vague is exactly the bug it exists to prevent.
  "contact": the name of the person they spoke to
  "email": a corrected or newly learned email address
  "phone": a corrected or newly learned phone number
  "followup_in_days": integer number of days until the agreed follow-up
  "summary": one clean sentence recording what happened

Rules:
- "not interested", "hung up", "don't call again", "no thanks" -> status "dead", outcome "not_interested"
- an agreed callback, a demo booked, real interest -> status "warm", outcome "callback"
- reached the decision maker, had a real conversation, no clear next step -> status "contacted", outcome "answered"
- signed up, bought, installed -> status "won", outcome "answered"
- left a voicemail, no answer, nobody picked up -> status "contacted", outcome "voicemail"
- spoke to staff/a gatekeeper, the decision maker wasn't in or available -> status "contacted", outcome "gatekeeper"
- "next week" is 7 days, "tomorrow" is 1, "Monday" is 3 unless told otherwise
"""


# Claude, and only Claude. The scan path's OpenAI/Gemini pair used to serve
# this too, on the reasoning that it needed no new key — but this is one short
# text extraction per logged call, and running it on Haiku is materially
# cheaper than running it on a frontier vision model. Called over the raw REST
# API through httpx rather than the SDK, the same way main.py sends through
# Resend: no new pinned dependency, nothing to keep in version lockstep.
# Overridable so the call can be pointed at a gateway, a proxy, or a local
# stand-in when testing without spending real tokens.
ANTHROPIC_URL = (os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
                 + "/v1/messages")
ANTHROPIC_VERSION = "2023-06-01"
DEBRIEF_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


def _ask_claude(system: str, user: str, max_tokens: int = 400) -> dict:
    """One JSON answer from Claude. Raises HTTPException when unusable.

    Shared by the call-notes reader and the email drafter — one place that
    knows the headers, the prefill trick and what each failure should say.
    """
    import json as _json

    import httpx

    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(status_code=503, detail={
            "error": "ai_unavailable",
            "message": ("No ANTHROPIC_API_KEY is set, so this can't be done "
                        "automatically — write it by hand."),
        })

    try:
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={"x-api-key": key,
                     "anthropic-version": ANTHROPIC_VERSION,
                     "content-type": "application/json"},
            json={
                "model": DEBRIEF_MODEL,
                "max_tokens": max_tokens,
                "temperature": 0,
                "system": system,
                "messages": [
                    {"role": "user", "content": user},
                    # Prefilling the opening brace is what makes the reply JSON
                    # without a tool definition: the model can only continue an
                    # object it has already started.
                    {"role": "assistant", "content": "{"},
                ],
            },
            timeout=40.0,
        )
    except Exception as exc:
        print(f"[crm] Claude request failed: {exc}", flush=True)
        raise HTTPException(status_code=503, detail={
            "error": "ai_unavailable",
            "message": "Couldn't reach the AI — write it by hand.",
        })

    if resp.status_code != 200:
        # The body carries the real reason (bad key, rate limit, credit) and
        # it's an internal tool, so say it rather than making it a guess.
        detail = resp.text[:160]
        print(f"[crm] Claude HTTP {resp.status_code}: {detail}", flush=True)
        raise HTTPException(status_code=503, detail={
            "error": "ai_unavailable",
            "message": f"The AI returned {resp.status_code} — {detail}",
        })

    try:
        body = resp.json()
        chunks = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        text = "{" + "".join(chunks)
        # A prefilled reply usually ends cleanly at the closing brace, but a
        # model can add a sentence after it. Cut at the last brace rather than
        # failing the whole call over a trailing "Hope that helps!".
        if not text.rstrip().endswith("}") and "}" in text:
            text = text[:text.rindex("}") + 1]
        return _json.loads(text)
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[crm] Claude returned unparseable JSON: {exc}", flush=True)
        raise HTTPException(status_code=503, detail={
            "error": "ai_unreadable",
            "message": "The AI's answer didn't parse — write it by hand.",
        })


def _debrief_extract(text: str) -> dict:
    """Ask the model for structured fields from a salesperson's rough notes."""
    return _ask_claude(DEBRIEF_SYSTEM, text, max_tokens=400)


def _apply_call_notes(cursor, lead, extracted: dict, raw_text: str, kind: str,
                      today: str, now: str) -> tuple[dict, dict, str, Optional[dict]]:
    """Write a debrief's extracted fields onto `lead`, log the touch, spend
    the day's counters, and return (updated_row, applied, undo_id, counters).

    Shared by /debrief (an existing lead, fetched moments earlier) and
    /leads/quick-add (a lead this same request just inserted) — a "here's
    what happened on this call" write behaves identically whichever door it
    came through, and a brand-new lead gets the exact same undo/counter/
    cadence handling a touch on an old one gets, not a simplified copy of it.
    """
    status = extracted.get("status")
    if status not in VALID_STATUSES:
        status = None
    followup = extracted.get("followup_in_days")
    try:
        followup = int(followup) if followup is not None else None
        if followup is not None and not (0 <= followup <= 365):
            followup = None
    except (TypeError, ValueError):
        followup = None

    undo_id = _snapshot(cursor, lead, f"debrief {kind}")
    attempt = (lead["attempts"] or 0) + 1 if kind == "call" else (lead["attempts"] or 0)
    sets = ["updated_at = %s", "last_touch_at = %s"]
    params: list = [now, now]
    applied: dict = {}

    if kind == "call":
        sets.append("call_date = %s"); params.append(today); applied["call_date"] = today
        sets.append("attempts = %s"); params.append(attempt)
        applied["attempt"] = attempt
    elif kind == "email" and not lead["email_date"]:
        sets.append("email_date = %s"); params.append(today); applied["email_date"] = today

    # The model reports what happened; the ladder decides when to try again
    # if nobody was reached and it didn't name a date itself.
    #
    # outcome comes from the model DIRECTLY now — it used to be re-derived
    # from status alone (dead -> not_interested, anything else landing on
    # warm/won/contacted -> "answered"), which threw away exactly the
    # distinction the prompt itself already draws: "voicemail or gatekeeper
    # with nobody reached -> status 'contacted'" was written so the PIPELINE
    # STAGE stays coarse on purpose, not so last_outcome should collapse a
    # voicemail into "Answered". A debrief reading "left a voicemail, no
    # answer" landed status=contacted (correctly) and last_outcome=answered
    # (wrong) — indistinguishable on screen from an actual conversation.
    outcome_guess = extracted.get("outcome")
    if outcome_guess not in TOUCH_OUTCOMES:
        # Older extractions, or a model that skipped the field: fall back to
        # the coarse guess rather than losing the outcome entirely.
        if status == "dead":
            outcome_guess = "not_interested"
        elif status in ("warm", "won", "contacted"):
            outcome_guess = "answered"
        else:
            outcome_guess = None
    cadence_days, forced_status = _cadence(attempt, outcome_guess) if kind == "call" else (None, None)
    if forced_status and not status:
        status = forced_status
        applied["status"] = status
    if outcome_guess:
        applied["outcome"] = outcome_guess

    if status:
        sets.append("status = %s"); params.append(status); applied["status"] = status
    sets.append("last_outcome = %s"); params.append(outcome_guess or "logged")
    # Length caps on model output: these columns are written straight from
    # whatever the model returned, and a model is perfectly capable of
    # handing back a paragraph where a name was asked for.
    FIELD_LIMITS = {"contact": 200, "email": 320, "phone": 50}
    for field, limit in FIELD_LIMITS.items():
        value = extracted.get(field)
        if isinstance(value, str) and value.strip():
            clean = value.strip()[:limit]
            sets.append(f"{field} = %s"); params.append(clean)
            applied[field] = clean
    follow_days = followup if followup is not None else cadence_days
    if follow_days is not None:
        when = (datetime.now(_reset_tz()) + timedelta(days=follow_days)).strftime("%Y-%m-%d")
        sets.append("followup_date = %s"); params.append(when)
        applied["followup_date"] = when
        if followup is None:
            applied["followup_set_by"] = f"cadence (attempt {attempt})"

    summary = extracted.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = raw_text.strip()
    summary = summary.strip()[:2000]
    stamp = f"[{today}] {kind}"
    if kind == "call":
        stamp += f" · attempt {attempt}"
    note = f"{stamp}: {summary}"
    sets.append("notes = COALESCE(notes || E'\\n', '') || %s"); params.append(note)
    applied["note"] = note

    _attach_touch(cursor, undo_id, _record_touch(
        cursor, lead, kind, outcome_guess, attempt, lead.get("tz_offset_hours")))
    params.append(lead["id"])
    cursor.execute(f"UPDATE crm_leads SET {', '.join(sets)} WHERE id = %s RETURNING *", params)
    updated = cursor.fetchone()

    counter_col = {"call": "daily_calls_remaining", "email": "daily_emails_remaining",
                   "fb": "daily_fb_remaining"}[kind]
    cursor.execute(f"""
        UPDATE crm_counters
           SET {counter_col} = GREATEST(0, {counter_col} - 1),
               touch_ticker_remaining = GREATEST(0, touch_ticker_remaining - 1),
               touch_ticker_last_action = %s, updated_at = %s
         WHERE id = 1 RETURNING *
    """, (kind, now))
    counters = cursor.fetchone()
    return updated, applied, undo_id, counters


@crm_router.post("/leads/{lead_id}/debrief", response_model=dict)
def debrief_lead(lead_id: str, data: Debrief, _: bool = Depends(require_crm_key)):
    """Free-text notes in, updated lead out.

    Everything it decides is echoed back in `applied` so a wrong reading is
    visible immediately rather than silently rewriting the pipeline.
    """
    extracted = _debrief_extract(data.text)
    today = _today()
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE", (lead_id,))
        lead = cursor.fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Lead not found"})

        updated, applied, undo_id, counters = _apply_call_notes(
            cursor, lead, extracted, data.text, data.kind, today, now)
        conn.commit()

    return {"lead": _lead_row(updated), "applied": applied, "undo_id": undo_id,
            "counters": _counters_row(counters) if counters else None}


# ============== AI QUICK ADD ==============
#
# A call that already happened to a bar that was never in the pipeline at
# all — cold-found on the operator's own initiative, a referral, someone who
# called in — has nowhere to go: /debrief updates a lead that already
# exists. Describing the call in plain words creates the lead AND logs that
# first call in the same step, through the exact same fields and cadence
# /debrief writes (via _apply_call_notes), so it isn't a second, thinner
# path into the pipeline.

class QuickAdd(BaseModel):
    text: str = Field(min_length=1, max_length=6000)
    # Optional override. The operator shouldn't have to type it — the model
    # reads it out of the notes — but a caller that already knows it wins.
    name: Optional[str] = Field(default=None, max_length=200)


QUICK_ADD_SYSTEM = """You turn a salesperson's rough notes about a call they just made — to a bar or restaurant that ISN'T already in the CRM — into a new lead record. The notes are often pasted straight off a website or Google listing, with words run together ("80002Primary Phone:") — read through that.

They sell 86'd, an iPhone app for bar inventory, to independent bars and restaurants.

Return ONLY a JSON object with these keys (omit any you truly cannot find):
  "name": the bar or restaurant's name. It is almost always the first thing in the notes and often repeated (e.g. after "Website:"). ALWAYS return it when any venue name appears anywhere in the text.
  "loc": "City, ST"
  "address": street address if given
  "phone": the main phone number
  "other_phones": any additional phone numbers, as one string
  "website": a URL if one is given (only a real URL, never a guess)
  "email": an email address, if one is given
  "email_on_website": true if the notes say the email is on their website / to find it on the site
  "contact": the person they spoke to, with role if given (e.g. "Taylor (bartender)")
  "decision_makers": who makes the buying decision, with role if given (e.g. "Mallory and Mike (owners)")
  "status": one of "new","contacted","warm","won","dead"
  "outcome": one of "answered","voicemail","gatekeeper","not_interested","callback"
  "followup_in_days": integer number of days until the agreed follow-up
  "next_step": the concrete next action, one short sentence (e.g. "Email the owners")
  "summary": one or two clean sentences recording what happened, keeping every useful detail

Rules:
- "not interested", "hung up", "don't call again" -> status "dead", outcome "not_interested"
- an agreed callback, a demo booked, real interest -> status "warm", outcome "callback"
- reached the decision maker, real conversation, no clear next step -> status "contacted", outcome "answered"
- left a voicemail, nobody picked up -> status "contacted", outcome "voicemail"
- spoke to staff/bartender/gatekeeper, the decision maker wasn't there -> status "contacted", outcome "gatekeeper"
- "next week" is 7 days, "tomorrow" is 1, "Monday" is 3 unless told otherwise
"""


def _quick_add_extract(text: str) -> dict:
    return _ask_claude(QUICK_ADD_SYSTEM, text, max_tokens=700)


_NAME_CUT = re.compile(r"\s+(?:at|@|-|–|—|:)\s+|\s*\(\d|\s*\d{3}[-.\s]\d{3}|[.,;\n]")


def _name_from_text(text: str) -> Optional[str]:
    """Last-resort name: whatever leads the notes, up to the first phone
    number, "at", or punctuation. Pasted listings start with the venue."""
    head = _NAME_CUT.split(text.strip(), maxsplit=1)[0].strip()
    head = re.sub(r"^(called|call(ing)?|spoke (to|with))\s+", "", head, flags=re.I).strip()
    return head[:200] if 2 <= len(head) <= 80 else None


def _clean(v, limit: int = 300) -> Optional[str]:
    return v.strip()[:limit] if isinstance(v, str) and v.strip() else None


@crm_router.post("/leads/quick-add", response_model=dict, status_code=201)
def quick_add_lead(data: QuickAdd, _: bool = Depends(require_crm_key)):
    """Describe a call to a bar that isn't in the CRM yet — paste whatever you
    have — and get back a new lead with the call logged and every detail kept.

    The name comes from the model, then a plain-text fallback (the first thing
    in pasted notes is the venue), and only 422s if both come up empty. A
    separate "type the name" box was tried and rejected: the whole point is
    pasting and walking away.

    If the notes carry no email — or say it's on their website — this finds
    the site (given URL, else OpenStreetMap by name + town) and reads the
    address off it, the same way the lead generator does.
    """
    extracted = _quick_add_extract(data.text)

    name = _clean(data.name, 200) or _clean(extracted.get("name"), 200) \
        or _name_from_text(data.text)
    if not name:
        raise HTTPException(status_code=422, detail={
            "error": "no_name",
            "message": "Couldn't tell which bar this was — start with the bar's name.",
        })

    loc = _clean(extracted.get("loc"), 200)
    website = _clean(extracted.get("website"), 300)
    found_via = None
    if not _clean(extracted.get("email"), 320):
        try:
            from leadgen import find_venue_website, find_email_on_site
            if not website:
                website = find_venue_website(name, loc)
            if website:
                email, page = find_email_on_site(website)
                if email:
                    extracted["email"] = email
                    found_via = page
        except Exception as exc:
            print(f"[crm] quick-add email lookup failed: {exc}", flush=True)

    # Everything the model found that has no column of its own goes into the
    # notes, labelled — "contacted" alone tells the operator nothing.
    details = [f"{label}: {val}" for label, val in [
        ("Decision makers", _clean(extracted.get("decision_makers"))),
        ("Spoke to", _clean(extracted.get("contact"))),
        ("Next step", _clean(extracted.get("next_step"))),
        ("Address", _clean(extracted.get("address"))),
        ("Other phones", _clean(extracted.get("other_phones"))),
        ("Website", website),
        ("Email found on", found_via),
    ] if val]

    today = _today()
    now = now_iso()
    lead_id = generate_id()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crm_leads (id, name, loc, status, source, attempts, notes,
                                   created_at, updated_at)
            VALUES (%s, %s, %s, 'new', 'manual', 0, %s, %s, %s)
            RETURNING *
        """, (lead_id, name, loc, "\n".join(details) or None, now, now))
        lead = cursor.fetchone()

        updated, applied, undo_id, counters = _apply_call_notes(
            cursor, lead, extracted, data.text, "call", today, now)
        conn.commit()

    if found_via:
        applied["email_found_on"] = found_via
    elif extracted.get("email_on_website") and not extracted.get("email"):
        applied["email_lookup"] = "couldn't find an address on their site"
    return {"lead": _lead_row(updated), "applied": applied, "undo_id": undo_id,
            "counters": _counters_row(counters) if counters else None}


class BulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)


@crm_router.post("/leads/bulk-delete", response_model=dict)
def bulk_delete(data: BulkDelete, _: bool = Depends(require_crm_key)):
    """Clear several leads at once — a whole zone, or a run of bad numbers."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE crm_lead_candidates SET status='rejected', "
            "reject_reason='lead deleted by hand' WHERE promoted_lead_id = ANY(%s)",
            (data.ids,),
        )
        cursor.execute("DELETE FROM crm_leads WHERE id = ANY(%s)", (data.ids,))
        removed = cursor.rowcount
        conn.commit()
    return {"deleted": removed}


# ============== WHAT'S ACTUALLY WORKING ==============

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@crm_router.get("/dialstats", response_model=dict)
def dial_stats(_: bool = Depends(require_crm_key)):
    """Connect rate by hour, by weekday, and by attempt number.

    The call windows in this system are a heuristic I reasoned out about how
    bars run. This is how that heuristic gets checked against reality: once
    there are a few hundred dials logged, the numbers here say which hours and
    days really connect for THIS list, and the window can be moved to match.
    Until then it reports thin data honestly rather than dressing up noise.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT local_hour AS hour, COUNT(*) AS dials,
                   COUNT(*) FILTER (WHERE connected) AS connects
              FROM crm_touches WHERE kind = 'call' AND local_hour IS NOT NULL
             GROUP BY local_hour ORDER BY local_hour
        """)
        by_hour = [{"hour": r["hour"], "dials": r["dials"], "connects": r["connects"],
                    "connect_pct": round(100.0 * r["connects"] / r["dials"], 1) if r["dials"] else 0.0}
                   for r in cursor.fetchall()]

        cursor.execute("""
            SELECT weekday, COUNT(*) AS dials,
                   COUNT(*) FILTER (WHERE connected) AS connects
              FROM crm_touches WHERE kind = 'call' AND weekday IS NOT NULL
             GROUP BY weekday ORDER BY weekday
        """)
        by_day = [{"day": WEEKDAY_NAMES[r["weekday"]], "dials": r["dials"],
                   "connects": r["connects"],
                   "connect_pct": round(100.0 * r["connects"] / r["dials"], 1) if r["dials"] else 0.0}
                  for r in cursor.fetchall()]

        # The persistence question: does the Nth try still pay?
        cursor.execute("""
            SELECT attempt, COUNT(*) AS dials,
                   COUNT(*) FILTER (WHERE connected) AS connects
              FROM crm_touches WHERE kind = 'call' AND attempt IS NOT NULL
             GROUP BY attempt ORDER BY attempt
        """)
        by_attempt = [{"attempt": r["attempt"], "dials": r["dials"], "connects": r["connects"],
                       "connect_pct": round(100.0 * r["connects"] / r["dials"], 1) if r["dials"] else 0.0}
                      for r in cursor.fetchall()]

        cursor.execute("SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE connected) AS c "
                       "FROM crm_touches WHERE kind = 'call'")
        totals = cursor.fetchone()

    dials = totals["n"] or 0
    overall = round(100.0 * totals["c"] / dials, 1) if dials else 0.0

    # A recommendation is only offered once there's enough to stand on.
    best_hours = [h for h in by_hour if h["dials"] >= 20]
    def hour12(hour: int) -> str:
        """14 -> '2pm'. Nobody reads a connect-rate table in 24-hour time."""
        suffix = "am" if hour < 12 else "pm"
        return f"{hour % 12 or 12}{suffix}"

    for row in by_hour:
        row["hour_label"] = hour12(row["hour"])

    best_hours.sort(key=lambda h: -h["connect_pct"])
    if dials < 100:
        advice = (f"Only {dials} dials logged — too few to draw from. "
                  "Come back after a couple of hundred.")
    elif best_hours:
        top = best_hours[0]
        advice = (f"Best hour so far is {hour12(top['hour'])} local at {top['connect_pct']}% "
                  f"across {top['dials']} dials, against {overall}% overall.")
    else:
        advice = f"{dials} dials at {overall}% overall; no single hour has 20 dials yet."

    return {"total_dials": dials, "connect_pct": overall, "by_hour": by_hour,
            "by_day": by_day, "by_attempt": by_attempt, "advice": advice}
