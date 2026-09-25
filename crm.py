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
from datetime import date, datetime, timedelta, timezone
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
            ("phone_status", "TEXT"),               # does their own site vouch for it
            ("phone_note", "TEXT"),                 # what the check found, in words
            # The owner's rules (pours liquor, not a tourist strip, not a
            # chain): 'ok' or 'blocked', and the evidence or the reason.
            ("fit_status", "TEXT"),
            ("fit_note", "TEXT"),
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

        # Every Message-ID the CRM sent, so a reply is tied to its lead even
        # when it comes back from a different address than it went to.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_sent_emails (
                touch_id TEXT PRIMARY KEY,      -- the 'email' row in crm_touches
                lead_id TEXT NOT NULL,
                to_addr TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_sent_messages (
                message_id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                sent_at TEXT NOT NULL
            )
        """)
        # Every inbox message looked at, once: what it matched and what the AI
        # did with it. Read-only on the mailbox, so this is the only record of
        # "already handled".
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_inbox (
                message_id TEXT PRIMARY KEY,
                from_addr TEXT,
                from_name TEXT,
                subject TEXT,
                received_at TEXT,
                lead_ids TEXT,
                status TEXT NOT NULL,           -- ignored | updated | no_change | failed
                result TEXT,                    -- JSON: reply, applied (with undo ids), skipped
                processed_at TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inbox_processed "
                       "ON crm_inbox(processed_at DESC)")
        # What the reply said (so a reply to it can be written from their own
        # words), whether it was an opt-out or needs answering, the reply the
        # AI drafted overnight, when it was answered, and when the operator
        # took its note off "While you were away" (dismissed_at — the row
        # itself is never deleted; see inbox_dismiss).
        for col, col_type in [("body_text", "TEXT"), ("opt_out", "BOOLEAN"),
                              ("needs_reply", "BOOLEAN"), ("draft", "TEXT"),
                              ("replied_at", "TEXT"), ("dismissed_at", "TEXT")]:
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'crm_inbox' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE crm_inbox ADD COLUMN {col} {col_type}")

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

        # The company brain (playbook.py): one row, pinned like crm_counters.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_ai_brain (
                id INTEGER PRIMARY KEY,
                owner_notes TEXT,
                owner_notes_updated_at TEXT,
                playbook TEXT,
                playbook_refreshed_at TEXT,
                playbook_touches INTEGER,
                playbook_error TEXT,
                pinned TEXT,
                rejected TEXT,
                playbook_prev TEXT,
                playbook_diff TEXT,
                scoreboard TEXT,
                CONSTRAINT crm_ai_brain_single_row CHECK (id = 1)
            )
        """)
        cursor.execute("INSERT INTO crm_ai_brain (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        # The owner's corrections (pinned / rejected lessons), the playbook
        # before the last refresh and what changed, and the scoreboard the
        # model was shown. See playbook.py.
        for col in ("pinned", "rejected", "playbook_prev", "playbook_diff", "scoreboard"):
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'crm_ai_brain' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE crm_ai_brain ADD COLUMN {col} TEXT")

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

    try:
        _reconcile_no_answer()
    except Exception as e:  # a repair pass must never stop the CRM booting
        print(f"[crm] NO_ANSWER_RECONCILE_FAILED {e!r}", flush=True)
    try:
        init_apple_tables()
    except Exception as e:  # Apple Analytics is optional; never block the CRM
        print(f"[crm] APPLE_TABLES_FAILED {e!r}", flush=True)


def _reconcile_no_answer() -> int:
    """Re-file calls logged "answered" whose own note says nobody picked up.

    Before "no_answer" existed, a rang-out call had nowhere to land and came
    out as "answered" — which also stopped the retry ladder, since _cadence
    treats "answered" as reached. Reads only the LATEST note line (the call
    that set last_outcome), so an older no-answer followed by a real
    conversation stays "answered". Idempotent: a fixed row no longer matches.
    """
    fixed = 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, notes, followup_date FROM crm_leads "
                       "WHERE last_outcome = 'answered'")
        for row in cursor.fetchall():
            lines = [l for l in (row["notes"] or "").splitlines() if l.strip()]
            outcome = _no_answer_outcome(lines[-1]) if lines else None
            if not outcome:
                continue
            cursor.execute("""
                UPDATE crm_leads SET last_outcome = %s,
                       followup_date = COALESCE(followup_date, %s), updated_at = %s
                 WHERE id = %s
            """, (outcome, _today(), now_iso(), row["id"]))
            cursor.execute("""
                UPDATE crm_touches SET outcome = %s, connected = FALSE
                 WHERE id = (SELECT id FROM crm_touches
                              WHERE lead_id = %s AND outcome = 'answered'
                              ORDER BY at DESC LIMIT 1)
            """, (outcome, row["id"]))
            fixed += 1
        conn.commit()
    if fixed:
        print(f"[crm] NO_ANSWER_RECONCILED rows={fixed}", flush=True)
    return fixed


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
    "phone_status", "phone_note", "fit_status", "fit_note",
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
TOUCH_OUTCOMES = {"answered", "voicemail", "no_answer", "gatekeeper", "not_interested",
                  "callback", "wrong_number"}

# leadgen.PHONE_OK: numbers the venue's own website vouches for. A generated
# lead is only offered for dialling with one of these; the operator's own
# entries are trusted as typed. See leadgen.judge_phone().
TRUSTED_PHONE = ("confirmed", "from_site")
# What the check found wrong. The auto-dialer export leaves these out; a lead
# not checked yet (no status) isn't one of them.
BAD_PHONE = ("conflict", "unconfirmed", "wrong")


def _dial_ok(row) -> bool:
    """Whether a lead's number may be offered for dialling at all."""
    return row.get("source") != "leadgen" or row.get("phone_status") in TRUSTED_PHONE


def _fit_ok(row) -> bool:
    """Whether a generated lead passed the owner's rules: pours liquor (shown
    on its own site), not on a main tourist strip, not a chain. Until the
    background check has looked (leadgen.verify_fit), it isn't offered. The
    operator's own entries are trusted as typed."""
    return row.get("source") != "leadgen" or row.get("fit_status") == "ok"


# The call list's order, the owner's priority 4 (2026-09-25): a lead with an
# email first. Then a name to ask for, the kind of mailbox, fewest tries, fit.
# (1-3 — chains, liquor, tourist strips — are filters, never an order.)
_KIND_RANK = {"personal": 0, "owner": 1, "unknown": 2, "role": 3}


def _reach(lead: dict) -> tuple:
    return (0 if lead.get("email") else 1,
            0 if lead.get("manager_name") else 1,
            _KIND_RANK.get(lead.get("email_kind"), 2),
            lead.get("attempts") or 0,
            -(lead.get("lead_score") or 0),
            lead["name"])

# "Nobody picked up" in the operator's own words. There was no outcome for
# this at all, so a call that rang out with no way to leave a message had
# nowhere to land — the model either left the field blank (and the fallback
# guessed "answered" from status=contacted) or called it "answered" outright.
# Pig & the Sprout was logged "Answered" from notes reading "no one picked up
# the phone, and you can't leave a message". Checked against the raw notes so
# it holds even when the model gets it wrong.
_LEFT_MESSAGE_RE = re.compile(
    r"\bleft (?:a |them a |him a |her a )?(?:voice ?mail|vm|message)\b", re.I)
_NO_ANSWER_RE = re.compile(
    r"\b(?:no ?one|nobody|no body)\b[^.;]{0,40}?\b(?:pick(?:ed|s)? ?up|answer(?:ed|s)?)\b"
    r"|\bno answer\b|\bdid(?:n'?t| not) (?:pick up|answer)\b|\bunanswered\b"
    r"|\brang out\b|\bjust rang\b|\bstraight to (?:voice ?mail|vm)\b"
    r"|\b(?:can'?t|cannot|couldn'?t|could not|unable to) leave (?:a )?(?:voice ?mail|message|vm)\b"
    r"|\bmailbox (?:is |was )?full\b|\bno (?:voice ?mail|vm)\b",
    re.I)


def _no_answer_outcome(raw_text: str) -> Optional[str]:
    """What the notes say when nobody was reached, or None if they don't."""
    text = raw_text or ""
    if _LEFT_MESSAGE_RE.search(text):
        return "voicemail"
    if _NO_ANSWER_RE.search(text):
        return "no_answer"
    return None

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


TRY_KINDS = ("call", "email", "fb")


def _tries(kind_counts) -> dict:
    """Every attempt to sell a lead — calls, emails and Facebook messages —
    from (kind, count) pairs off `crm_touches`.

    NOT the lead's `attempts` column. That one counts CALLS only, because it
    paces the call-retry ladder (`_cadence`, `MAX_ATTEMPTS`): an email must not
    use up a bar's six tries at being rung. The screen's "tries" is this —
    a bar called once and emailed twice has been tried three times.
    """
    out = {"total": 0, "call": 0, "email": 0, "fb": 0}
    for kind, n in kind_counts:
        out["total"] += n
        if kind in TRY_KINDS:
            out[kind] += n
    return out


def touch_story(touches, replies=()) -> dict:
    """What the CRM tab's WHERE THINGS STAND and REACHED OUT are drawn from,
    for one lead. Pure.

    `touches`: its crm_touches rows ({kind, outcome, at}) with undone ones
    already left out. `replies`: its crm_inbox rows ({processed_at, opt_out,
    needs_reply, replied_at}).

    Read from the touch log, not the lead's `last_outcome`: sending an email
    overwrites that with "emailed", so a call and then an email used to lose
    the call's result and read as "Called yesterday". Here the latest touch
    of ANY kind and the latest CALL are kept apart, and a reply that came in
    after them counts too.
    """
    def at(row):
        return str(row.get("at") or "")

    touches = [t for t in touches if t.get("outcome") != "undone"]
    last = max(touches, key=at, default=None)
    last_call = max((t for t in touches if t.get("kind") == "call"), key=at, default=None)
    reply = max(replies, key=lambda r: str(r.get("processed_at") or ""), default=None)
    counts: dict = {}
    for t in touches:
        counts[t.get("kind")] = counts.get(t.get("kind"), 0) + 1
    return {
        "tries": _tries(counts.items()),
        "last_touch": ({"kind": last.get("kind"), "outcome": last.get("outcome"), "at": at(last)}
                       if last else None),
        "last_call": ({"outcome": last_call.get("outcome"), "at": at(last_call)}
                      if last_call else None),
        "last_reply": ({"at": str(reply.get("processed_at") or ""),
                        "opt_out": bool(reply.get("opt_out")),
                        "needs_reply": bool(reply.get("needs_reply")),
                        "answered": bool(reply.get("replied_at"))} if reply else None),
    }


def _touch_stories(cursor, lead_ids) -> dict:
    """`touch_story()` for a page of leads: two queries, whatever the page size."""
    ids = [i for i in lead_ids if i]
    if not ids:
        return {}
    cursor.execute("""
        SELECT lead_id, kind, outcome, at FROM crm_touches
         WHERE lead_id = ANY(%s) AND outcome IS DISTINCT FROM 'undone'
    """, (ids,))
    touches: dict = {}
    for r in cursor.fetchall():
        touches.setdefault(r["lead_id"], []).append(r)
    # crm_inbox.lead_ids is a comma-joined list: one reply can be about
    # several venues (one management company's).
    cursor.execute("""
        SELECT lead_ids, processed_at, opt_out, needs_reply, replied_at FROM crm_inbox
         WHERE status IN ('updated', 'no_change')
           AND string_to_array(COALESCE(lead_ids, ''), ',') && %s::text[]
    """, (ids,))
    wanted, replies = set(ids), {}
    for r in cursor.fetchall():
        for i in (r["lead_ids"] or "").split(","):
            if i in wanted:
                replies.setdefault(i, []).append(r)
    return {i: touch_story(touches.get(i, []), replies.get(i, [])) for i in ids}


def _touch_counts(cursor, lead_ids) -> dict:
    """`_tries()` for many leads in one query. Undone touches don't count —
    the undo button exists to make a misclick not have happened."""
    ids = [i for i in lead_ids if i]
    if not ids:
        return {}
    cursor.execute("""
        SELECT lead_id, kind, COUNT(*) AS n FROM crm_touches
         WHERE lead_id = ANY(%s) AND outcome IS DISTINCT FROM 'undone'
         GROUP BY lead_id, kind
    """, (ids,))
    pairs: dict = {}
    for r in cursor.fetchall():
        pairs.setdefault(r["lead_id"], []).append((r["kind"], int(r["n"] or 0)))
    return {lead_id: _tries(kc) for lead_id, kc in pairs.items()}


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
    # The AI bar can rename a lead or fix its town. Snapshots taken before
    # these were added simply lack them, and undo only restores what a
    # snapshot holds.
    "name", "loc",
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


# Never contacted: the same test the call list uses to decide who's still
# unworked. Everything else has had at least one logged call or email.
_UNTOUCHED = "(status = 'new' AND last_touch_at IS NULL)"
LEAD_VIEWS = {
    "untouched": _UNTOUCHED,
    "worked": f"NOT {_UNTOUCHED}",
    "open": f"(status <> 'dead' AND NOT {_UNTOUCHED})",
}


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
               first: Optional[str] = None,
               limit: int = 200, offset: int = 0,
               _: bool = Depends(require_crm_key)):
    """The whole pipeline: every lead, at every stage, searchable.

    This is the one view that shows a lead AFTER it has been worked. The call
    list deliberately hides anything touched — that's what stops the same bar
    being rung twice — and follow-ups only show what's due. Without this, a
    lead you spoke to on Tuesday and didn't set a follow-up for is invisible,
    which is how warm leads quietly die.
    """
    # Views, not stages. The CRM tab shows only leads that have been WORKED
    # (a call or email logged): "open" is worked and not dead, and "worked"
    # is every worked lead, for its search. Never-contacted leads live in the
    # burger's Yet to Contact tab ("untouched") — the CRM tab listing 195
    # names nobody had called buried the handful actually in play.
    if status is not None and status not in LEAD_VIEWS and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_status",
            "message": (f"status must be one of {', '.join(LEAD_VIEWS)} or "
                        f"{', '.join(VALID_STATUSES)}"),
        })
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where, params = ["1=1"], []
    if status in LEAD_VIEWS:
        where.append(LEAD_VIEWS[status])
    elif status:
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
        # `first` = a stage to float to the top (the CRM tab's clickable STAGE
        # header). Done here, not in the page, so it holds across pages.
        stage_first, order_params = "", []
        if first in VALID_STATUSES:
            stage_first, order_params = "(status = %s) DESC, ", [first]
        cursor.execute(
            f"""SELECT * FROM crm_leads WHERE {sql_where}
                 ORDER BY {stage_first}COALESCE(last_touch_at, updated_at) DESC, created_at DESC
                 LIMIT %s OFFSET %s""",
            params + order_params + [limit, offset])
        leads = [_lead_row(row) for row in cursor.fetchall()]
        # Every call, email and reply behind WHERE THINGS STAND and REACHED OUT.
        stories = _touch_stories(cursor, [lead["id"] for lead in leads])

        # Always the totals for the whole pipeline, not for the current filter:
        # the counts are the tab labels, and a tab that renumbers itself when
        # you click it is unreadable.
        cursor.execute("SELECT status, COUNT(*) AS n FROM crm_leads GROUP BY status")
        by_status = {r["status"]: r["n"] for r in cursor.fetchall()}
        cursor.execute("SELECT COUNT(*) AS n FROM crm_leads")
        everything = cursor.fetchone()["n"]
        view_counts = {}
        for view, clause in LEAD_VIEWS.items():
            cursor.execute(f"SELECT COUNT(*) AS n FROM crm_leads WHERE {clause}")
            view_counts[view] = cursor.fetchone()["n"]

    for lead in leads:
        lead["window"] = _call_window(lead.get("tz_offset_hours"),
                                      lead.get("opening_hours"), lead.get("tz_name"))
        lead.update(stories.get(lead["id"]) or touch_story([]))
    # The page decides "overdue" against this, the same day Follow-ups uses —
    # not the browser's UTC date, which is a day behind in Manila's morning.
    return {"leads": leads, "count": len(leads), "matching": matching,
            "offset": offset, "limit": limit, "today": _today(),
            "counts": {**{k: by_status.get(k, 0) for k in VALID_STATUSES},
                       **view_counts, "all": everything}}


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
        # Every attempt to sell them, for the details drawer — undone ones
        # excluded, same as the tally.
        cursor.execute("""
            SELECT id, kind, outcome, at FROM crm_touches
             WHERE lead_id = %s AND outcome IS DISTINCT FROM 'undone'
             ORDER BY at ASC
        """, (lead_id,))
        touches = [{"id": t["id"], "kind": t["kind"], "outcome": t["outcome"], "at": t["at"]}
                   for t in cursor.fetchall()]
    lead = _lead_row(row)
    lead["window"] = _call_window(row.get("tz_offset_hours"),
                                  row.get("opening_hours"), row.get("tz_name"))
    lead["touches"] = touches
    lead["tries"] = _tries((t["kind"], 1) for t in touches)
    return {"lead": lead}


def _parse_utc(value) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


_NOTE_EMAIL_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\] email to (\S{1,200}): (.{0,300})$", re.M)


def _email_from_notes(notes: Optional[str], at: str) -> Optional[dict]:
    """The note line an email left behind — for mail sent before bodies were
    kept, it's all there is: the date, the address and the subject. The note
    is dated in the CRM's day and the touch in UTC, so the nearest day wins."""
    when = _parse_utc(at)
    if not when:
        return None
    day, best = when.date(), None
    for when, to, subject in _NOTE_EMAIL_RE.findall(notes or ""):
        try:
            gap = abs((date.fromisoformat(when) - day).days)
        except ValueError:
            continue
        if gap <= 1 and (best is None or gap < best[0]):
            best = (gap, to, subject.strip())
    return {"to": best[1], "subject": best[2]} if best else None


@crm_router.get("/leads/{lead_id}/touches/{touch_id}/email", response_model=dict)
def sent_email(lead_id: str, touch_id: str, _: bool = Depends(require_crm_key)):
    """The email behind one 'email' attempt, for the details panel.

    Kept on every send since this was added. Earlier ones are recovered where
    something still holds them: a scheduled send kept its body in
    crm_scheduled_emails; a send-now left only its subject in the notes, and
    the answer says so rather than pretending there's nothing.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_touches WHERE id = %s AND lead_id = %s",
                       (touch_id, lead_id))
        touch = cursor.fetchone()
        if not touch or touch["kind"] != "email":
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "No email attempt with that id."})
        cursor.execute("SELECT * FROM crm_sent_emails WHERE touch_id = %s", (touch_id,))
        kept = cursor.fetchone()
        if kept:
            return {"to": kept["to_addr"], "subject": kept["subject"], "body": kept["body"],
                    "sent_at": kept["sent_at"], "complete": True}
        cursor.execute("""
            SELECT to_addr, subject, body, sent_at FROM crm_scheduled_emails
             WHERE lead_id = %s AND status = 'sent' AND sent_at IS NOT NULL
        """, (lead_id,))
        at = _parse_utc(touch["at"])
        for job in cursor.fetchall():
            sent = _parse_utc(job["sent_at"])
            if at and sent and abs((sent - at).total_seconds()) <= 600:
                return {"to": job["to_addr"], "subject": job["subject"], "body": job["body"],
                        "sent_at": job["sent_at"], "complete": True}
        cursor.execute("SELECT notes FROM crm_leads WHERE id = %s", (lead_id,))
        row = cursor.fetchone()
    found = _email_from_notes(row["notes"] if row else None, touch["at"]) or {}
    return {"to": found.get("to"), "subject": found.get("subject"), "body": None,
            "sent_at": touch["at"], "complete": False}


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

        # The company brain (playbook.py): one row, pinned like crm_counters.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_ai_brain (
                id INTEGER PRIMARY KEY,
                owner_notes TEXT,
                owner_notes_updated_at TEXT,
                playbook TEXT,
                playbook_refreshed_at TEXT,
                playbook_touches INTEGER,
                playbook_error TEXT,
                pinned TEXT,
                rejected TEXT,
                playbook_prev TEXT,
                playbook_diff TEXT,
                scoreboard TEXT,
                CONSTRAINT crm_ai_brain_single_row CHECK (id = 1)
            )
        """)
        cursor.execute("INSERT INTO crm_ai_brain (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
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


# ============== APP FUNNEL (pre-signup) ==============

@crm_router.get("/app-funnel", response_model=dict)
def app_funnel(days: int = 30, _: bool = Depends(require_crm_key)):
    """The steps before an account exists, from the app_events table.

    /funnel starts at `users` and measures forward. This starts at the app
    icon being tapped and measures up to that same point, so the two together
    cover download → paying customer with no blind segment in the middle.

    Counts are DISTINCT INSTALLS, not raw events: someone who opens the
    sign-up form four times is one person deciding, not four. Rates are
    step-over-previous-step, because that is where a fix goes.
    """
    days = max(1, min(days, 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT event, COUNT(DISTINCT anon_id) AS installs, COUNT(*) AS events
              FROM app_events
             WHERE created_at >= %s
             GROUP BY event
        """, (since,))
        rows = {r["event"]: r for r in cursor.fetchall()}

    def installs(event: str) -> int:
        row = rows.get(event)
        return int(row["installs"]) if row else 0

    opened = installs("app_opened")
    reg_viewed = installs("register_viewed")
    reg_submitted = installs("register_submitted")
    reg_succeeded = installs("register_succeeded")

    def pct(n: int, d: int):
        return round(n * 100.0 / d, 1) if d else None

    return {
        "days": days,
        "steps": {
            "opened_app": opened,
            "saw_login": installs("login_viewed"),
            "reached_signup": reg_viewed,
            "submitted_signup": reg_submitted,
            "created_account": reg_succeeded,
        },
        "step_rates_pct": {
            "opened_to_signup_form": pct(reg_viewed, opened),
            "form_to_submitted": pct(reg_submitted, reg_viewed),
            "submitted_to_created": pct(reg_succeeded, reg_submitted),
            "opened_to_created": pct(reg_succeeded, opened),
        },
        # The two numbers this endpoint exists to produce. The first is the
        # cost of the sign-up wall; the second is the cost of the form itself
        # (a submit that never became an account is a validation failure, a
        # duplicate email or a dead connection).
        "abandoned_at_form": max(0, reg_viewed - reg_submitted),
        "failed_after_submit": max(0, reg_submitted - reg_succeeded),
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
        # The address is freed the same way the app's own "delete my account"
        # does it. Without this the row kept its email under users' UNIQUE
        # index, so the person could never sign up again with that address:
        # registration's own check skips deleted rows, and the insert failed.
        cursor.execute(
            "UPDATE users SET deleted_at=%s, updated_at=%s, "
            "email = CONCAT(email, '.deleted.', %s) "
            "WHERE id=%s AND deleted_at IS NULL",
            (now_iso(), now_iso(), generate_id()[:8], user_id),
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
            rows = cursor.fetchall()
            tries = _touch_counts(cursor, [r["id"] for r in rows])
            out = []
            for row in rows:
                lead = _lead_row(row)
                lead["call_window"] = _call_window(row.get("tz_offset_hours"), row.get("opening_hours"))
                lead["tries"] = tries.get(row["id"]) or _tries([])
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
    outcome: Optional[Literal["answered", "voicemail", "no_answer", "gatekeeper",
                              "not_interested", "callback"]] = None
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
                          "no_answer": "contacted",
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

# What the drafter may say about the product, and how it should sound, lives
# in pitch.py: the master sheet (every fact checked against this repo), the
# owner's own example email, and the style guide. COMPANY_APP_URL stays here
# for anything else that wants the link.
COMPANY_APP_URL = (os.getenv("COMPANY_APP_URL")
                   or "https://apps.apple.com/us/app/86d-bar-inventory/id6798359825")
# How much of the log a draft reads. The newest part is what matters; older
# calls less, and the whole history can run long.
DRAFT_LOG_CHARS = 4000


WINNERS_SHOWN = 2


def _winning_emails(limit: int = WINNERS_SHOWN) -> list:
    """Our most recent emails that got a reply (and not a "stop emailing me"):
    what the drafter learns tone and angle from, beside the owner's example.
    Bodies are only kept since crm_sent_emails existed, so this starts empty
    and fills as mail goes out and replies come back."""
    import pitch
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT e.subject, e.body, MAX(e.sent_at) AS sent_at
                  FROM crm_sent_emails e
                  JOIN crm_inbox i ON i.lead_ids LIKE '%%' || e.lead_id || '%%'
                                  AND i.processed_at > e.sent_at
                                  AND i.status IN ('updated', 'no_change')
                                  AND COALESCE(i.opt_out, FALSE) = FALSE
                 GROUP BY e.subject, e.body
                 ORDER BY MAX(e.sent_at) DESC LIMIT %s
            """, (limit,))
            rows = cursor.fetchall()
    except Exception as exc:
        print(f"[crm] winning emails unavailable: {exc}", flush=True)
        return []
    return [{"subject": r["subject"], "body": (r["body"] or "")[:pitch.WINNER_CHARS]}
            for r in rows if r["body"]]


def _draft_context(row: dict, include_log: bool = True) -> str:
    """WHAT WE KNOW about this bar: its facts with their sources, the
    prep-sheet points and (for a first email or a reply; a follow-up's ask
    carries its own) what's been logged."""
    import pitch
    import venue

    lead = _lead_row(row)
    lines = venue.facts_to_lines(venue.loads(row.get("venue_facts")))
    brief = _brief_of(row)
    points = [str(p) for p in (brief.get("points") or []) if p][:3]
    log = ""
    if include_log:
        log = (lead.get("notes") or "").strip()
        if len(log) > DRAFT_LOG_CHARS:
            log = "…" + log[-DRAFT_LOG_CHARS:]
    return pitch.lead_context(lead, lines, points, log)


def _draft_system() -> str:
    import pitch
    return pitch.system_prompt(_knowledge(), _winning_emails())


def _write_draft(row: dict, ask: str, include_log: bool = True,
                 to_decision_maker: bool = False) -> dict:
    """One draft: the cached system (sheet, brain, examples, style), then this
    bar and what to write. Shared by the Email button and the inbox reader's
    overnight replies, so both write from the same brain.

    Every draft ends with the owner's signature (pitch.sign — in code, not
    left to the prompt). A fresh outreach draft (`to_decision_maker`) is also
    made to greet the decision maker by name; a reply answers whoever wrote,
    and a revision keeps the greeting the draft already has."""
    import pitch
    out = _claude_json(_draft_system(), pitch.user_prompt(_draft_context(row, include_log), ask),
                       pitch.SCHEMA, max_tokens=AI_MIN_TOKENS, timeout=120.0, purpose="draft")
    subject = str(out.get("subject") or "").strip()[:200]
    body = str(out.get("body") or "").strip()[:20000]
    if not subject or not body:
        raise HTTPException(status_code=502, detail={
            "error": "draft_incomplete",
            "message": "The draft came back empty — try saying it a different way."})
    if to_decision_maker:
        body = pitch.address_to(body, pitch.first_name(pitch.decision_maker(dict(row))[0]))
    return {"subject": subject, "body": pitch.sign(body)}


def _reply_ask(mail: dict, brief: str = "") -> str:
    """Asking for a reply to an email a bar sent us."""
    subject = (mail.get("subject") or "").strip()
    re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
    lines = [
        "Write a REPLY to the email below, which they sent us. Answer every question in it "
        "from the MASTER SHEET and the owner's instructions. Anything neither covers: say "
        "you'll find out, or offer a quick call — never guess. They wrote to you, so keep it "
        "short: a few lines usually does it, no four-step list unless they asked how it works. "
        f'Subject: "{re_subject}".',
        "",
        "THEIR EMAIL",
        f"From: {mail.get('from_name') or ''} <{mail.get('from_addr') or ''}>",
        f"Subject: {subject}",
        "",
        (mail.get("body_text") or mail.get("text") or "").strip()[:4000] or "(no text kept)",
    ]
    if brief:
        lines += ["", f"Also: {brief}"]
    return "\n".join(lines)


# ============== THE COMPANY BRAIN ==============
#
# What the owner tells the AI (standing instructions, typed on the AI Brain
# page) and what the AI has learned from the log (the playbook, refreshed
# about once a day). See playbook.py. Read by the drafter, the prep sheet and
# the School through _knowledge(); the master sheet stays the only source of
# product facts.

PLAYBOOK_MIN_TOUCHES = 5         # below this there is nothing to learn from yet
PLAYBOOK_EVERY_HOURS = 20
PLAYBOOK_NEW_TOUCHES = 3         # a refresh needs this much new activity
PLAYBOOK_DAYS = 90
_playbook_lock = threading.Lock()


def _brain_row() -> dict:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM crm_ai_brain WHERE id = 1")
            row = cursor.fetchone()
        return dict(row) if row else {}
    except Exception as exc:
        # A missing table must never take the drafter or the prep sheet down.
        print(f"[crm] brain read failed: {exc}", flush=True)
        return {}


def _playbook_of(row: dict) -> Optional[dict]:
    try:
        pb = json.loads(row.get("playbook") or "null")
    except (TypeError, ValueError):
        return None
    return pb if isinstance(pb, dict) else None


def _knowledge(playbook: bool = True) -> str:
    """The owner's instructions and (optionally) the playbook, as prompt text.
    Empty when neither exists yet."""
    import playbook as _pb
    row = _brain_row()
    parts = [_pb.render_owner(row.get("owner_notes"))]
    if playbook:
        parts.append(_pb.render(_playbook_of(row), row.get("playbook_refreshed_at")))
    return "\n\n".join(p for p in parts if p)


def _json_list(value) -> list:
    try:
        v = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _scoreboard(cursor, cutoff: str) -> dict:
    """The log's counts over the window, for playbook.scoreboard_lines():
    dials and what they reached, emails and replies, where the worked bars
    stand, and how many became app signups. Undone touches never count."""
    cursor.execute("""
        SELECT COUNT(*) FILTER (WHERE kind = 'call') AS dials,
               COUNT(*) FILTER (WHERE kind = 'call' AND connected) AS connects,
               COUNT(*) FILTER (WHERE kind = 'call'
                                AND outcome IN ('answered', 'callback', 'not_interested')) AS conversations,
               COUNT(*) FILTER (WHERE kind = 'call' AND outcome = 'gatekeeper') AS gatekeepers,
               COUNT(*) FILTER (WHERE kind = 'call' AND outcome = 'callback') AS callbacks,
               COUNT(*) FILTER (WHERE kind = 'call' AND outcome = 'not_interested') AS not_interested
          FROM crm_touches
         WHERE outcome IS DISTINCT FROM 'undone' AND at >= %s
    """, (cutoff,))
    s = dict(cursor.fetchone() or {})
    cursor.execute("""
        SELECT COUNT(*) AS emails,
               COUNT(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM crm_inbox i
                    WHERE i.lead_ids LIKE '%%' || t.lead_id || '%%'
                      AND i.processed_at > t.at
                      AND i.status IN ('updated', 'no_change')
                      AND COALESCE(i.opt_out, FALSE) = FALSE)) AS email_replies
          FROM crm_touches t
         WHERE t.kind = 'email' AND t.outcome IS DISTINCT FROM 'undone' AND t.at >= %s
    """, (cutoff,))
    s.update(dict(cursor.fetchone() or {}))
    cursor.execute("""
        SELECT attempt, COUNT(*) AS dials, COUNT(*) FILTER (WHERE connected) AS connects
          FROM crm_touches
         WHERE kind = 'call' AND outcome IS DISTINCT FROM 'undone'
           AND attempt IS NOT NULL AND at >= %s
         GROUP BY attempt ORDER BY attempt
    """, (cutoff,))
    s["by_attempt"] = [dict(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT local_hour AS hour, COUNT(*) AS dials, COUNT(*) FILTER (WHERE connected) AS connects
          FROM crm_touches
         WHERE kind = 'call' AND outcome IS DISTINCT FROM 'undone'
           AND local_hour IS NOT NULL AND at >= %s
         GROUP BY local_hour
    """, (cutoff,))
    s["by_hour"] = [dict(r) for r in cursor.fetchall()]
    cursor.execute("""
        SELECT COUNT(*) AS worked,
               COUNT(*) FILTER (WHERE status = 'warm') AS warm,
               COUNT(*) FILTER (WHERE status = 'won') AS won,
               COUNT(*) FILTER (WHERE status = 'dead') AS dead
          FROM crm_leads WHERE last_touch_at >= %s
    """, (cutoff,))
    s.update(dict(cursor.fetchone() or {}))
    # A signup counts whenever it happened: the few there are matter most.
    cursor.execute("""
        SELECT COUNT(*) AS signups,
               COUNT(*) FILTER (WHERE u.subscription_status = 'active') AS paying
          FROM crm_leads l JOIN users u ON u.id = l.matched_user_id AND u.deleted_at IS NULL
         WHERE l.last_touch_at IS NOT NULL
    """)
    s.update(dict(cursor.fetchone() or {}))
    s["days"] = PLAYBOOK_DAYS
    return s


def _playbook_inputs(cursor, today: str, row: Optional[dict] = None) -> dict:
    """What a refresh reads: the digest, the bar names it may cite (every
    worked bar in the window, not only the ones that fit in the digest — so a
    lesson backed by a bar from three weeks ago still validates), the touch
    count, and the scoreboard with the percentages a point may quote."""
    import playbook as _pb

    row = row or {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PLAYBOOK_DAYS)).isoformat()
    cursor.execute("SELECT COUNT(*) AS n FROM crm_touches WHERE outcome IS DISTINCT FROM 'undone'")
    touches = cursor.fetchone()["n"]
    cursor.execute("""
        SELECT l.id, l.name, l.loc, l.status, l.last_outcome, l.notes,
               u.subscription_status AS customer
          FROM crm_leads l
          LEFT JOIN users u ON u.id = l.matched_user_id AND u.deleted_at IS NULL
         WHERE l.last_touch_at IS NOT NULL AND (l.last_touch_at >= %s OR u.id IS NOT NULL)
         ORDER BY l.last_touch_at DESC LIMIT 1500
    """, (cutoff,))
    leads = [dict(l) for l in cursor.fetchall()]
    names = {l["id"]: l["name"] for l in leads}
    cursor.execute("""
        SELECT from_name, from_addr, subject, lead_ids, result, processed_at FROM crm_inbox
         WHERE status IN ('updated', 'no_change') AND processed_at >= %s
         ORDER BY processed_at DESC LIMIT 60
    """, (cutoff,))
    inbox = cursor.fetchall()
    replies, replied_at = [], {}
    for r in inbox:
        try:
            said = (json.loads(r["result"] or "{}") or {}).get("reply")
        except ValueError:
            said = None
        ids = [i for i in (r["lead_ids"] or "").split(",") if i]
        for i in ids:
            replied_at[i] = max(replied_at.get(i, ""), r["processed_at"] or "")
        replies.append({"from": r["from_name"] or r["from_addr"], "subject": r["subject"],
                        "about": ", ".join(names.get(i, "") for i in ids if names.get(i)) or "a bar",
                        "said": said or r["subject"]})
    cursor.execute("""
        SELECT t.lead_id, t.at, e.subject FROM crm_touches t
          LEFT JOIN crm_sent_emails e ON e.touch_id = t.id
         WHERE t.kind = 'email' AND t.outcome IS DISTINCT FROM 'undone' AND t.at >= %s
         ORDER BY t.at DESC LIMIT 80
    """, (cutoff,))
    emails = [{"lead": names.get(e["lead_id"], "a bar"),
               "subject": e["subject"] or "(subject not kept)",
               "replied": replied_at.get(e["lead_id"], "") > (e["at"] or "")}
              for e in cursor.fetchall()]
    board = _pb.scoreboard_lines(_scoreboard(cursor, cutoff))
    digest = _pb.digest(leads, replies, emails, today, scoreboard=board,
                        current=_playbook_of(row), pinned=_json_list(row.get("pinned")),
                        rejected=_json_list(row.get("rejected")))
    return {"digest": digest, "names": list(names.values()), "touches": touches,
            "scoreboard": board, "percents": _pb.percents_in(board)}


def refresh_playbook(force: bool = False) -> dict:
    """Re-learn the playbook from the log if there's something new to learn.

    Skips (no model call) when fewer than PLAYBOOK_MIN_TOUCHES calls/emails
    are logged at all, or — unless forced — when the last refresh is under
    PLAYBOOK_EVERY_HOURS old or fewer than PLAYBOOK_NEW_TOUCHES touches have
    been logged since. One refresh at a time. The owner's pins and rejections
    are applied from the row as it stands at SAVE time, under a lock, so a
    click made while the model was thinking isn't overwritten.
    """
    import playbook as _pb

    if not _playbook_lock.acquire(blocking=False):
        return {"skipped": "a refresh is already running"}
    try:
        try:
            # The newest signups count as results in this refresh, not the next.
            rematch_attribution()
        except Exception as exc:
            print(f"[crm] attribution before the playbook failed: {exc}", flush=True)
        today = _today()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM crm_ai_brain WHERE id = 1")
            row = dict(cursor.fetchone() or {})
            inputs = _playbook_inputs(cursor, today, row)
        touches = inputs["touches"]
        if touches < PLAYBOOK_MIN_TOUCHES:
            return {"skipped": f"only {touches} calls and emails logged so far — "
                               f"it starts learning at {PLAYBOOK_MIN_TOUCHES}"}
        last = _parse_utc(row.get("playbook_refreshed_at"))
        fresh = last and datetime.now(timezone.utc) - last < timedelta(hours=PLAYBOOK_EVERY_HOURS)
        quiet = touches - (row.get("playbook_touches") or 0) < PLAYBOOK_NEW_TOUCHES
        if not force and row.get("playbook") and (fresh or quiet):
            return {"skipped": "nothing new to learn since the last refresh"}
        try:
            # A background job reading a long log: give it room.
            out = _claude_json(_pb.SYSTEM, inputs["digest"], _pb.SCHEMA, timeout=300.0,
                               purpose="playbook")
            pb = _pb.clean(out, inputs["names"], allowed_percents=inputs["percents"],
                           rejected=_json_list(row.get("rejected")))
            error = None
        except HTTPException as exc:
            pb, error = None, str((exc.detail or {}).get("message") if isinstance(exc.detail, dict)
                                  else exc.detail)[:300]
        change = None
        with get_db() as conn:
            cursor = conn.cursor()
            if pb is not None:
                cursor.execute("SELECT playbook, pinned, rejected FROM crm_ai_brain "
                               "WHERE id = 1 FOR UPDATE")
                live = dict(cursor.fetchone() or {})
                pb = _pb.finalize(pb, _json_list(live.get("pinned")),
                                  _json_list(live.get("rejected")))
                change = _pb.diff(_playbook_of(live), pb)
                cursor.execute("""
                    UPDATE crm_ai_brain SET playbook = %s, playbook_prev = %s, playbook_diff = %s,
                           scoreboard = %s, playbook_refreshed_at = %s, playbook_touches = %s,
                           playbook_error = NULL WHERE id = 1
                """, (json.dumps(pb), live.get("playbook"), json.dumps(change),
                      json.dumps(inputs["scoreboard"]), now_iso(), touches))
            else:
                cursor.execute("UPDATE crm_ai_brain SET playbook_error = %s WHERE id = 1",
                               (error,))
            conn.commit()
        points = len(_pb.all_points(pb))
        print(f"[crm] PLAYBOOK_REFRESHED points={points} touches={touches}"
              + (f" new={len(change['new'])} dropped={len(change['dropped'])}" if change else "")
              + (f" error={error}" if error else ""), flush=True)
        return {"refreshed": pb is not None, "points": points, "error": error,
                "new": len(change["new"]) if change else 0}
    finally:
        _playbook_lock.release()


def _refresh_playbook_safe(force: bool = False) -> None:
    try:
        refresh_playbook(force=force)
    except Exception as exc:
        print(f"[crm] PLAYBOOK_FAILED {exc}", flush=True)


class OwnerNotes(BaseModel):
    text: str = Field(default="", max_length=8000)


def _live_scoreboard(stored) -> list:
    """The scoreboard as of now, for the page; the stored one (what the model
    last saw) if the numbers can't be read."""
    import playbook as _pb
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PLAYBOOK_DAYS)).isoformat()
        with get_db() as conn:
            return _pb.scoreboard_lines(_scoreboard(conn.cursor(), cutoff))
    except Exception as exc:
        print(f"[crm] scoreboard unavailable: {exc}", flush=True)
        try:
            v = json.loads(stored or "[]")
            return v if isinstance(v, list) else []
        except (TypeError, ValueError):
            return []


@crm_router.get("/brain", response_model=dict)
def brain(_: bool = Depends(require_crm_key)):
    """Everything the AI works from: the master sheet (fixed, from pitch.py),
    the owner's standing instructions and the learned playbook — plus the
    scoreboard it learns from, what changed at the last re-learn, and the
    lessons the owner kept or marked wrong."""
    import pitch
    row = _brain_row()
    try:
        change = json.loads(row.get("playbook_diff") or "null")
    except (TypeError, ValueError):
        change = None
    return {"master_sheet": pitch.master_sheet(),
            "owner_notes": row.get("owner_notes") or "",
            "owner_notes_updated_at": row.get("owner_notes_updated_at"),
            "playbook": _playbook_of(row),
            "playbook_refreshed_at": row.get("playbook_refreshed_at"),
            "playbook_error": row.get("playbook_error"),
            "diff": change if isinstance(change, dict) else None,
            "pinned": _json_list(row.get("pinned")),
            "rejected": _json_list(row.get("rejected")),
            "scoreboard": _live_scoreboard(row.get("scoreboard")),
            "refreshing": _playbook_lock.locked(),
            "min_touches": PLAYBOOK_MIN_TOUCHES}


@crm_router.put("/brain/notes", response_model=dict)
def save_owner_notes(data: OwnerNotes, _: bool = Depends(require_crm_key)):
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crm_ai_brain (id, owner_notes, owner_notes_updated_at) VALUES (1, %s, %s)
            ON CONFLICT (id) DO UPDATE SET owner_notes = EXCLUDED.owner_notes,
                   owner_notes_updated_at = EXCLUDED.owner_notes_updated_at
        """, (data.text.strip(), now))
        conn.commit()
    return {"saved": True, "owner_notes_updated_at": now}


class BrainPoint(BaseModel):
    id: str = Field(min_length=6, max_length=40)
    keep: bool = True


def _brain_edit(fn) -> dict:
    """Read the brain row under a lock, let `fn` change (playbook, pinned,
    rejected), write all three back. The owner's clicks and a refresh never
    interleave: both take this row lock."""
    import playbook as _pb
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT playbook, pinned, rejected FROM crm_ai_brain WHERE id = 1 FOR UPDATE")
        row = dict(cursor.fetchone() or {})
        pb = _playbook_of(row) or {"summary": "", "sections": []}
        pinned, rejected = _json_list(row.get("pinned")), _json_list(row.get("rejected"))
        result = fn(pb, pinned, rejected)
        pb = _pb.finalize(pb, pinned, rejected)
        cursor.execute("UPDATE crm_ai_brain SET playbook = %s, pinned = %s, rejected = %s "
                       "WHERE id = 1", (json.dumps(pb), json.dumps(pinned), json.dumps(rejected)))
        conn.commit()
    return result


def _find_point(pb: dict, pinned: list, pid: str) -> dict:
    import playbook as _pb
    point = next((p for p in _pb.all_points(pb) if p.get("id") == pid), None) \
        or next((p for p in pinned if p.get("id") == pid), None)
    if point is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "That lesson isn't in the playbook any more — reload."})
    return point


@crm_router.post("/brain/keep", response_model=dict)
def brain_keep(data: BrainPoint, _: bool = Depends(require_crm_key)):
    """Pin a lesson (keep=true) so every re-learn keeps it, in these words;
    or unpin it (keep=false), and it lives or goes with the evidence again."""
    import playbook as _pb

    def edit(pb, pinned, rejected):
        point = _find_point(pb, pinned, data.id)
        pinned[:] = [p for p in pinned if p.get("id") != data.id]
        if data.keep:
            pinned.append({"id": point["id"], "text": point["text"],
                           "evidence": list(point.get("evidence") or []),
                           "section": point.get("section"), "at": now_iso()})
            del pinned[:-_pb.MAX_PINNED]
        else:
            # Unpinned, it goes back to being an ordinary point for now.
            for sec in pb.get("sections") or []:
                if (sec.get("title") or "") == (point.get("section") or ""):
                    if not any(p.get("id") == data.id for p in sec["points"]):
                        sec["points"].insert(0, {"id": point["id"], "text": point["text"],
                                                 "evidence": list(point.get("evidence") or [])})
        return {"kept": data.keep, "pinned": len(pinned)}

    return _brain_edit(edit)


@crm_router.post("/brain/wrong", response_model=dict)
def brain_wrong(data: BrainPoint, _: bool = Depends(require_crm_key)):
    """The owner says a lesson is wrong: it leaves the playbook now (so no
    draft or prep sheet uses it from this moment), and every later re-learn
    is told never to write it or anything like it."""
    import playbook as _pb

    def edit(pb, pinned, rejected):
        point = _find_point(pb, pinned, data.id)
        pinned[:] = [p for p in pinned if p.get("id") != data.id]
        rejected[:] = [r for r in rejected if r.get("id") != data.id]
        rejected.append({"id": point["id"], "text": point["text"], "at": now_iso()})
        del rejected[:-_pb.MAX_REJECTED]
        return {"rejected": True}

    return _brain_edit(edit)


@crm_router.post("/brain/unreject", response_model=dict)
def brain_unreject(data: BrainPoint, _: bool = Depends(require_crm_key)):
    """Take back a "wrong": the lesson may be learned again if the log backs it."""
    def edit(pb, pinned, rejected):
        before = len(rejected)
        rejected[:] = [r for r in rejected if r.get("id") != data.id]
        return {"restored": len(rejected) < before}

    return _brain_edit(edit)


@crm_router.post("/brain/refresh", response_model=dict)
def brain_refresh(_: bool = Depends(require_crm_key)):
    """Re-learn the playbook now, in the background (a minute or so). Poll GET /brain."""
    if _playbook_lock.locked():
        return {"started": False, "message": "Already learning — give it a minute."}
    threading.Thread(target=_refresh_playbook_safe, kwargs={"force": True}, daemon=True,
                     name="crm-playbook").start()
    return {"started": True}


BRIEF_SYSTEM = """You prepare a salesperson for the next phone call to one bar. They sell 86'd; the
MASTER SHEET below is everything true about it, and the only product facts you may use.

You are given THE BAR: facts about it (each says where it came from), what has happened
with it so far, and sometimes what the owner has told you and what we've learned from
other calls.

Return a JSON object:
- "opener": the first sentence to say once someone picks up, in plain spoken English. Use
  one real detail from THE BAR if there is one; otherwise open with the job itself (the
  count, the orders). No "is this a bad time", no fake compliment. Null if unsure.
- "ask_for": who to ask for, only if THE BAR names a person (a contact, a manager, someone
  in the notes), with why ("Brent, the owner, per Lesley"). Otherwise null.
- "points": two or three short notes to glance at mid-dial. Say what a fact MEANS for the
  pitch, not the fact again ("open till 2am seven nights: a lot of pours to count by hand").
  Where it came from a map rather than their own site, make it a question, not a claim.
- "watch_for": the objection they're most likely to raise and a one-line answer, drawn from
  the history or what we've learned; null if nothing points to one.

Rules: invent nothing about the bar. Never state a product fact that isn't on the MASTER
SHEET. Short: every line fits on a phone screen. No greetings, no exclamation marks."""

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "opener": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "ask_for": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "points": {"type": "array", "items": {"type": "string"}},
        "watch_for": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["opener", "ask_for", "points", "watch_for"],
    "additionalProperties": False,
}
BRIEF_VERSION = 2


def _brief_of(row: dict) -> dict:
    """The stored prep-sheet brief. The first version stored a bare list of
    points; that reads as a brief with points and no fingerprint (so stale)."""
    try:
        raw = json.loads(row.get("call_brief") or "null")
    except (TypeError, ValueError):
        raw = None
    if isinstance(raw, list):
        return {"points": [str(p) for p in raw]}
    return raw if isinstance(raw, dict) else {}


def _brief_input(row: dict, lines: list, profile: dict, knowledge: str) -> str:
    about = [f"Venue: {row['name']}" + (f", {row['loc']}" if row.get("loc") else "")]
    if profile.get("kind"):
        about.append(f"Kind: {profile['kind']}")
    if profile.get("hours"):
        about.append(f"Hours: {profile['hours']}")
    if row.get("opener"):
        about.append(f"From their own website: {row['opener']}")
    if row.get("manager_name"):
        about.append(f"Named on their website: {row['manager_name']}"
                     + (f" ({row['manager_role']})" if row.get("manager_role") else ""))
    if row.get("contact"):
        about.append(f"We've been asking for: {row['contact']}")
    if row.get("last_outcome"):
        about.append(f"Last outcome: {row['last_outcome']}")
    import pitch
    angle = pitch.state_angle(row.get("loc"))
    if angle:
        about.append(f"Angle for this state: {angle}")
    facts = "\n".join(f"- {l['text']} (from {l['source']})" for l in lines)
    history = _lead_history(row, lines=6)
    parts = ["THE BAR\n" + "\n".join(about)]
    if facts:
        parts.append("FACTS\n" + facts)
    if history:
        parts.append("WHAT HAS HAPPENED (oldest first)\n" + history)
    if knowledge:
        parts.append(knowledge)
    return "\n\n".join(parts)


def _brief_fingerprint(text: str) -> str:
    import hashlib
    return hashlib.sha1(f"{BRIEF_VERSION}\n{text}".encode()).hexdigest()[:16]


def _brief_ask_for(value, row: dict) -> Optional[str]:
    """Keep a suggested name only if the lead's own record carries it."""
    if not isinstance(value, str) or not value.strip():
        return None
    first = re.split(r"[\s,(—-]+", value.strip())[0].lower()
    known = " ".join(str(row.get(k) or "") for k in ("contact", "manager_name", "notes")).lower()
    return value.strip()[:200] if len(first) >= 2 and first in known else None


@crm_router.get("/leads/{lead_id}/brief", response_model=dict)
def lead_brief(lead_id: str, refresh: bool = False, quick: bool = False,
               _: bool = Depends(require_crm_key)):
    """What's worth knowing about this venue before the phone rings.

    The facts are extracted, never generated, and each carries its source — a
    brief that asserts something wrong is the moment the person on the other
    end decides you're reading a script. On top: an opener, who to ask for,
    two or three talking points and the objection to watch for, written from
    those facts, the lead's own history and what we've learned (the brain).

    Cached against a fingerprint of everything it was written from, so it's
    rewritten when a call is logged or the playbook changes, not before.
    `quick=1` never calls the model: the page shows the facts at once and
    asks for the rest in a second request — nobody waits on a model to see a
    bar's hours with a phone in their hand.
    """
    import pitch
    import venue

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT l.*, c.website AS cand_website, c.amenity AS cand_amenity
              FROM crm_leads l
              LEFT JOIN crm_lead_candidates c ON c.promoted_lead_id = l.id
             WHERE l.id = %s
        """, (lead_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})
    profile = _venue_profile(row)
    lines = venue.facts_to_lines(venue.loads(row.get("venue_facts")))
    knowledge = _knowledge()
    ask = _brief_input(row, lines, profile, knowledge)
    fp = _brief_fingerprint(ask)
    stored = _brief_of(row)

    def answer(brief: dict, cached: bool, pending: bool = False) -> dict:
        return {"facts": lines, "profile": profile, "cached": cached, "pending": pending,
                "points": brief.get("points") or [], "opener": brief.get("opener"),
                "ask_for": brief.get("ask_for"), "watch_for": brief.get("watch_for")}

    if stored.get("fp") == fp and not refresh:
        return answer(stored, True)
    if not os.getenv("ANTHROPIC_API_KEY"):
        # Facts alone are still worth showing — they're the part that had to
        # be true anyway.
        return answer({}, False)
    if quick:
        return answer({}, False, pending=True)

    try:
        out = _claude_json(BRIEF_SYSTEM + "\n\n=== MASTER SHEET ===\n" + pitch.master_sheet(),
                           ask, BRIEF_SCHEMA, purpose="prep-sheet")
    except HTTPException:
        # A model that's down must not take the facts down with it.
        return answer({}, False)
    brief = {"v": BRIEF_VERSION, "fp": fp,
             "points": [str(p).strip()[:220] for p in (out.get("points") or []) if str(p).strip()][:3],
             "opener": (str(out.get("opener")).strip()[:300] if out.get("opener") else None),
             "ask_for": _brief_ask_for(out.get("ask_for"), row),
             "watch_for": (str(out.get("watch_for")).strip()[:300] if out.get("watch_for") else None)}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE crm_leads SET call_brief = %s WHERE id = %s",
                       (json.dumps(brief)[:6000], lead_id))
        conn.commit()
    return answer(brief, False)


def _venue_profile(row) -> dict:
    """The plain facts on file for the prep sheet, all already stored: what
    kind of place, their website, the hours the map lists. Nothing here is
    fetched or generated, and nothing here decides where a lead sorts — a
    bare profile is just a bare profile."""
    website = row.get("cand_website")
    if not website:
        found = re.search(r"https?://[^\s|,;]+", row.get("notes") or "")
        website = found.group(0) if found else None
    return {"kind": row.get("cand_amenity"), "website": website,
            "hours": row.get("opening_hours")}


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


FOLLOWUP_OUTCOME = {
    "answered": "spoke with someone",
    "voicemail": "left a voicemail",
    "no_answer": "called, nobody picked up",
    "gatekeeper": "the manager wasn't in",
    "callback": "they asked for a callback",
    "not_interested": "they said they weren't interested",
}
# The newest part of the log is what a follow-up is about; older calls matter
# less and the whole history can run long.
FOLLOWUP_NOTES_CHARS = 4000


def _followup_ask(lead: dict, brief: str = "") -> str:
    """The drafting request for a follow-up, built from what's been LOGGED.

    The notes are the operator's own record — call summaries, their verbatim
    words, and machine lines from the lead generator (where the email was
    found, the website). The model is told to write only from what was
    actually said or done with a person, never from the bookkeeping.
    """
    notes = (lead.get("notes") or "").strip()
    if len(notes) > FOLLOWUP_NOTES_CHARS:
        notes = "…" + notes[-FOLLOWUP_NOTES_CHARS:]
    lines = ["Write a FOLLOW-UP email to this venue, based on what has been logged about it."]
    if lead.get("contact"):
        lines.append(f"Contact: {lead['contact']}")
    outcome = FOLLOWUP_OUTCOME.get(lead.get("last_outcome") or "")
    if outcome:
        when = lead.get("call_date") or lead.get("email_date") or ""
        lines.append(f"Last contact: {outcome}" + (f" ({when})" if when else ""))
    lines.append("Salesperson's log, oldest first:\n" + (notes or "(nothing logged)"))
    lines.append(
        "Whose log this is: the SENDER's. Every call and email in it was made by "
        "the sender, who owns 86'd. A person named in it (\"spoke to Lesley\") is "
        "someone at the bar the sender talked to — they did not tell anyone about "
        "86'd, and the email must never say or imply they did. If a staff member "
        "pointed the sender to the owner or manager, the email is to that person, "
        "and says so plainly: \"I spoke with Lesley at the bar, and she said you're "
        "the one to talk to about inventory.\"\n\n"
        "How to use the log: refer back to what was actually discussed — who "
        "they spoke to, what that person said or asked for, any personal detail "
        "worth a friendly nod — and answer what they asked where the product "
        "facts allow. Only state things the log says; if it is unclear whether "
        "something was said, leave it out. Ignore bookkeeping lines (where the "
        "email or website was found, attempt numbers, lead-generator notes) — "
        "never mention them. If nobody was reached, keep it to a short note "
        "saying you tried calling and why you're reaching out.")
    if brief:
        lines.append(f"Also: {brief}")
    return "\n\n".join(lines)


class DraftRequest(BaseModel):
    brief: str = Field(default="", max_length=2000)
    # The Follow-ups tab's Email button: write a follow-up from what's been
    # logged on this lead, with no brief needed.
    followup: bool = False
    # Present on a revision: the draft on screen right now, which the model
    # edits rather than replacing from scratch.
    subject: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, max_length=20000)
    # A reply to an email they sent (crm_inbox.message_id): drafted from
    # their own words, answering what they asked.
    reply_to: Optional[str] = Field(default=None, max_length=500)


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
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})
    lead = _lead_row(row)

    revising = bool(data.subject or data.body)
    followup = data.followup and not revising
    brief = data.brief.strip()
    if not brief and not data.followup and not data.reply_to:
        raise HTTPException(status_code=422, detail={
            "error": "brief_required", "message": "Say what the email should cover."})

    if revising:
        ask = (f"Here is the current draft.\n\nSubject: {data.subject or ''}\n\n"
               f"{data.body or ''}\n\n---\n\nChange it as follows, keeping "
               f"everything else as it is: {brief}")
    elif data.reply_to:
        mail = _inbox_mail(data.reply_to)
        if not mail:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "That email isn't in the inbox log any more."})
        ask = _reply_ask(mail, brief)
    elif followup:
        ask = _followup_ask(lead, brief)
    else:
        ask = f"Write the email. What it needs to say: {brief}"
    return _write_draft(row, ask, include_log=not followup,
                        to_decision_maker=not revising and not data.reply_to)


class OutgoingEmail(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    to: Optional[str] = Field(default=None, max_length=320)
    # UTC ISO 8601. Present means "hold it until then" rather than send now.
    send_at: Optional[str] = Field(default=None, max_length=40)
    # Answering an email they sent (its Message-ID): threads the reply under
    # theirs in both inboxes, and marks it answered on the Follow-ups card.
    in_reply_to: Optional[str] = Field(default=None, max_length=500)


@crm_router.get("/mail/status", response_model=dict)
def mail_status(_: bool = Depends(require_crm_key)):
    """Whether the server can send, so the page knows which button to show."""
    import mailer
    import pitch
    return {"configured": mailer.is_configured(), "from": mailer.sender(),
            "host": mailer.HOST, "port": mailer.PORT, "signature": pitch.SIGNATURE,
            # What clicking the page's title copies (COMPANY_APP_URL).
            "app_url": pitch.APP_URL,
            # The page hides the "draft it for me" box rather than offering a
            # button that can only fail.
            "ai": bool(os.getenv("ANTHROPIC_API_KEY"))}


@crm_router.get("/mail/sent-check", response_model=dict)
def mail_sent_check(_: bool = Depends(require_crm_key)):
    """Can the server file copies in Sent, and which folder? Logs in over
    IMAP and looks; files nothing."""
    import mailer
    return mailer.check_sent_folder()


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
        to = (data.to or (lead or {}).get("email") or "").strip()
        opted_out = _email_suppressed(cursor, to) if lead else None
    if not lead:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})

    if not mailer.valid_address(to):
        raise HTTPException(status_code=422, detail={
            "error": "no_address",
            "message": f"No usable email address for {lead['name']}."})
    # Checked for a queued send too: an opt-out has to hold for mail already
    # scheduled, not just what's sent from here on (run_due_emails re-checks).
    if opted_out:
        raise HTTPException(status_code=409, detail={
            "error": "opted_out",
            "message": f"Not sent — {to} {opted_out}. They can't be emailed again."})

    if data.send_at:
        return _queue_email(lead, to, data)

    reply_to = (data.in_reply_to or "").strip() or None
    try:
        sent = mailer.send(to, data.subject, data.body, in_reply_to=reply_to)
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
        undo_id = _record_email_sent(cursor, lead_id, to, data.subject, today, now,
                                     body=data.body)
        _remember_sent(cursor, sent.get("message_id"), lead_id, now)
        if reply_to:
            cursor.execute("UPDATE crm_inbox SET replied_at = %s WHERE message_id = %s",
                           (now, reply_to))
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
        # They may have opted out after this was queued: that wins, always.
        with get_db() as conn:
            cursor = conn.cursor()
            opted_out = _email_suppressed(cursor, job["to_addr"])
            if opted_out:
                cursor.execute("""
                    UPDATE crm_scheduled_emails SET status = 'failed', last_error = %s
                     WHERE id = %s
                """, (f"Not sent: {job['to_addr']} {opted_out}.", job["id"]))
                cursor.execute("""
                    UPDATE crm_leads SET queued_email_at = NULL
                     WHERE id = %s AND NOT EXISTS (
                        SELECT 1 FROM crm_scheduled_emails
                         WHERE lead_id = %s AND status = 'pending')
                """, (job["lead_id"], job["lead_id"]))
                conn.commit()
        if opted_out:
            skipped += 1
            print(f"[crm] held back a queued email to {job['to_addr']} — opted out", flush=True)
            continue
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
            sent_msg = mailer.send(job["to_addr"], job["subject"], job["body"])
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
                _remember_sent(cursor, sent_msg.get("message_id"), job["lead_id"], now_iso())
                _record_email_sent(cursor, job["lead_id"], job["to_addr"],
                                   job["subject"], _today(), now_iso(), body=job["body"])
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


def _remember_sent(cursor, message_id: Optional[str], lead_id: str, now: str) -> None:
    if message_id:
        cursor.execute("INSERT INTO crm_sent_messages (message_id, lead_id, sent_at) "
                       "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (message_id, lead_id, now))


def _record_email_sent(cursor, lead_id: str, to: str, subject: str,
                       today: str, now: str, body: Optional[str] = None) -> str:
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

        touch_id = _record_touch(cursor, lead, "email", "emailed", lead["attempts"] or 0,
                                 lead.get("tz_offset_hours"))
        _attach_touch(cursor, undo_id, touch_id)
        # What was actually said, so the attempt can be opened later. Only the
        # subject used to survive, as a line in the notes.
        if body is not None:
            cursor.execute("""
                INSERT INTO crm_sent_emails (touch_id, lead_id, to_addr, subject, body, sent_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (touch_id, lead_id, to, subject.strip(), body, now))
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
        elif (undo["action"] or "").startswith("edit"):
            # An edit (the AI bar's) never logged a touch, so there is none to
            # mark. The newest-touch fallback below would pick the lead's last
            # REAL call and quietly un-count it.
            pass
        else:
            cursor.execute("""
                UPDATE crm_touches SET outcome = 'undone', connected = FALSE
                 WHERE id = (SELECT id FROM crm_touches WHERE lead_id = %s
                              ORDER BY at DESC LIMIT 1)
            """, (undo["lead_id"],))

        if (undo["action"] or "").startswith("wrong-number"):
            # The number goes back on the lead, so it comes off the list of
            # numbers never to dial again.
            bad = phone_digits(snapshot.get("phone")) or re.sub(
                r"\D", "", snapshot.get("phone") or "")[-10:]
            if bad:
                cursor.execute("DELETE FROM crm_suppressions WHERE kind = 'phone' "
                               "AND value = %s", (bad,))

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


# ============== WRONG NUMBER ==============

_URL_IN_TEXT = re.compile(r"https?://[^\s|,;]+", re.I)


@crm_router.post("/leads/{lead_id}/wrong-number", response_model=dict)
def wrong_number(lead_id: str, _: bool = Depends(require_crm_key)):
    """The number on file rang somebody else.

    Logs the dial (it happened, and /dialstats should know), retires the
    number for good — no lead may bring it back — and looks on the venue's own
    website for the right one. Found: the lead gets it, and a follow-up for
    today so it shows under Follow-ups to try again. Not found: no number at
    all rather than a known-bad one; the lead stays, so it can still be
    emailed. Undo puts all of it back, the retired number included.
    """
    from leadgen import _local_codes_by_city, find_site_phones, judge_phone

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT l.*, c.website AS cand_website, c.city AS cand_city
              FROM crm_leads l
              LEFT JOIN crm_lead_candidates c ON c.promoted_lead_id = l.id
             WHERE l.id = %s
        """, (lead_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Lead not found"})
        codes = _local_codes_by_city(cursor, [row.get("cand_city")]).get(
            row.get("cand_city"), set())
    bad = phone_digits(row["phone"]) or re.sub(r"\D", "", row["phone"] or "")[-10:]
    if not bad:
        raise HTTPException(status_code=422, detail={
            "error": "no_phone", "message": "There's no number on this lead to mark wrong."})

    # The network first, outside any transaction: a slow site mustn't hold a
    # row lock. A quick-added lead has no candidate, but its notes often carry
    # the website it was found on.
    website = row.get("cand_website")
    if not website:
        found = _URL_IN_TEXT.search(row.get("notes") or "")
        website = found.group(0) if found else None
    new = None
    if website:
        try:
            numbers, _loaded = find_site_phones(website, None)
            verdict = judge_phone(bad, [p for p in numbers if p != bad], codes)
            if verdict["status"] == "from_site":
                new = verdict["phone"]
        except Exception as exc:
            print(f"[crm] wrong-number site lookup failed: {exc}", flush=True)

    today, now = _today(), now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE", (lead_id,))
        lead = cursor.fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Lead not found"})
        undo_id = _snapshot(cursor, lead, "wrong-number call")
        attempt = (lead["attempts"] or 0) + 1
        _attach_touch(cursor, undo_id, _record_touch(
            cursor, lead, "call", "wrong_number", attempt, lead.get("tz_offset_hours")))
        said = f"[{today}] call · attempt {attempt}: Wrong number — {format_us_phone_dashed(bad)} rang someone else."
        if new:
            said += f" Their website lists {format_us_phone_dashed(new)}; try that."
            phone, status, why, follow = (format_us_phone_dashed(new), "from_site",
                                          f"{format_us_phone_dashed(bad)} was a wrong number; "
                                          f"{format_us_phone_dashed(new)} is from their website",
                                          today)
        else:
            said += " Their website shows no other number — email them instead."
            phone, status, why, follow = (None, "wrong",
                                          f"{format_us_phone_dashed(bad)} was a wrong number", None)
        cursor.execute("""
            UPDATE crm_leads
               SET phone = %s, phone_status = %s, phone_note = %s, followup_date = %s,
                   attempts = %s, call_date = %s, last_touch_at = %s,
                   last_outcome = 'wrong_number', updated_at = %s,
                   notes = COALESCE(notes || E'\\n', '') || %s
             WHERE id = %s
         RETURNING *
        """, (phone, status, why, follow, attempt, today, now, now, said, lead_id))
        updated = cursor.fetchone()
        # Never again, on any lead: the generator checks this before promoting.
        cursor.execute("""
            INSERT INTO crm_suppressions (id, kind, value, reason, created_at)
            VALUES (%s, 'phone', %s, %s, %s) ON CONFLICT DO NOTHING
        """, (generate_id(), bad, f"wrong number for {lead['name']}"[:200], now))
        _load_counters_locked(cursor)
        cursor.execute("""
            UPDATE crm_counters
               SET daily_calls_remaining = GREATEST(0, daily_calls_remaining - 1),
                   touch_ticker_remaining = GREATEST(0, touch_ticker_remaining - 1),
                   touch_ticker_last_action = 'call', updated_at = %s
             WHERE id = 1
        """, (now,))
        conn.commit()

    return {"lead": _lead_row(updated), "undo_id": undo_id,
            "new_phone": format_us_phone_dashed(new) if new else None,
            "bad_phone": format_us_phone_dashed(bad)}


@crm_router.post("/leadgen/verify-phones", response_model=dict)
def leadgen_verify_phones(_: bool = Depends(require_crm_key)):
    """Check one batch of the call list's numbers against each venue's own
    website now, in the background. main.py's _phone_check_loop already works
    through them a batch at a time; this only hurries the next one. Poll GET."""
    import leadgen
    threading.Thread(target=leadgen._verify_phones_safe, daemon=True,
                     name="leadgen-verify-phones").start()
    return {"started": True}


@crm_router.get("/leadgen/verify-phones", response_model=dict)
def leadgen_verify_phones_status(_: bool = Depends(require_crm_key)):
    """How the call list's numbers stand: checked, corrected, not yet looked at."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(phone_status, 'not checked yet') AS s, COUNT(*) AS n
              FROM crm_leads
             WHERE source = 'leadgen' AND status = 'new' AND last_touch_at IS NULL
             GROUP BY 1
        """)
        return {"call_list": {r["s"]: r["n"] for r in cursor.fetchall()}}


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
        if row.get("phone_status") in BAD_PHONE:
            continue      # a number the website check found wrong never reaches a dialer
        if row.get("fit_status") == "blocked" or (
                scope != "all" and not row.get("last_touch_at") and not _fit_ok(row)):
            continue      # the owner's rules: chains, beer-and-wine rooms, tourist strips
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
        # A number that didn't validate, or that the venue's own site doesn't
        # vouch for, is never offered for dialling.
        if not lead["phone_ok"] or not _dial_ok(row) or not _fit_ok(row):
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
        return (WINDOW_RANK.get(lead["call_window"].get("state"), 2), *_reach(lead))

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
        # About one map number in five isn't the bar's any more: only numbers
        # the venue's own website vouches for are dialled. See leadgen.
        if not lead["phone_ok"] or not _dial_ok(row) or not _fit_ok(row):
            continue
        window = _call_window(row.get("tz_offset_hours"), row.get("opening_hours"),
                              row.get("tz_name"))
        lead["call_window"] = window
        lead["zone"] = ZONE_LABELS.get(row.get("tz_offset_hours"), "—")
        # Only a venue in its own calling window is "ready". A row with no
        # timezone has no window at all — it used to count as ready around
        # the clock.
        if window["good_now"] and window.get("state") != "unknown":
            ready.append(lead)
        elif window.get("state") == "early":
            soon.append(lead)
        else:
            # late, shut_today, permanently_closed. `late` is the big one and it
            # is NOT a closed venue: it means the quiet half hour has passed,
            # not that the doors have. Those calls are still answerable, just
            # noisier, and at 3am in Iloilo a noisier call beats no call.
            rest.append(lead)

    reach = _reach

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
        # Capped at `limit`, not a fixed 12: when the ready pile is thin the
        # page turns this into the actual working table (see crm.html's
        # MIN_WORKING_TABLE), and a 12-row cap would starve that table before
        # it ever reached a usable size. It's still a countdown strip's data
        # when ready alone already clears the floor — cheap either way.
        "soon": soon[:limit],
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


# What a logged call is turned into — one definition for /debrief and
# quick-add, so the two can't drift. Two things the old prompts got wrong:
#  - DATES. The model was handed the notes and nothing else — no today, no
#    calendar — with a rule reading '"Monday" is 3 unless told otherwise',
#    which is only true on a Friday. It now gets today and the next two weeks
#    spelled out (assist.dates_table, the AI bar's fix for the same problem)
#    and returns the actual date.
#  - WHO TO ASK FOR. `contact` is shown on every screen as "ask for X", but
#    it was filled with whoever picked up. A bartender who said "the owner is
#    Brent, he's in Tuesdays" became the person to ask for next time. It is
#    now `ask_for` (the decision maker when one is named), with `spoke_to`
#    kept in the note.
CALL_FIELDS = """  "status": one of "new","contacted","warm","won","dead"
  "outcome": one of "answered","voicemail","no_answer","gatekeeper","not_interested","callback" —
    what SPECIFICALLY happened on this call, not to be confused with status. A voicemail and
    an actual conversation can both land status "contacted", and outcome is the only field
    that still tells them apart.
  "spoke_to": the person they actually spoke to, with role if given ("Taylor (bartender)")
  "ask_for": who to ask for on the NEXT call: the owner, GM or whoever decides, if the notes
    name them ("Brent (owner)"); otherwise the person they spoke to, if that's who to deal
    with. Null if no name was given
  "followup_date": YYYY-MM-DD, the day they agreed to be contacted again. Null if no day
    was agreed — the system schedules retries itself when nobody was reached
  "best_time": when they said to call, short, in their words ("Tuesdays after 2pm")
  "objection": what they pushed back with, close to their words ("already use BevSpot",
    "too busy until after the holidays", "the owner does all the ordering")
  "current_setup": how they do inventory and ordering today, if said ("clipboard and a
    spreadsheet", "the owner counts Sunday nights")
  "next_step": the concrete next action, one short sentence ("Email Brent the app link")
  "summary": one or two sentences recording what happened. Keep personal details the
    person shared (pets, family, plans, what they said about the product) — the
    salesperson opens the next call with those"""

CALL_RULES = """Rules:
- Never guess. Leave out any key the notes don't support.
- "not interested", "hung up", "don't call again", "no thanks" -> status "dead", outcome "not_interested"
- they already have a system / app / software for inventory, or are happy with how they do it now -> status "dead", outcome "not_interested"
- an agreed callback, a demo booked, real interest -> status "warm", outcome "callback"
- reached the decision maker, had a real conversation, no clear next step -> status "contacted", outcome "answered"
- signed up, bought, installed -> status "won", outcome "answered"
- left a voicemail -> status "contacted", outcome "voicemail"
- nobody picked up and NO message was left (rang out, no voicemail, mailbox full) -> status "contacted", outcome "no_answer"
- "answered" means a real person picked up and spoke. If nobody picked up it is NEVER "answered"
- spoke to staff/a bartender/a gatekeeper, the decision maker wasn't in -> status "contacted", outcome "gatekeeper"
- Dates come from DATES: a weekday means the next one after today, "tomorrow" is the next
  day, "next week" with no day is 7 days from today, "in two weeks" is 14; for anything
  further, count from TODAY. Never a date before today.
- "call back at 3" is a best_time, not a date; put it in best_time and use the day it
  goes with, if one was given."""

DEBRIEF_SYSTEM = f"""You turn a salesperson's rough notes from a phone call into structured CRM fields.

They sell 86'd, an iPhone app for bar inventory and ordering, to independent bars and
restaurants. You are given TODAY, DATES (today and the next two weeks, with weekdays), THE
LEAD (the bar, who we've been asking for, what happened before) and their CALL NOTES.

Return ONLY a JSON object with these keys (omit any you cannot determine):
{CALL_FIELDS}
  "email": a corrected or newly learned email address
  "phone": a corrected or newly learned phone number

{CALL_RULES}
- Earlier history is context for "her", "him", "again". The fields describe THIS call.
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
# ONE model for every AI in the CRM, at ONE effort level — the owner's call:
# the notes reader, quick-add, the prep sheet, Ask AI, the AI bar, the inbox
# reader, the email drafter and the School all run on it. It used to be Haiku
# for most of them, and the drafts read like it. `CRM_AI_MODEL` is a new name
# on purpose: ANTHROPIC_MODEL / ANTHROPIC_ASSIST_MODEL may still be set on
# Render from the Haiku days, and reading them would quietly keep the old
# model. The product's bottle scanner (main.py, OpenAI -> Gemini) is a
# different system and is not affected.
AI_MODEL = os.getenv("CRM_AI_MODEL") or "claude-opus-5"
AI_EFFORT = os.getenv("CRM_AI_EFFORT") or "medium"
DEBRIEF_MODEL = AI_MODEL
# A thinking model spends tokens before it answers; the old per-call caps
# (200-900) were sized for Haiku's bare JSON and would cut it off mid-thought.
# Only what's used is billed, so the floor costs nothing when it isn't needed.
AI_MIN_TOKENS = 8000
AI_MIN_TIMEOUT = 90.0


def _ask_claude(system: str, user: str, max_tokens: int = 400,
                temperature: float = 0, timeout: float = 40.0, purpose: str = "") -> dict:
    """One JSON answer from Claude, for prompts that describe their own JSON
    shape. Raises HTTPException when unusable. See `_claude()` for how every
    CRM AI call is made; `temperature` is accepted and ignored (current models
    reject it) so callers written for Haiku don't change."""
    return _claude(system, user, max_tokens=max_tokens, timeout=timeout, purpose=purpose)


def _calendar(today: str) -> str:
    import assist as _assist
    d = date.fromisoformat(today)
    return f"TODAY: {today} ({d.strftime('%A')})\n\nDATES\n{_assist.dates_table(d)}"


def _lead_history(lead: dict, lines: int = 4) -> str:
    """The last few note lines, clipped: enough for "called her again" to
    resolve, not the whole history."""
    notes = [l.strip() for l in (lead.get("notes") or "").splitlines() if l.strip()]
    return "\n".join(l[:400] for l in notes[-lines:])


def _debrief_extract(text: str, lead: Optional[dict] = None,
                     today: Optional[str] = None) -> dict:
    """Ask the model for structured fields from a salesperson's rough notes,
    with the calendar and the lead's own recent history alongside."""
    today = today or _today()
    parts = [_calendar(today)]
    if lead:
        about = [f"Bar: {lead.get('name')}" + (f", {lead['loc']}" if lead.get("loc") else "")]
        if lead.get("contact"):
            about.append(f"Asking for: {lead['contact']}")
        if lead.get("last_outcome"):
            about.append(f"Last outcome: {lead['last_outcome']}")
        history = _lead_history(lead)
        if history:
            about.append("Recent history (oldest first):\n" + history)
        parts.append("THE LEAD\n" + "\n".join(about))
    parts.append("CALL NOTES\n" + text.strip())
    return _ask_claude(DEBRIEF_SYSTEM, "\n\n".join(parts), purpose="call-notes")


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
    # The date the model read off the calendar wins; a day count is the
    # older shape (and what an AI-bar log passes). Either must land between
    # today and a year out, or it's dropped rather than trusted.
    followup = None
    when = extracted.get("followup_date")
    if isinstance(when, str) and when.strip():
        try:
            followup = (date.fromisoformat(when.strip()[:10]) - date.fromisoformat(today)).days
        except ValueError:
            followup = None
    if followup is None:
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
    # The operator's own words beat the model on one question: did anybody
    # pick up. Only overrides "answered" or a blank — a callback or a "not
    # interested" is something a person said, so somebody did answer.
    heard = _no_answer_outcome(raw_text) if kind == "call" else None
    if heard and (outcome_guess == "answered" or outcome_guess not in TOUCH_OUTCOMES):
        outcome_guess = heard
    if outcome_guess not in TOUCH_OUTCOMES:
        # Older extractions, or a model that skipped the field: fall back to
        # the coarse guess rather than losing the outcome entirely. Never to
        # "answered" from status=contacted alone — contacted also covers a
        # call nobody picked up, and guessing "answered" there is what put a
        # rang-out call on screen as a conversation.
        if status == "dead":
            outcome_guess = "not_interested"
        elif status == "warm":
            outcome_guess = "callback"
        elif status == "won":
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
    #
    # `contact` is who to ASK FOR next time: `ask_for` when the model names
    # one (the decision maker), else the older `contact` key. Whoever actually
    # picked up goes in the note instead.
    def _text(key: str, limit: int) -> Optional[str]:
        v = extracted.get(key)
        return re.sub(r"\s+", " ", v).strip()[:limit] if isinstance(v, str) and v.strip() else None

    FIELD_LIMITS = {"contact": 200, "email": 320, "phone": 50}
    for field, limit in FIELD_LIMITS.items():
        value = _text("ask_for", limit) if field == "contact" else None
        value = value or _text(field, limit)
        if value:
            sets.append(f"{field} = %s"); params.append(value)
            applied[field] = value
    follow_days = followup if followup is not None else cadence_days
    if follow_days is not None:
        when = (date.fromisoformat(today) + timedelta(days=follow_days)).isoformat()
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
    # What the call taught us that has no column, labelled and on the same
    # line: the next call, the prep sheet, the drafter and the playbook all
    # read these. Who picked up only when it isn't who we'll ask for.
    spoke_to = _text("spoke_to", 200)
    extras = [("Spoke to", spoke_to if spoke_to and spoke_to != applied.get("contact") else None),
              ("Objection", _text("objection", 300)),
              ("How they do it now", _text("current_setup", 300)),
              ("Best time", _text("best_time", 120)),
              ("Next", _text("next_step", 300))]
    extras = [(k, v) for k, v in extras if v]
    if extras:
        note += " · " + " · ".join(f"{k}: {v}" for k, v in extras)
        applied["details"] = {k: v for k, v in extras}
    # The operator's OWN words, verbatim, every time. The summary is a
    # model's one-or-two-sentence rewrite and it drops whatever doesn't fit a
    # field — The Barrel House lost "Laura just paid $800 at the vet for her
    # cat" and "she thinks I should patent it", which are exactly what a
    # callback opens with. Flattened to one line so each call stays one entry.
    said = re.sub(r"\s+", " ", raw_text or "").strip()[:4000]
    if said and said.lower() != summary.lower():
        note += f" — Your notes: {said}"
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
    today = _today()
    # Read the lead first (no lock: the model call takes seconds, and a row
    # lock must never wait on a network round trip), so the model sees who
    # we've been asking for and what happened last time.
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
        before = cursor.fetchone()
    if not before:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": "Lead not found"})
    extracted = _debrief_extract(data.text, before, today)
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


# ============== AI BAR (Follow-ups) ==============
#
# A box above the Follow-ups list: "Barrel House — Laura's cell is
# 720-242-9667, call her back Friday", "Olde Town said no", "push everything
# overdue to Monday". Claude reads the whole book, works out which bars the
# message means, and proposes changes; assist.py checks every one before it
# is written (nothing a name, phone or email the operator didn't type), and
# each lead changed gets its own undo. See assist.py.
#
# Smarter model than the call-notes reader, on purpose: this one has to pick
# the right bar out of hundreds, turn "Friday" into a date and split one
# message across several leads — and a wrong guess writes to the CRM.
ASSIST_MODEL = AI_MODEL
# Server-side refusal fallbacks are documented for these models; sending the
# parameter to any other risks a 400 for nothing.
_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}


def _claude_json(system: str, user: str, schema: dict, model: Optional[str] = None,
                 max_tokens: int = 16000, timeout: float = 120.0,
                 context: Optional[str] = None, purpose: str = "") -> dict:
    """One schema-shaped JSON answer: structured outputs guarantee the reply
    parses against `schema`. See `_claude()`."""
    return _claude(system, user, schema=schema, model=model, max_tokens=max_tokens,
                   timeout=timeout, context=context, purpose=purpose)


def _claude(system: str, user: str, *, schema: Optional[dict] = None,
            model: Optional[str] = None, max_tokens: int = 16000, timeout: float = 120.0,
            context: Optional[str] = None, purpose: str = "") -> dict:
    """Every AI call the CRM makes goes through here.

    - One model and effort (`CRM_AI_MODEL` / `CRM_AI_EFFORT`), sent as
      `output_config.effort`; thinking is on by default on Opus 5 and its
      blocks come first, so only text blocks are read.
    - No prefill and no temperature: current models reject both with a 400.
    - `fallbacks: "default"` on the models that document it: a request the
      safety classifiers decline is re-run on Anthropic's recommended model
      inside the same call instead of coming back empty.
    - PROMPT CACHING. The system prompt, and `context` when given (a large
      block that stays the same across several calls, like the whole book the
      AI bar reads), are marked `cache_control`. A repeat within 5 minutes
      reads them at a tenth of the input price; everything that changes per
      call (the question, the notes) goes after them, uncached.
    - A 400 is retried once as the plainest request there is — no effort,
      no fallbacks, no cache markers, the schema written into the prompt —
      so an API-side change to any of those can't take the AI features down.
    - One `AI_USAGE` log line per call: tokens in/out and cache reads/writes,
      which is how a cost question gets answered from the Render logs.
    """
    import json as _json

    import httpx

    model = model or AI_MODEL
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(status_code=503, detail={
            "error": "ai_unavailable",
            "message": "No ANTHROPIC_API_KEY is set, so the AI can't run — "
                       "do this one by hand."})

    def send(plain: bool):
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        body: dict = {"model": model, "max_tokens": max(max_tokens, AI_MIN_TOKENS)}
        if plain:
            sys_text = system
            if schema:
                sys_text += ("\n\nReturn ONLY a JSON object matching this JSON schema:\n"
                             + _json.dumps(schema))
            body["system"] = sys_text
            body["messages"] = [{"role": "user", "content":
                                 (context + "\n\n" + user) if context else user}]
        else:
            cached = {"type": "ephemeral"}
            body["system"] = [{"type": "text", "text": system, "cache_control": cached}]
            content = ([{"type": "text", "text": context, "cache_control": cached},
                        {"type": "text", "text": user}] if context else user)
            body["messages"] = [{"role": "user", "content": content}]
            output: dict = {}
            if AI_EFFORT:
                output["effort"] = AI_EFFORT
            if schema:
                output["format"] = {"type": "json_schema", "schema": schema}
            if output:
                body["output_config"] = output
            if model in _FALLBACK_MODELS:
                body["fallbacks"] = "default"
                headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        return httpx.post(ANTHROPIC_URL, headers=headers, json=body,
                          timeout=max(timeout, AI_MIN_TIMEOUT))

    try:
        resp = send(False)
        if resp.status_code == 400:
            print(f"[crm] AI {purpose or '-'}: request refused, retrying plain: "
                  f"{resp.text[:200]}", flush=True)
            resp = send(True)
    except Exception as exc:
        print(f"[crm] AI {purpose or '-'} request failed: {exc}", flush=True)
        raise HTTPException(status_code=503, detail={
            "error": "ai_unavailable", "message": "Couldn't reach the AI — try again."})

    if resp.status_code != 200:
        # The body carries the real reason (bad key, rate limit, credit) and
        # it's an internal tool, so say it rather than making it a guess.
        detail = resp.text[:160]
        print(f"[crm] AI {purpose or '-'} HTTP {resp.status_code}: {detail}", flush=True)
        raise HTTPException(status_code=503, detail={
            "error": "ai_unavailable",
            "message": f"The AI returned {resp.status_code} — {detail}"})

    try:
        body = resp.json()
    except ValueError:
        body = {}
    usage = body.get("usage") or {}
    print(f"[crm] AI_USAGE {purpose or '-'} model={body.get('model') or model} "
          f"in={usage.get('input_tokens', 0)} cache_read={usage.get('cache_read_input_tokens', 0)} "
          f"cache_write={usage.get('cache_creation_input_tokens', 0)} "
          f"out={usage.get('output_tokens', 0)}", flush=True)
    stop = body.get("stop_reason")
    if stop == "refusal":
        raise HTTPException(status_code=503, detail={
            "error": "ai_declined",
            "message": "The AI declined that one — do it by hand."})
    if stop == "max_tokens":
        raise HTTPException(status_code=503, detail={
            "error": "ai_truncated",
            "message": "The AI ran out of room — try it as a shorter message."})
    text = "".join(b.get("text", "") for b in body.get("content", [])
                   if b.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    try:
        if start < 0 or end < start:
            raise ValueError("no JSON object in the reply")
        out = _json.loads(text[start:end + 1])
        if not isinstance(out, dict):
            raise ValueError("the reply is not a JSON object")
        return out
    except ValueError as exc:
        print(f"[crm] AI {purpose or '-'} returned unparseable JSON: {exc}", flush=True)
        raise HTTPException(status_code=503, detail={
            "error": "ai_unreadable",
            "message": "The AI's answer didn't parse — try again."})


def _apply_assist_change(cursor, lead, clean: dict, today: str, now: str) -> str:
    """Write one lead's checked change from the AI bar; return its undo id.

    A contact that HAPPENED ("called, left a voicemail") goes through
    `_apply_call_notes()`, the write /debrief and quick-add share, so it
    counts as a try, spends the day's counters and books the ladder's next
    attempt exactly as the Log call button would. Anything else is an edit:
    no touch, no counters, its own undo, and one dated line in the notes
    saying what changed, so the history shows it.
    """
    import assist as _assist

    fields = {k: clean[k] for k in ("name", "loc", "status", "contact", "phone",
                                    "email", "followup_date") if k in clean}
    if fields.get("phone"):
        digits = normalize_us_phone(fields["phone"])
        if digits:
            fields["phone"] = format_us_phone_dashed(digits)
    note = clean.get("note")

    if "logged" in clean:
        lg = clean["logged"]
        extracted = {"status": fields.get("status"), "outcome": lg["outcome"],
                     "summary": lg["summary"] or None}
        for f in ("contact", "email", "phone"):
            if f in fields:
                extracted[f] = fields[f]
        if fields.get("followup_date"):
            extracted["followup_in_days"] = (
                date.fromisoformat(fields["followup_date"]) - date.fromisoformat(today)).days
        _, _, undo_id, _ = _apply_call_notes(
            cursor, lead, extracted, lg["their_words"], lg["kind"], today, now)
        # What the call-notes write doesn't cover. Still under the same undo:
        # the snapshot was taken before any of this.
        extra: dict = {k: fields[k] for k in ("name", "loc") if k in fields}
        if "followup_date" in fields and fields["followup_date"] is None:
            extra["followup_date"] = None
        if lg["kind"] == "email" and not lg["outcome"]:
            extra["last_outcome"] = "emailed"   # what the Send button records
        line = f"[{today}] note: {note}" if note else None
    else:
        undo_id = _snapshot(cursor, lead, "edit (AI bar)", counters_spent=0)
        extra = fields
        bits = _assist.describe({k: v for k, v in clean.items() if k not in ("logged", "note")})
        if bits:
            line = f"[{today}] updated: {' · '.join(bits)}" + (f" — {note}" if note else "")
        else:
            line = f"[{today}] note: {note}" if note else None

    # Column names come from the fixed lists above, never from the model.
    sets = [f"{col} = %s" for col in extra]
    params: list = list(extra.values())
    if line:
        sets.append("notes = COALESCE(notes || E'\\n', '') || %s")
        params.append(line)
    if sets:
        sets.append("updated_at = %s")
        params += [now, lead["id"]]
        cursor.execute(f"UPDATE crm_leads SET {', '.join(sets)} WHERE id = %s", params)
    return undo_id


class AssistTurn(BaseModel):
    you: str = Field(default="", max_length=2000)
    ai: str = Field(default="", max_length=4000)


class AssistRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    # The last few exchanges, so "her", "that one" and "yes, the Denver one"
    # resolve. Kept by the page, not the server.
    history: list[AssistTurn] = Field(default_factory=list, max_length=6)
    # Whichever row's panel is open on screen — what a message naming no bar
    # is about.
    focus_lead_id: Optional[str] = Field(default=None, max_length=64)


@crm_router.post("/assist", response_model=dict)
def assist_update(data: AssistRequest, _: bool = Depends(require_crm_key)):
    """Plain English in, checked CRM changes out, each with its own undo."""
    import assist as _assist

    text = data.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail={
            "error": "empty", "message": "Type what happened or what to change."})
    today = _today()
    today_d = date.fromisoformat(today)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, loc, status, contact, phone, email, followup_date,
                   last_outcome, last_touch_at, notes, manager_name
              FROM crm_leads
             ORDER BY COALESCE(last_touch_at, '') DESC, created_at DESC
             LIMIT 2000
        """)
        leads = cursor.fetchall()
        tries = _touch_counts(cursor, [l["id"] for l in leads])
        # The log too, so the same box answers "who did I email Thursday" —
        # it replaced Ask AI on the CRM tab, which could answer but not act.
        cursor.execute("""
            SELECT lead_id, kind, outcome, at FROM crm_touches
             WHERE outcome IS DISTINCT FROM 'undone'
             ORDER BY at DESC LIMIT 300
        """)
        touches = cursor.fetchall()

    book, back = _assist.snapshot(leads, tries, today, data.focus_lead_id)
    alias_of = {lead_id: alias for alias, lead_id in back.items()}
    tz = _operator_tz()
    log = "\n".join(["TOUCHES (when, your time | lead | kind | outcome)"] + [
        f"{_ask_when(t['at'], tz)} | {alias_of.get(t['lead_id'], '?')} | {t['kind']} | "
        f"{t['outcome'] or ''}" for t in touches])
    history = [t.model_dump() for t in data.history][-4:]
    out = _claude_json(_assist.BAR_SYSTEM, _assist.message_block(text, history),
                       _assist.BAR_SCHEMA,
                       context=_assist.context_block(book, _assist.dates_table(today_d), log),
                       purpose="ai-bar")

    reply = str(out.get("reply") or "").strip()[:2000]
    question = out.get("question")
    question = str(question).strip()[:1000] if question else None
    proposed = out.get("changes") if isinstance(out.get("changes"), list) else []

    # A bar that isn't in the book can't be CHANGED — it has to be added. The
    # bar used to have no way to do that: "Added NE Moose Bar & Grill as a new
    # lead" came back over "couldn't match 'NE Moose Bar & Grill' to a lead",
    # and nothing was saved. New bars go through the same path as "Add a lead"
    # (_quick_add): the lead, the call, the follow-up, and a bar that IS in the
    # book after all gets the call logged on its existing row.
    new_texts = _assist.new_lead_texts(out, text, back)
    stray = [c for c in proposed if isinstance(c, dict)
             and re.fullmatch(r"L\d+", str(c.get("lead") or "").strip())
             and str(c.get("lead")).strip() not in back]
    proposed = [c for c in proposed if isinstance(c, dict)
                and str(c.get("lead") or "").strip() in back]
    applied, skipped = _apply_proposed(proposed, back, text, today)
    skipped += [{"lead": None, "why": f"couldn't match {c['lead']!r} to a lead"} for c in stray]
    added: list = []
    for part in new_texts:
        try:
            made = _quick_add(part)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            skipped.append({"lead": None, "why": "couldn't add the new bar — "
                            + str(detail.get("message") or exc.detail)})
            continue
        except Exception as exc:
            print(f"[crm] AI_BAR_ADD_FAILED {exc}", flush=True)
            skipped.append({"lead": None, "why": "couldn't add the new bar — try Add a lead"})
            continue
        lead = made["lead"]
        existing = (made.get("applied") or {}).get("matched_existing")
        changed = [("already in your book — logged the call on it" if existing
                    else "added as a new lead")]
        if lead.get("last_outcome"):
            changed.append(f"logged call ({lead['last_outcome'].replace('_', ' ')})")
        if lead.get("contact"):
            changed.append(f"ask for {lead['contact']}")
        if lead.get("followup_date"):
            changed.append(f"follow-up → {lead['followup_date']}")
        applied.append({"lead_id": lead["id"], "name": lead["name"],
                        "changed": changed, "undo_id": made.get("undo_id")})
        added.append(lead["name"])
    if added:
        said = ", ".join(added)
        reply = (reply + " " if reply else "") + f"Saved: {said}."
    elif not applied and new_texts:
        reply = "Nothing was saved — see below."
    return {"reply": reply, "question": question, "applied": applied, "skipped": skipped}


def _apply_proposed(proposed: list, back: dict, text: str, today: str,
                    allow_logged: bool = True) -> tuple[list, list]:
    """Check each proposed change against `text` (assist.clean_change) and
    write what passes, one undo per lead. Only aliases in `back` can be
    touched — for the inbox that's the leads the email is about, and nothing
    else in the book."""
    import assist as _assist

    today_d = date.fromisoformat(today)
    now = now_iso()
    applied: list = []
    skipped: list = []
    with get_db() as conn:
        cursor = conn.cursor()
        for change in proposed[:_assist.MAX_CHANGES]:
            if not isinstance(change, dict):
                continue
            alias = str(change.get("lead") or "").strip()
            lead_id = back.get(alias)
            if not lead_id:
                skipped.append({"lead": None, "why": f"couldn't match {alias!r} to a lead"})
                continue
            cursor.execute("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE", (lead_id,))
            lead = cursor.fetchone()
            if not lead:
                skipped.append({"lead": None, "why": "that lead has since been deleted"})
                continue
            clean, problems = _assist.clean_change(change, lead, text, today_d)
            if not allow_logged:
                clean.pop("logged", None)
            skipped += [{"lead": lead["name"], "why": p} for p in problems]
            if not clean:
                continue
            undo_id = _apply_assist_change(cursor, lead, clean, today, now)
            applied.append({"lead_id": lead_id, "name": lead["name"],
                            "changed": _assist.describe(clean), "undo_id": undo_id})
        conn.commit()
    return applied, skipped


# ============== THE INBOX, WHILE YOU SLEEP ==============
#
# Every few minutes (main.py's _inbox_loop) the mailbox is read — read-only,
# nothing marked as read — and each new email from a bar in the book goes
# through the AI bar's engine: a new contact, a "no thanks", "call me
# Tuesday" land on the lead without anyone typing them in. Same gate as the
# bar (nothing written that the email doesn't say), and an email can only
# change the leads it's about. Everything it did shows under Follow-ups,
# "While you were away", each with Undo. See inbox.py.

INBOX_BATCH = int(os.getenv("CRM_INBOX_BATCH", "20"))   # emails handled per pass, max


INBOX_MAX_TRIES = 3
_INBOX_FAILS: dict = {}     # message_id -> failed passes, this process


def process_inbox(days: int = 3) -> dict:
    import assist as _assist
    import inbox as _inbox
    import mailer

    if not mailer.is_configured() or not os.getenv("ANTHROPIC_API_KEY"):
        return {"skipped": "no mailbox or no AI key"}
    raws = mailer.fetch_recent(days=days)
    mails = [m for m in (_inbox.parse(r) for r in raws) if m.get("message_id")]
    if not mails:
        return {"read": 0}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT message_id FROM crm_inbox WHERE message_id = ANY(%s)",
                       ([m["message_id"] for m in mails],))
        done = {r["message_id"] for r in cursor.fetchall()}
        cursor.execute("SELECT id, email FROM crm_leads WHERE email IS NOT NULL")
        book = cursor.fetchall()
        cursor.execute("SELECT message_id, lead_id FROM crm_sent_messages")
        sent = {r["message_id"]: r["lead_id"] for r in cursor.fetchall()}

    tally = {"updated": 0, "no_change": 0, "ignored": 0, "failed": 0}
    handled = 0
    for mail in mails:
        if mail["message_id"] in done or handled >= INBOX_BATCH:
            continue
        lead_ids = (_inbox.match_leads(mail, book, sent)
                    if _inbox.worth_reading(mail, mailer.sender()) else [])
        result, status, draft = None, "ignored", None
        if lead_ids:
            handled += 1
            try:
                result = _read_reply(mail, lead_ids)
                if result.get("opt_out"):
                    result["applied"] = _record_opt_out(mail, lead_ids) + result["applied"]
                status = "updated" if result["applied"] else "no_change"
            except Exception as exc:
                print(f"[crm] INBOX_FAILED {mail.get('subject')!r}: {exc}", flush=True)
                result, status = {"error": str(exc)[:300]}, "failed"
            if result and result.get("needs_reply"):
                draft = _reply_draft_for(mail, lead_ids[0])
        tally[status] += 1
        if status == "failed":
            # Not recorded, so the next pass tries it again — but only
            # INBOX_MAX_TRIES times. Each try is a paid model call, and a mail
            # that always fails used to be retried every 5 minutes for as long
            # as it stayed in the fetch window (up to 20 of them a pass).
            fails = _INBOX_FAILS[mail["message_id"]] = _INBOX_FAILS.get(mail["message_id"], 0) + 1
            if fails < INBOX_MAX_TRIES:
                continue
            print(f"[crm] INBOX_GAVE_UP after {fails} tries: {mail.get('subject')!r}", flush=True)
            _INBOX_FAILS.pop(mail["message_id"], None)
            # Giving up on reading it must never mean ignoring "stop emailing
            # me": the plain-words check needs no model, and an opt-out has to
            # be honoured (CAN-SPAM) however the rest of the mail went.
            if lead_ids and _inbox.looks_like_opt_out(mail.get("text")):
                try:
                    _record_opt_out(mail, lead_ids)
                    result = {**(result or {}), "opt_out": True}
                except Exception as exc:
                    print(f"[crm] INBOX_OPT_OUT_FAILED {mail.get('from_addr')}: {exc}", flush=True)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO crm_inbox (message_id, from_addr, from_name, subject,
                                       received_at, lead_ids, status, result, processed_at,
                                       body_text, opt_out, needs_reply, draft)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (mail["message_id"], mail["from_addr"], mail["from_name"], mail["subject"],
                  mail["date"], ",".join(lead_ids), status,
                  json.dumps(result) if result else None, now_iso(),
                  (mail.get("text") or "")[:4000] if lead_ids else None,
                  bool(result and result.get("opt_out")),
                  bool(result and result.get("needs_reply")),
                  json.dumps(draft) if draft else None))
            conn.commit()
    if tally["updated"] or tally["no_change"] or tally["failed"]:
        print(f"[crm] INBOX {tally}", flush=True)
    return tally


def _read_reply(mail: dict, lead_ids: list) -> dict:
    """One email through the AI bar's engine, able to touch only `lead_ids`."""
    import assist as _assist
    import inbox as _inbox

    today = _today()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, loc, status, contact, phone, email, followup_date,
                   last_outcome, last_touch_at, notes, manager_name
              FROM crm_leads WHERE id = ANY(%s)
        """, (lead_ids,))
        leads = cursor.fetchall()
        tries = _touch_counts(cursor, lead_ids)
    book, back = _assist.snapshot(leads, tries, today, lead_ids[0])
    text = (f"From: {mail['from_name']} <{mail['from_addr']}>\n"
            f"Subject: {mail['subject']}\n\n{mail['text']}")
    # The rules are the same for every email in a pass, so they're the cached
    # system prompt; the leads and the email come after them.
    out = _claude_json(_assist.SYSTEM + _inbox.INBOX_RULES, _assist.message_block(text, []),
                       _inbox.INBOX_SCHEMA,
                       context=_assist.context_block(
                           book, _assist.dates_table(date.fromisoformat(today))),
                       purpose="inbox")
    proposed = out.get("changes") if isinstance(out.get("changes"), list) else []
    # An opt-out is honoured whatever the model thought, if the words say so.
    opt_out = bool(out.get("opt_out")) or _inbox.looks_like_opt_out(mail.get("text"))
    if opt_out:
        proposed = []           # _record_opt_out does the writing, and nothing else should
    applied, skipped = _apply_proposed(proposed, back, text, today, allow_logged=False)
    return {"reply": str(out.get("reply") or "").strip()[:1000],
            "applied": applied, "skipped": skipped, "opt_out": opt_out,
            "needs_reply": bool(out.get("needs_reply")) and not opt_out}


def _record_opt_out(mail: dict, lead_ids: list) -> list:
    """They asked not to be emailed again. The address goes on the do-not-
    contact list for good — every send path checks it, and Undo does NOT lift
    it (US law requires honouring an opt-out) — and each lead it's about goes
    dead with its follow-up cleared and a note saying why. The lead changes
    are undoable in case the AI misread; the suppression stays."""
    today, now = _today(), now_iso()
    who = mail.get("from_name") or mail.get("from_addr")
    applied = []
    with get_db() as conn:
        cursor = conn.cursor()
        if mail.get("from_addr"):
            cursor.execute("""
                INSERT INTO crm_suppressions (id, kind, value, reason, created_at)
                VALUES (%s, 'email', %s, %s, %s) ON CONFLICT DO NOTHING
            """, (generate_id(), mail["from_addr"].lower(),
                  f"asked not to be emailed ({today}, replying to us)", now))
        for lead_id in lead_ids:
            cursor.execute("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE", (lead_id,))
            lead = cursor.fetchone()
            if not lead:
                continue
            undo_id = _snapshot(cursor, lead, "edit (opted out by email)")
            note = (f"[{today}] {who} asked not to be emailed again. {mail.get('from_addr')} "
                    "is on the do-not-email list; never email it again.")
            cursor.execute("""
                UPDATE crm_leads SET status = 'dead', followup_date = NULL, updated_at = %s,
                       notes = COALESCE(notes || E'\\n', '') || %s
                 WHERE id = %s
            """, (now, note, lead_id))
            applied.append({"lead_id": lead_id, "name": lead["name"], "undo_id": undo_id,
                            "changed": ["asked not to be emailed", "stage → dead",
                                        "follow-up cleared", "on the do-not-email list"]})
        conn.commit()
    return applied


def _reply_draft_for(mail: dict, lead_id: str) -> Optional[dict]:
    """A reply to their email, drafted overnight for the owner to review and
    send in the morning. Never sent by itself. None when drafting fails —
    the email is still recorded, and the page offers to draft it then."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM crm_leads WHERE id = %s", (lead_id,))
            row = cursor.fetchone()
        if not row:
            return None
        draft = _write_draft(row, _reply_ask({**mail, "body_text": mail.get("text")}))
        return {**draft, "to": mail.get("from_addr"), "lead_id": lead_id}
    except Exception as exc:
        print(f"[crm] INBOX_DRAFT_FAILED {mail.get('subject')!r}: {exc}", flush=True)
        return None


def _inbox_mail(message_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT message_id, from_name, from_addr, subject, body_text, lead_ids
              FROM crm_inbox WHERE message_id = %s
        """, (message_id,))
        row = cursor.fetchone()
    return dict(row) if row else None


def _email_suppressed(cursor, addr: Optional[str]) -> Optional[str]:
    """Why this address must not be emailed, or None."""
    if not addr:
        return None
    cursor.execute("SELECT reason FROM crm_suppressions WHERE kind = 'email' "
                   "AND LOWER(value) = LOWER(%s)", (addr.strip(),))
    row = cursor.fetchone()
    return (row["reason"] or "on the do-not-email list") if row else None


INBOX_SHOWN = 50        # notes on "While you were away"
INBOX_DELETED_SHOWN = 30  # deleted notes that can still be put back


def _inbox_item(r):
    """One crm_inbox row as the page shows it."""
    try:
        result = json.loads(r["result"] or "{}")
    except ValueError:
        result = {}
    try:
        draft = json.loads(r["draft"]) if r["draft"] else None
    except ValueError:
        draft = None
    return {"from": r["from_name"] or r["from_addr"], "from_addr": r["from_addr"],
            "subject": r["subject"], "received_at": r["received_at"],
            "processed_at": r["processed_at"], "status": r["status"],
            "reply": result.get("reply"), "applied": result.get("applied") or [],
            "skipped": result.get("skipped") or [],
            "message_id": r["message_id"],
            "lead_id": (r["lead_ids"] or "").split(",")[0] or None,
            "opt_out": bool(r["opt_out"]), "needs_reply": bool(r["needs_reply"]),
            "draft": draft, "replied_at": r["replied_at"],
            "dismissed_at": r.get("dismissed_at")}


@crm_router.get("/inbox", response_model=dict)
def inbox_feed(hours: int = 72, _: bool = Depends(require_crm_key)):
    """What the inbox reader did lately — Follow-ups' "While you were away".
    Notes the operator deleted are left out of `items` and listed, most
    recently deleted first, under `deleted`, so one deleted by mistake can
    still be put back after the toast's Undo is gone."""
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 720)))).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT message_id, from_addr, from_name, subject, received_at, status,
                   result, processed_at, lead_ids, opt_out, needs_reply, draft, replied_at,
                   dismissed_at
              FROM crm_inbox
             WHERE status IN ('updated', 'no_change') AND processed_at >= %s
             ORDER BY processed_at DESC LIMIT 200
        """, (since,))
        rows = cursor.fetchall()
        cursor.execute("SELECT MAX(processed_at) AS last FROM crm_inbox")
        last = cursor.fetchone()["last"]
    items = [_inbox_item(r) for r in rows if not r.get("dismissed_at")]
    deleted = sorted((_inbox_item(r) for r in rows if r.get("dismissed_at")),
                     key=lambda it: it["dismissed_at"], reverse=True)
    return {"items": items[:INBOX_SHOWN], "deleted": deleted[:INBOX_DELETED_SHOWN],
            "last_processed": last}


class InboxNote(BaseModel):
    message_id: str = Field(..., min_length=1, max_length=1000)


def _set_dismissed(message_id, dismiss):
    """Stamp (or clear) dismissed_at on one note. A second delete keeps the
    first time, so "deleted 2h ago" stays true."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crm_inbox
               SET dismissed_at = CASE WHEN %s THEN COALESCE(dismissed_at, %s) END
             WHERE message_id = %s
         RETURNING message_id, dismissed_at
        """, (bool(dismiss), now_iso(), message_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "That note isn't in the inbox log any more."})
        conn.commit()
    return {"message_id": row["message_id"], "dismissed_at": row["dismissed_at"]}


@crm_router.post("/inbox/dismiss", response_model=dict)
def inbox_dismiss(data: InboxNote, _: bool = Depends(require_crm_key)):
    """Take a note off "While you were away" — the operator already knows.

    Only the NOTE goes. What the reply changed on the lead stays (each change
    has its own Undo), an opt-out stays on the do-not-email list, and the row
    is kept, only stamped: the reader treats a message it has no row for as
    new mail, so deleting the row would bring the email back on the next pass
    and apply its changes a second time."""
    return _set_dismissed(data.message_id, True)


@crm_router.post("/inbox/restore", response_model=dict)
def inbox_restore(data: InboxNote, _: bool = Depends(require_crm_key)):
    """Put a deleted note back on the list — the Undo, and the Restore button
    under "Deleted"."""
    return _set_dismissed(data.message_id, False)


@crm_router.post("/inbox/check", response_model=dict)
def inbox_check(_: bool = Depends(require_crm_key)):
    """Read the inbox now instead of waiting for the next pass."""
    import mailer
    try:
        return process_inbox()
    except mailer.MailFailed as exc:
        raise HTTPException(status_code=502, detail={"error": "inbox_failed", "message": str(exc)})


# ============== ASK AI ==============
#
# A question box above the CRM tab's search: "how many people did I call",
# "who did we leave a voicemail on the 22nd", "who did we email last
# Thursday". Claude is handed a snapshot of the book — every lead and the
# full call/email log — and answers from that alone. It is deliberately NOT
# allowed to write SQL: the same database holds customer accounts and
# password hashes, and a snapshot of CRM rows is all a sales question needs.
# Read-only by construction: nothing here writes anything.

ASK_SYSTEM = """You answer questions about a salesperson's CRM. They sell 86'd, an iPhone
app for bar inventory, to independent bars and restaurants, mostly by cold-calling.

You are given a snapshot: TODAY, a LEADS table and a TOUCHES log (every call and email
logged, newest first). Answer ONLY from the snapshot. Never invent a lead, a number,
a date or a name. If the snapshot can't answer it, say so plainly.

Meanings:
- A touch's outcome: answered = a person picked up and spoke; voicemail = left a
  message; no_answer = nobody picked up, no message left; gatekeeper = spoke to staff,
  decision maker not in; not_interested = said no (lead is dead); callback = asked to
  be called back / showed interest; logged = recorded with no specific outcome.
- Lead status: new = never worked; contacted; warm = interested; won = signed up;
  dead = said no. "Open" means anything not dead.
- All dates and times are the salesperson's own local time (given in TODAY). Resolve
  "today", "yesterday", "last Thursday", "this week" against TODAY. "Last Thursday" is
  the most recent Thursday before today.
- Count carefully. When asked "how many", give the number first.

Return ONLY a JSON object:
  "answer": the answer in plain, short sentences (no markdown tables). Lead with the
            direct answer. At most ~6 short lines.
  "leads": the ids (like "L12") of the leads the answer is about, most relevant first,
           or [] if it isn't about particular leads. At most 50."""


def _ask_when(iso: Optional[str], tz) -> str:
    """A stored UTC ISO timestamp as the operator's local 'YYYY-MM-DD Thu 2:05pm'."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)[:16]
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(tz)
    return local.strftime("%Y-%m-%d %a ") + local.strftime("%I:%M%p").lstrip("0").lower()


def _ask_snapshot(leads: list, touches: list, now: datetime, tz) -> tuple[str, dict]:
    """Everything Ask AI may see, as compact text, plus alias -> lead id.

    Leads get short aliases (L1, L2 ...) instead of their ids: the ids are
    long and would be repeated on every touch line, which is most of the
    tokens for no information.
    """
    alias: dict = {}
    back: dict = {}
    for i, lead in enumerate(leads, 1):
        alias[lead["id"]] = f"L{i}"
        back[f"L{i}"] = lead["id"]

    def clip(v, n):
        v = (v or "").replace("\n", " ").replace("|", "/").strip()
        return v[:n]

    lines = [f"TODAY: {now.astimezone(tz).strftime('%Y-%m-%d %A %I:%M%p')} "
             f"(salesperson's local time, {getattr(tz, 'key', 'UTC')})", "",
             "LEADS (id | name | where | status | last outcome | calls | last touched | "
             "follow-up | contact | email | latest note)"]
    for lead in leads:
        notes = [n for n in (lead.get("notes") or "").splitlines() if n.strip()]
        lines.append(" | ".join([
            alias[lead["id"]], clip(lead.get("name"), 60), clip(lead.get("loc"), 40),
            lead.get("status") or "", lead.get("last_outcome") or "",
            str(lead.get("attempts") or 0), _ask_when(lead.get("last_touch_at"), tz),
            lead.get("followup_date") or "", clip(lead.get("contact"), 40),
            clip(lead.get("email"), 60), clip(notes[-1] if notes else "", 700),
        ]))
    lines += ["", "TOUCHES (when | lead | kind | outcome | attempt #)"]
    for t in touches:
        lines.append(" | ".join([
            _ask_when(t.get("at"), tz), alias.get(t.get("lead_id"), "?"),
            t.get("kind") or "", t.get("outcome") or "", str(t.get("attempt") or ""),
        ]))
    return "\n".join(lines), back


class AskCRM(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@crm_router.post("/ask", response_model=dict)
def ask_crm(data: AskCRM, _: bool = Depends(require_crm_key)):
    """Plain-English questions about the CRM, answered from a snapshot of it."""
    tz = _operator_tz()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, loc, status, last_outcome, attempts, last_touch_at,
                   followup_date, contact, email, phone, notes
              FROM crm_leads
             ORDER BY COALESCE(last_touch_at, '') DESC, created_at DESC
             LIMIT 2000
        """)
        leads = cursor.fetchall()
        # Undone touches never happened as far as the operator is concerned.
        cursor.execute("""
            SELECT lead_id, kind, outcome, attempt, at FROM crm_touches
             WHERE outcome IS DISTINCT FROM 'undone'
             ORDER BY at DESC LIMIT 3000
        """)
        touches = cursor.fetchall()

    snapshot, back = _ask_snapshot(leads, touches, datetime.now(timezone.utc), tz)
    out = _ask_claude(ASK_SYSTEM, f"{snapshot}\n\nQUESTION: {data.question.strip()}",
                      max_tokens=900, timeout=60.0)

    answer = str(out.get("answer") or "").strip()[:3000] or "I couldn't work that out."
    by_id = {l["id"]: l for l in leads}
    picked = []
    for a in (out.get("leads") or [])[:50]:
        lead = by_id.get(back.get(str(a).strip()))
        if lead and lead not in picked:
            picked.append(lead)
    return {"answer": answer, "leads": [{
        "id": l["id"], "name": l["name"], "loc": l["loc"], "status": l["status"],
        "last_outcome": l["last_outcome"], "last_touch_at": l["last_touch_at"],
        "phone_dial": format_us_phone_dashed(phone_digits(l.get("phone"))),
    } for l in picked]}


# ============== APPLE ANALYTICS ==============
#
# The burger menu's Apple Analytics tab: App Store Connect's App Analytics
# numbers for the 86'd app, pulled through Apple's Analytics Reports API (see
# apple.py for how that API works and why the first data takes a day or two).
#
# Credentials: the team API key's Issuer ID, Key ID and .p8 private key. Env
# vars win when set (APPLE_ISSUER_ID / APPLE_KEY_ID / APPLE_PRIVATE_KEY /
# APPLE_APP_ID); otherwise the page's Connect form saves them here. The .p8 is
# stored ENCRYPTED with a key derived from SECRET_KEY and is never sent back
# to the page — the page only ever learns whether a key is set. Rotating
# SECRET_KEY makes a saved key unreadable; the tab then asks to reconnect.

import apple as _apple

APPLE_STALE_HOURS = 6        # opening the tab re-syncs when data is older than this
_apple_lock = threading.Lock()
_apple_state: dict = {"running": False}


def init_apple_tables():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_apple_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                issuer_id TEXT, key_id TEXT, private_key_enc TEXT,
                app_ref TEXT, app_id TEXT, app_name TEXT, bundle_id TEXT,
                request_id TEXT,
                last_sync_at TEXT, last_sync_ok BOOLEAN, last_error TEXT,
                updated_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_apple_instances (
                id TEXT PRIMARY KEY, report TEXT, processing_date TEXT, imported_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_apple_metrics (
                report TEXT NOT NULL, day TEXT NOT NULL, dim TEXT NOT NULL,
                metric TEXT NOT NULL, value DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (report, day, dim, metric)
            )
        """)
        conn.commit()


def _apple_fernet():
    import base64
    import hashlib

    from cryptography.fernet import Fernet
    from auth import SECRET_KEY
    return Fernet(base64.urlsafe_b64encode(
        hashlib.sha256(("86d-apple-analytics:" + SECRET_KEY).encode()).digest()))


def _apple_config() -> dict:
    """Saved config merged with env overrides. `private_key` is decrypted."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_apple_config WHERE id = 1")
        row = dict(cursor.fetchone() or {})
    key = None
    if row.get("private_key_enc"):
        try:
            key = _apple_fernet().decrypt(row["private_key_enc"].encode()).decode()
        except Exception:
            key = None
            row["key_unreadable"] = True
    env = {"issuer_id": os.getenv("APPLE_ISSUER_ID"), "key_id": os.getenv("APPLE_KEY_ID"),
           "private_key": os.getenv("APPLE_PRIVATE_KEY"), "app_ref": os.getenv("APPLE_APP_ID")}
    from_env = bool(env["issuer_id"] and env["key_id"] and env["private_key"])
    cfg = {**row, "private_key": key}
    if from_env:
        cfg.update({k: v for k, v in env.items() if v})
    cfg["source"] = "env" if from_env else ("saved" if key else None)
    cfg["connected"] = bool(cfg.get("issuer_id") and cfg.get("key_id") and cfg.get("private_key"))
    # Diagnostic only, never a substitute for `connected`: which of the three
    # Render env vars this process can actually see right now, so the page
    # can say "APPLE_KEY_ID isn't set" instead of a bare connect form when an
    # operator swears they set it — a name typo or the wrong Render service
    # is the far more common cause than anything in this file.
    cfg["env_seen"] = {k: bool(env[k]) for k in ("issuer_id", "key_id", "private_key")}
    return cfg


def _apple_save(**fields):
    fields["updated_at"] = now_iso()
    cols = list(fields)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            INSERT INTO crm_apple_config (id, {', '.join(cols)})
            VALUES (1, {', '.join(['%s'] * len(cols))})
            ON CONFLICT (id) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)}
        """, [fields[c] for c in cols])
        conn.commit()


def _apple_client(cfg: dict) -> "_apple.ASC":
    return _apple.ASC(cfg["key_id"], cfg["issuer_id"], cfg["private_key"])


def _apple_sync_job():
    """One background import. Errors are stored for the page, never raised."""
    try:
        cfg = _apple_config()
        if not cfg["connected"]:
            return
        asc = _apple_client(cfg)
        app_id = cfg.get("app_id")
        if not app_id:
            app = _apple.resolve_app(asc, cfg.get("app_ref"))
            app_id = app["id"]
            _apple_save(app_id=app_id, app_name=app["name"], bundle_id=app["bundle_id"])
        request_id = _apple.ensure_report_request(asc, app_id, cfg.get("request_id"))
        if request_id != cfg.get("request_id"):
            _apple_save(request_id=request_id)

        def already(inst_id):
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM crm_apple_instances WHERE id = %s", (inst_id,))
                return cursor.fetchone() is not None

        def save(inst_id, report, pdate, totals):
            with get_db() as conn:
                cursor = conn.cursor()
                for (day, dim, metric), value in totals.items():
                    cursor.execute("""
                        INSERT INTO crm_apple_metrics (report, day, dim, metric, value)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (report, day, dim, metric) DO UPDATE SET value = EXCLUDED.value
                    """, (report[:200], day, dim[:200], metric[:120], value))
                cursor.execute("""
                    INSERT INTO crm_apple_instances (id, report, processing_date, imported_at)
                    VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING
                """, (inst_id, report[:200], pdate, now_iso()))
                conn.commit()

        result = _apple.sync(asc, request_id, already, save)
        _apple_save(last_sync_at=now_iso(), last_sync_ok=True, last_error=None)
        print(f"[crm] APPLE_SYNC ok reports={result['reports']} imported={result['imported']} "
              f"partial={result['partial']}", flush=True)
    except _apple.AppleError as e:
        _apple_save(last_sync_at=now_iso(), last_sync_ok=False, last_error=str(e)[:500])
        print(f"[crm] APPLE_SYNC failed: {e}", flush=True)
    except Exception as e:
        _apple_save(last_sync_at=now_iso(), last_sync_ok=False,
                    last_error=f"Unexpected error: {e!r}"[:500])
        print(f"[crm] APPLE_SYNC crashed: {e!r}", flush=True)
    finally:
        _apple_state["running"] = False


def _apple_start_sync() -> bool:
    with _apple_lock:
        if _apple_state["running"]:
            return False
        _apple_state["running"] = True
    threading.Thread(target=_apple_sync_job, daemon=True, name="apple-sync").start()
    return True


def _apple_status(cfg: dict) -> dict:
    return {
        "connected": cfg["connected"], "source": cfg.get("source"),
        "key_unreadable": bool(cfg.get("key_unreadable")),
        "env_seen": cfg.get("env_seen"),
        "issuer_id": cfg.get("issuer_id"), "key_id": cfg.get("key_id"),
        "app": {"id": cfg.get("app_id"), "name": cfg.get("app_name"),
                "bundle_id": cfg.get("bundle_id")} if cfg.get("app_id") else None,
        "reports_requested": bool(cfg.get("request_id")),
        "last_sync_at": cfg.get("last_sync_at"), "last_sync_ok": cfg.get("last_sync_ok"),
        "last_error": cfg.get("last_error"), "syncing": _apple_state["running"],
    }


@crm_router.get("/apple", response_model=dict)
def apple_analytics(window: int = 30, _: bool = Depends(require_crm_key)):
    """Status + everything imported so far. Kicks off a sync when stale."""
    window = 7 if window <= 7 else 90 if window >= 90 else 30
    cfg = _apple_config()
    if cfg["connected"]:
        last = cfg.get("last_sync_at")
        stale = True
        if last:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))
                stale = age > timedelta(hours=APPLE_STALE_HOURS)
            except ValueError:
                pass
        if stale:
            _apple_start_sync()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT report, day, dim, metric, value FROM crm_apple_metrics "
                       "WHERE day >= %s",
                       ((datetime.now(timezone.utc) - timedelta(days=2 * window + 10)).strftime("%Y-%m-%d"),))
        rows = [dict(r) for r in cursor.fetchall()]
    return {**_apple_status(_apple_config()), **_apple.summarize(rows, window)}


class AppleConnect(BaseModel):
    issuer_id: str = Field(min_length=8, max_length=100)
    key_id: str = Field(min_length=4, max_length=40)
    private_key: str = Field(min_length=40, max_length=10000)
    app: Optional[str] = Field(default=None, max_length=200)


@crm_router.post("/apple/connect", response_model=dict)
def apple_connect(data: AppleConnect, _: bool = Depends(require_crm_key)):
    """Check the key against Apple RIGHT NOW, then save it and start importing.

    Checking first means a typo'd Issuer ID fails here, with Apple's reason,
    instead of being saved and failing silently in the background later.
    """
    key = _apple.normalize_key(data.private_key)
    asc = _apple.ASC(data.key_id.strip(), data.issuer_id.strip(), key)
    try:
        app = _apple.resolve_app(asc, data.app)
    except _apple.AppleError as e:
        raise HTTPException(status_code=422, detail={"error": "apple_rejected", "message": str(e)})
    _apple_save(issuer_id=data.issuer_id.strip(), key_id=data.key_id.strip(),
                private_key_enc=_apple_fernet().encrypt(key.encode()).decode(),
                app_ref=(data.app or "").strip() or None, app_id=app["id"],
                app_name=app["name"], bundle_id=app["bundle_id"], request_id=None,
                last_sync_at=None, last_sync_ok=None, last_error=None)
    _apple_start_sync()
    return {"ok": True, "app": app}


@crm_router.post("/apple/sync", response_model=dict)
def apple_sync(_: bool = Depends(require_crm_key)):
    if not _apple_config()["connected"]:
        raise HTTPException(status_code=409, detail={
            "error": "not_connected", "message": "Connect your Apple API key first."})
    return {"started": _apple_start_sync(), "syncing": True}


@crm_router.post("/apple/disconnect", response_model=dict)
def apple_disconnect(_: bool = Depends(require_crm_key)):
    """Forget the saved key. Imported numbers stay — they're yours either way."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crm_apple_config WHERE id = 1")
        conn.commit()
    return {"ok": True}


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


QUICK_ADD_SYSTEM = f"""You turn a salesperson's rough notes about a call they just made — to a bar or restaurant that ISN'T already in the CRM — into a new lead record. The notes are often pasted straight off a website or Google listing, with words run together ("80002Primary Phone:") — read through that.

They sell 86'd, an iPhone app for bar inventory and ordering, to independent bars and
restaurants. You are given TODAY, DATES (today and the next two weeks, with weekdays) and
their NOTES.

Return ONLY a JSON object with these keys (omit any you truly cannot find).
About the venue:
  "name": the bar or restaurant's name. It is almost always the first thing in the notes and often repeated (e.g. after "Website:"). ALWAYS return it when any venue name appears anywhere in the text.
  "loc": "City, ST"
  "address": street address if given
  "phone": the main phone number
  "other_phones": any additional phone numbers, as one string
  "website": a URL if one is given (only a real URL, never a guess)
  "email": an email address, if one is given
  "email_on_website": true if the notes say the email is on their website / to find it on the site
  "decision_makers": who makes the buying decision, with role if given (e.g. "Mallory and Mike (owners)")
About the call:
{CALL_FIELDS}

{CALL_RULES}
"""


def _quick_add_extract(text: str, today: Optional[str] = None) -> dict:
    return _ask_claude(QUICK_ADD_SYSTEM,
                       _calendar(today or _today()) + "\n\nNOTES\n" + text.strip(),
                       purpose="quick-add")


_NAME_CUT = re.compile(r"\s+(?:at|@|-|–|—|:)\s+|\s*\(\d|\s*\d{3}[-.\s]\d{3}|[.,;\n]")


def _name_from_text(text: str) -> Optional[str]:
    """Last-resort name: whatever leads the notes, up to the first phone
    number, "at", or punctuation. Pasted listings start with the venue."""
    head = _NAME_CUT.split(text.strip(), maxsplit=1)[0].strip()
    head = re.sub(r"^(called|call(ing)?|spoke (to|with))\s+", "", head, flags=re.I).strip()
    return head[:200] if 2 <= len(head) <= 80 else None


def _clean(v, limit: int = 300) -> Optional[str]:
    return v.strip()[:limit] if isinstance(v, str) and v.strip() else None


def _find_existing_lead(cursor, name: str, loc: Optional[str], phones,
                        email: Optional[str]):
    """The lead already in the book for this bar, locked, or None.

    Same phone and the same name (leadgen.same_venue: "Olde Town Tavern & Grill"
    is "Olde Town Tavern"), else the same email, else the same name in the
    same town. Quick-add used to skip this and create a second row for a bar
    the generator had already put on the call list: the copy with the call
    landed in the CRM tab, and the never-called copy stayed on the call list.
    The one someone has worked wins, then the oldest.
    """
    from leadgen import same_venue

    found = []
    for digits in phones:
        cursor.execute(
            "SELECT * FROM crm_leads "
            "WHERE RIGHT(regexp_replace(COALESCE(phone, ''), '\\D', '', 'g'), 10) = %s",
            (digits,))
        found += [r for r in cursor.fetchall() if same_venue(r["name"], name)]
    if not found and email:
        cursor.execute("SELECT * FROM crm_leads WHERE LOWER(email) = LOWER(%s)", (email,))
        found = cursor.fetchall()
    city = (loc or "").split(",")[0].strip().lower()
    if not found and city:
        cursor.execute(
            "SELECT * FROM crm_leads "
            "WHERE split_part(LOWER(COALESCE(loc, '')), ',', 1) = %s", (city,))
        found = [r for r in cursor.fetchall() if same_venue(r["name"], name)]
    if not found:
        return None
    best = min(found, key=lambda r: (not (r["last_touch_at"] or r["status"] != "new"),
                                     r["created_at"] or ""))
    cursor.execute("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE", (best["id"],))
    return cursor.fetchone()


# The decorator must sit directly on quick_add_lead. #34 inserted
# _find_existing_lead between them, which registered THAT function as the
# route: every "Add a lead" came back 422 asking for query parameters.
# test_routes.py now checks every CRM route lands on the function it names.
@crm_router.post("/leads/quick-add", response_model=dict, status_code=201)
def quick_add_lead(data: QuickAdd, _: bool = Depends(require_crm_key)):
    """Add a lead from pasted notes — see _quick_add()."""
    return _quick_add(data.text, data.name)


def _quick_add(text: str, name_override: Optional[str] = None) -> dict:
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
    today = _today()
    extracted = _quick_add_extract(text, today)

    name = _clean(name_override, 200) or _clean(extracted.get("name"), 200) \
        or _name_from_text(text)
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
            import leadgen
            from leadgen import find_venue_website, find_email_on_site
            if not website:
                # Looked up, not given: it only counts if the map hit is this
                # bar in this town AND the site itself names the bar. A guess
                # gave NE Moose Bar & Grill another restaurant's email.
                website = find_venue_website(name, loc)
                if website and not leadgen.site_is_venue(website, name):
                    print(f"[crm] quick-add: {website} doesn't name {name!r}; not used",
                          flush=True)
                    website = None
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
        ("Address", _clean(extracted.get("address"))),
        ("Other phones", _clean(extracted.get("other_phones"))),
        ("Website", website),
        ("Email found on", found_via),
    ] if val]

    today = _today()
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        # A bar already in the book gets the call logged on ITS row — never a
        # second row for the same venue.
        # Every number in the paste, not just the one the model picked: notes
        # like "(720) 242-9667 or (303) 467-1472" name the bar either way.
        from leadgen import phones_in
        phones = sorted(phones_in(" ".join(filter(None, [
            str(extracted.get("phone") or ""), str(extracted.get("other_phones") or ""),
            text]))))
        lead = _find_existing_lead(cursor, name, loc, phones,
                                   _clean(extracted.get("email"), 320))
        matched = lead is not None
        if not matched:
            cursor.execute("""
                INSERT INTO crm_leads (id, name, loc, status, source, attempts, notes,
                                       created_at, updated_at)
                VALUES (%s, %s, %s, 'new', 'manual', 0, %s, %s, %s)
                RETURNING *
            """, (generate_id(), name, loc, "\n".join(details) or None, now, now))
            lead = cursor.fetchone()

        updated, applied, undo_id, counters = _apply_call_notes(
            cursor, lead, extracted, text, "call", today, now)
        if matched:
            # After the call's own note, and still under its undo: the
            # snapshot was taken before either.
            sets, params = [], []
            if details:
                sets.append("notes = COALESCE(notes || E'\\n', '') || %s")
                params.append(" · ".join(details))
            if loc and not updated.get("loc"):
                sets.append("loc = %s")
                params.append(loc)
            if sets:
                cursor.execute(f"UPDATE crm_leads SET {', '.join(sets)} WHERE id = %s "
                               "RETURNING *", params + [lead["id"]])
                updated = cursor.fetchone()
            applied["matched_existing"] = lead["name"]
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


# ── cold-call practice ────────────────────────────────────────────────────────
# The practice screen inside the Call list tab. Prompts and the referee live in
# coach.py (pure, tested); these routes only carry them to Claude and back.
# Nothing here reads or writes a lead: practice scores stay in the operator's
# browser, because they're about the caller, not the pipeline.

import coach as _coach


class CurveballRequest(BaseModel):
    level: Literal["warm", "busy", "hostile"] = "busy"


class GradeRequest(BaseModel):
    who: str = Field(max_length=120)
    line: str = Field(max_length=400)
    answer: str = Field(min_length=1, max_length=1500)
    seconds: int = Field(default=0, ge=0, le=600)
    timed_out: bool = False


class CoachLine(BaseModel):
    role: Literal["rep", "owner"]
    text: str = Field(max_length=1500)


class TurnRequest(BaseModel):
    boss: str = Field(max_length=20)
    challenge: str = Field(default="none", max_length=20)
    transcript: list[CoachLine] = Field(default_factory=list, max_length=200)
    said: str = Field(min_length=1, max_length=1500)
    patience: int = Field(ge=0, le=100)
    trust: int = Field(ge=0, le=100)
    found: list[str] = Field(default_factory=list, max_length=10)
    interrupt: Optional[str] = Field(default=None, max_length=160)


class ReviewRequest(BaseModel):
    boss: str = Field(max_length=20)
    transcript: list[CoachLine] = Field(max_length=200)
    result: Literal["won", "lost"]


def _known_boss(boss_id: str) -> None:
    if not _coach.get_boss(boss_id):
        raise HTTPException(status_code=422, detail={
            "error": "unknown_owner", "message": "That owner isn't in the game any more — pick another."})


class ScriptRequest(BaseModel):
    skill: Literal["opener", "discovery", "objections", "ask"]
    draft: str = Field(min_length=1, max_length=1000)


@crm_router.post("/coach/script", response_model=dict)
def coach_script(data: ScriptRequest, _: bool = Depends(require_crm_key)):
    system, user = _coach.script_prompt(data.skill, data.draft)
    out = _ask_claude(system, user, max_tokens=400)
    return {"score": _coach.clamp(out.get("score"), 0, 10), "keep": str(out.get("keep") or "")[:300],
            "change": str(out.get("change") or "")[:300], "tight": str(out.get("tight") or "")[:400]}


@crm_router.get("/coach/bosses", response_model=dict)
def coach_bosses(_: bool = Depends(require_crm_key)):
    """Who you can call. Names and openers only — the pains stay hidden."""
    return {"bosses": [
        {"id": k, "name": b["name"], "level": b["level"], "patience": b["patience"],
         "opening": b["opening"], "pains": len(b["pains"])}
        for k, b in sorted(_coach.BOSSES.items(), key=lambda kv: kv[1]["level"])],
        "guests": [{"id": k, "name": g["name"], "patience": g["patience"],
                    "opening": g["opening"]} for k, g in _coach.GUESTS.items()],
        "challenges": {k: {"label": c["label"], "patience": c["patience"]}
                       for k, c in _coach.CHALLENGES.items()},
        "win_trust": _coach.WIN_TRUST, "win_pains": _coach.WIN_PAINS,
        "ai": bool(os.getenv("ANTHROPIC_API_KEY"))}


_OBJECTION_IN_NOTES = re.compile(r"Objection: ([^·\n]{3,200})")


def _real_objections(limit: int = 12) -> list:
    """What prospects actually pushed back with lately: the "Objection:"
    detail logged calls now carry, then the playbook's "Objections we hear".
    Practice runs on these (coach.curveball_prompt)."""
    found: list = []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PLAYBOOK_DAYS)).isoformat()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT notes FROM crm_leads
                 WHERE last_touch_at >= %s AND notes LIKE '%%Objection: %%'
                 ORDER BY last_touch_at DESC LIMIT 60
            """, (cutoff,))
            for r in cursor.fetchall():
                for m in _OBJECTION_IN_NOTES.findall(r["notes"] or ""):
                    m = m.strip(" .")
                    if m and m not in found:
                        found.append(m)
        pb = _playbook_of(_brain_row()) or {}
        for sec in pb.get("sections") or []:
            if "objection" in sec.get("title", "").lower():
                found += [p["text"] for p in sec.get("points") or [] if p.get("text")]
    except Exception as exc:
        print(f"[crm] real objections unavailable: {exc}", flush=True)
    return found[:limit]


@crm_router.post("/coach/curveball", response_model=dict)
def coach_curveball(data: CurveballRequest, _: bool = Depends(require_crm_key)):
    system, user = _coach.curveball_prompt(data.level, _real_objections())
    out = _ask_claude(system, user, max_tokens=200, temperature=1, purpose="school")
    return {"who": str(out.get("who") or "Bar owner")[:120],
            "line": str(out.get("line") or "")[:400]}


@crm_router.post("/coach/grade", response_model=dict)
def coach_grade(data: GradeRequest, _: bool = Depends(require_crm_key)):
    system, user = _coach.grade_prompt(data.who, data.line, data.answer,
                                       data.seconds, data.timed_out)
    out = _ask_claude(system, user, max_tokens=400)
    skill = out.get("skill") if out.get("skill") in ("opener", "discovery", "objections", "ask") else None
    return {"score": _coach.clamp(out.get("score"), 0, 10), "skill": skill,
            "worked": str(out.get("worked") or "")[:300], "fix": str(out.get("fix") or "")[:300],
            "better": str(out.get("better") or "")[:400]}


@crm_router.post("/coach/turn", response_model=dict)
def coach_turn(data: TurnRequest, _: bool = Depends(require_crm_key)):
    _known_boss(data.boss)
    system, user = _coach.turn_prompt(
        data.boss, [t.model_dump() for t in data.transcript], data.said,
        data.patience, data.trust, data.found, data.interrupt, data.challenge)
    out = _ask_claude(system, user, max_tokens=400, temperature=0.8, purpose="school")
    return _coach.apply_turn(data.boss, data.patience, data.trust, data.found, out)


@crm_router.post("/coach/review", response_model=dict)
def coach_review(data: ReviewRequest, _: bool = Depends(require_crm_key)):
    _known_boss(data.boss)
    system, user = _coach.review_prompt(data.boss, [t.model_dump() for t in data.transcript],
                                        data.result)
    out = _ask_claude(system, user, max_tokens=700, purpose="school")
    scores = {k: _coach.clamp(out.get(k), 0, 10) for k in ("opener", "discovery", "objections", "ask")}
    return {**scores, "turning_point": str(out.get("turning_point") or "")[:400],
            "redo": str(out.get("redo") or "")[:400]}


FILM_DAYS = 14
FILM_CALLS = 15


def _local_day(at: Optional[str]) -> str:
    """"Tue Sep 23" in the operator's own clock, for the coach's call list."""
    t = _parse_utc(at)
    return t.astimezone(_operator_tz()).strftime("%a %b %d") if t else ""


@crm_router.post("/coach/film", response_model=dict)
def coach_film(_: bool = Depends(require_crm_key)):
    """Game film: the founder's own recent conversations, read back as
    coaching — one thing working, the pattern costing the most, and drills
    built from what prospects actually said (the page files them into
    Replay). Voicemails and ring-outs aren't film; nothing happened on them."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=FILM_DAYS)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT ON (t.lead_id) t.lead_id, t.at, t.outcome, l.name, l.notes
              FROM crm_touches t JOIN crm_leads l ON l.id = t.lead_id
             WHERE t.kind = 'call' AND t.at >= %s
               AND t.outcome IN ('answered', 'callback', 'not_interested', 'gatekeeper')
             ORDER BY t.lead_id, t.at DESC
        """, (cutoff,))
        rows = sorted(cursor.fetchall(), key=lambda r: r["at"] or "", reverse=True)[:FILM_CALLS]
    if not rows:
        return {"working": None, "costing": None, "drills": [], "calls": 0,
                "note": f"No real conversations logged in the last {FILM_DAYS} days yet — "
                        "voicemails and missed calls don't count. Make a few calls first."}
    calls = [{"bar": r["name"], "when": _local_day(r["at"]), "outcome": r["outcome"],
              "notes": " / ".join(_lead_history(dict(r), lines=2).splitlines())} for r in rows]
    system, user = _coach.film_prompt(calls)
    out = _ask_claude(system, user, max_tokens=1200, purpose="school")
    return {**_coach.validate_film(out, [r["name"] for r in rows]), "calls": len(rows)}


class TapeRequest(BaseModel):
    subtle: bool = False


@crm_router.post("/coach/tape", response_model=dict)
def coach_tape(data: TapeRequest, _: bool = Depends(require_crm_key)):
    """A call with three planted mistakes. Asked twice at most: a tape the game
    can't score fairly is worse than no tape."""
    system, user = _coach.tape_prompt(data.subtle)
    for _attempt in range(2):
        tape = _coach.validate_tape(_ask_claude(system, user, max_tokens=1800, temperature=1))
        if tape:
            return tape
    raise HTTPException(status_code=503, detail={
        "error": "tape_unusable", "message": "Couldn't make a fair tape. Here's a built-in one."})


@crm_router.get("/coach/school", response_model=dict)
def coach_school(_: bool = Depends(require_crm_key)):
    """The latest refreshed school pack, or {pack: None} before the first one."""
    import school
    return school.latest_pack()


@crm_router.post("/coach/school/refresh", response_model=dict)
def coach_school_refresh(_: bool = Depends(require_crm_key)):
    """Run a refresh now instead of waiting for 10am. Returns at once; the pack
    lands a few minutes later and the page picks it up on its next load."""
    import school
    threading.Thread(target=school.refresh_if_due, kwargs={"force": True}, daemon=True).start()
    return {"started": True}
