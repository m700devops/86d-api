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

import os
import secrets
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
        ]:
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'crm_leads' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE crm_leads ADD COLUMN {col} {col_type}")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_leads_status ON crm_leads(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_leads_followup ON crm_leads(followup_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_crm_leads_created ON crm_leads(created_at)")

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
    "source", "last_touch_at",
)

# Columns a PATCH is allowed to write. `id`, `created_at` and `updated_at` are
# not in here on purpose — an allowlist beats filtering a denylist when the
# values are being interpolated into a SQL fragment.
LEAD_WRITABLE = (
    "name", "loc", "status", "contact", "phone", "email",
    "call_date", "email_date", "followup_date", "notes",
)


import re as _re

def phone_digits(phone: Optional[str]) -> str:
    """Bare digits, ready to paste into a dialer.

    CloudTalk and every other dialer want a number, not a formatted string, so
    "+1-615-742-9095" becomes "6157429095". A leading US country code is
    dropped because 10 digits is what a US dialer expects; anything that isn't
    an 11-digit US number is left at its full digit string rather than guessed at.
    """
    digits = _re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


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
    return {"offset": offset, "label": label, "local_time": local.strftime("%-I:%M %p"),
            "state": state, "headline": headline, "rank": rank, "callable": ok}


def _lead_row(row) -> dict:
    lead = {k: row[k] for k in LEAD_COLUMNS}
    lead["phone_digits"] = phone_digits(lead.get("phone"))
    return lead


def _blank_to_none(value):
    """An empty string from the page means "clear this", not "store ''"."""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


# ============== LEADS ==============

@crm_router.get("/leads", response_model=dict)
def list_leads(status: Optional[str] = None, _: bool = Depends(require_crm_key)):
    """Every lead, newest first. Optional ?status= filter."""
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_status",
            "message": f"status must be one of {', '.join(VALID_STATUSES)}",
        })
    with get_db() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute(
                "SELECT * FROM crm_leads WHERE status = %s ORDER BY created_at DESC", (status,)
            )
        else:
            cursor.execute("SELECT * FROM crm_leads ORDER BY created_at DESC")
        leads = [_lead_row(row) for row in cursor.fetchall()]
        return {"leads": leads, "count": len(leads)}


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


# ============== TODAY'S CALL QUEUE ==============

# Bars are shut in the morning and slammed at night. Early afternoon is when
# somebody who can make a decision is there and not busy.
CALL_WINDOW_START = 14   # 2pm local
CALL_WINDOW_END = 17     # 5pm local


def _call_window(tz_offset: Optional[int]) -> dict:
    """Is now a sane time to ring this venue?"""
    if tz_offset is None:
        return {"known": False, "good_now": True, "local_time": None, "hint": ""}
    local = datetime.now(timezone.utc) + timedelta(hours=tz_offset)
    hour = local.hour
    good = CALL_WINDOW_START <= hour < CALL_WINDOW_END
    if hour < 11:
        hint = "too early — most bars aren't staffed yet"
    elif hour < CALL_WINDOW_START:
        hint = f"opens up around {CALL_WINDOW_START}:00 local"
    elif good:
        hint = "good time to call"
    elif hour < 21:
        hint = "service is starting — expect a brush-off"
    else:
        hint = "too late — they're busy"
    return {"known": True, "good_now": good,
            "local_time": local.strftime("%H:%M"), "hint": hint}


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
                lead["call_window"] = _call_window(row.get("tz_offset_hours"))
                out.append(lead)
            return out

        overdue = fetch(
            "followup_date IS NOT NULL AND followup_date < %s AND status NOT IN ('won','dead')",
            (today,), "followup_date ASC")
        due_today = fetch(
            "followup_date = %s AND status NOT IN ('won','dead')",
            (today,), "updated_at ASC")
        never_called = fetch(
            "call_date IS NULL AND status = 'new'", (), "created_at ASC")

        return {
            "as_of": today,
            "overdue": overdue,
            "due_today": due_today,
            "never_called": never_called,
            "counts": {
                "overdue": len(overdue),
                "due_today": len(due_today),
                "never_called": len(never_called),
            },
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

        sets = ["updated_at = %s", "last_touch_at = %s"]
        params: list = [now, now]

        if data.kind == "call":
            sets.append("call_date = %s"); params.append(today)
        elif data.kind == "email" and not lead["email_date"]:
            sets.append("email_date = %s"); params.append(today)

        if data.followup_in_days is not None:
            follow = (datetime.now(_reset_tz()) + timedelta(days=data.followup_in_days)).strftime("%Y-%m-%d")
            sets.append("followup_date = %s"); params.append(follow)

        new_status = data.status
        if new_status is None and data.outcome:
            # A call that reached a human has, at minimum, contacted them.
            new_status = {"not_interested": "dead", "callback": "warm",
                          "answered": "contacted", "voicemail": "contacted",
                          "gatekeeper": "contacted"}.get(data.outcome)
        if new_status and lead["status"] == "new":
            sets.append("status = %s"); params.append(new_status)
        elif new_status and new_status in ("warm", "won", "dead"):
            sets.append("status = %s"); params.append(new_status)

        if data.note:
            stamped = f"[{today}] {data.kind}"
            if data.outcome:
                stamped += f" · {data.outcome}"
            stamped += f": {data.note}"
            sets.append("notes = COALESCE(notes || E'\\n', '') || %s")
            params.append(stamped)

        params.append(lead_id)
        cursor.execute(f"UPDATE crm_leads SET {', '.join(sets)} WHERE id = %s RETURNING *", params)
        updated = cursor.fetchone()

        # Same transaction as the lead update: the scoreboard and the pipeline
        # can't drift apart if they move together.
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
        return {"lead": _lead_row(updated),
                "counters": _counters_row(counters) if counters else None}


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
        if data.kind in ("email", "phone"):
            cursor.execute(
                f"UPDATE crm_leads SET status='dead', updated_at=%s WHERE LOWER({data.kind}) = LOWER(%s)",
                (now_iso(), data.value.strip()))
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
            lead["call_window"] = _call_window(row.get("tz_offset_hours"))
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
    from leadgen import pool_depth, DAILY_TARGET
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
    if stale:
        warnings.append("No successful run in the last 36 hours — the daily list has stopped.")
    if depth["qualified"] < DAILY_TARGET:
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

    return {
        "healthy": not stale and not warnings,
        "stale": stale,
        "hours_since_last_success": hours_since,
        "pool": depth,
        "cities": {"total": cities["total"], "unharvested": cities["unharvested"]},
        "warnings": warnings,
        "recent_runs": runs,
    }


@crm_router.post("/leadgen/run", response_model=dict)
def leadgen_run(target: Optional[int] = None, max_cities: int = 4,
                max_enrich: int = 120, _: bool = Depends(require_crm_key)):
    """Run the pipeline now. Same path the daily scheduler takes."""
    from leadgen import run_daily, DAILY_TARGET
    return run_daily(target=target or DAILY_TARGET, max_cities=max_cities,
                     max_enrich=max_enrich)


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
        window = _call_window(row.get("tz_offset_hours"))
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
    today = _today()
    with get_db() as conn:
        cursor = conn.cursor()

        # Untouched and still 'new'. Any CRM update moves one of those two and
        # the lead drops off.
        cursor.execute("""
            SELECT * FROM crm_leads
             WHERE status = 'new' AND last_touch_at IS NULL
               AND phone IS NOT NULL AND phone <> ''
             ORDER BY created_at DESC
        """)
        rows = cursor.fetchall()

        cursor.execute("""
            SELECT COUNT(*) AS n FROM crm_leads
             WHERE SUBSTRING(COALESCE(last_touch_at, ''), 1, 10) = %s
        """, (today,))
        done_today = cursor.fetchone()["n"]

    grouped: dict = {}
    for row in rows:
        lead = _lead_row(row)
        offset = row.get("tz_offset_hours")
        grouped.setdefault(offset, []).append(lead)

    zones = []
    for offset, leads in grouped.items():
        zone = zone_state(offset)
        zone["leads"] = leads
        zone["count"] = len(leads)
        zones.append(zone)
    # Callable-now first, then the ones opening up, then the rest.
    zones.sort(key=lambda z: (z["rank"], z["label"]))

    callable_now = [z for z in zones if z["state"] == "good"]
    if callable_now:
        focus = (f"Call {callable_now[0]['label']} now — "
                 f"{callable_now[0]['count']} waiting")
    else:
        soon = [z for z in zones if z["state"] in ("opening", "closed")]
        focus = (f"Nothing in the sweet spot. {soon[0]['label']} is next."
                 if soon else "Nothing to call right now.")

    return {
        "date": today,
        "focus": focus,
        "done_today": done_today,
        "remaining": len(rows),
        "zones": zones,
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
  "contact": the name of the person they spoke to
  "email": a corrected or newly learned email address
  "phone": a corrected or newly learned phone number
  "followup_in_days": integer number of days until the agreed follow-up
  "summary": one clean sentence recording what happened

Rules:
- "not interested", "hung up", "don't call again", "no thanks" -> status "dead"
- an agreed callback, a demo booked, real interest -> status "warm"
- reached someone but no clear outcome -> status "contacted"
- signed up, bought, installed -> status "won"
- voicemail or gatekeeper with nobody reached -> status "contacted"
- "next week" is 7 days, "tomorrow" is 1, "Monday" is 3 unless told otherwise
"""


def _debrief_extract(text: str) -> dict:
    """Ask the model for structured fields. Raises HTTPException when unusable.

    Uses the same providers the scan path already uses — OpenAI first, Gemini
    as the fallback — so this needs no new key and no new vendor.
    """
    import json as _json
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    if openai_key:
        try:
            import openai as _openai
            client = _openai.OpenAI(api_key=openai_key, timeout=25.0)
            resp = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o"),
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": DEBRIEF_SYSTEM},
                          {"role": "user", "content": text}],
                temperature=0,
            )
            return _json.loads(resp.choices[0].message.content)
        except Exception as exc:
            print(f"[crm] debrief via OpenAI failed: {exc}", flush=True)

    if gemini_key:
        try:
            import google.generativeai as _genai
            _genai.configure(api_key=gemini_key)
            model = _genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
            resp = model.generate_content(
                f"{DEBRIEF_SYSTEM}\n\nNotes:\n{text}",
                generation_config={"response_mime_type": "application/json",
                                   "temperature": 0},
            )
            return _json.loads(resp.text)
        except Exception as exc:
            print(f"[crm] debrief via Gemini failed: {exc}", flush=True)

    raise HTTPException(status_code=503, detail={
        "error": "ai_unavailable",
        "message": "Couldn't reach the AI to read those notes — type the fields in by hand.",
    })


@crm_router.post("/leads/{lead_id}/debrief", response_model=dict)
def debrief_lead(lead_id: str, data: Debrief, _: bool = Depends(require_crm_key)):
    """Free-text notes in, updated lead out.

    Everything it decides is echoed back in `applied` so a wrong reading is
    visible immediately rather than silently rewriting the pipeline.
    """
    extracted = _debrief_extract(data.text)

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

    today = _today()
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crm_leads WHERE id = %s FOR UPDATE", (lead_id,))
        lead = cursor.fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail={
                "error": "not_found", "message": "Lead not found"})

        sets = ["updated_at = %s", "last_touch_at = %s"]
        params: list = [now, now]
        applied: dict = {}

        if data.kind == "call":
            sets.append("call_date = %s"); params.append(today); applied["call_date"] = today
        elif data.kind == "email" and not lead["email_date"]:
            sets.append("email_date = %s"); params.append(today); applied["email_date"] = today

        if status:
            sets.append("status = %s"); params.append(status); applied["status"] = status
        for field in ("contact", "email", "phone"):
            value = extracted.get(field)
            if isinstance(value, str) and value.strip():
                sets.append(f"{field} = %s"); params.append(value.strip())
                applied[field] = value.strip()
        if followup is not None:
            when = (datetime.now(_reset_tz()) + timedelta(days=followup)).strftime("%Y-%m-%d")
            sets.append("followup_date = %s"); params.append(when)
            applied["followup_date"] = when

        summary = extracted.get("summary") or data.text.strip()
        note = f"[{today}] {data.kind}: {summary}"
        sets.append("notes = COALESCE(notes || E'\\n', '') || %s"); params.append(note)
        applied["note"] = note

        params.append(lead_id)
        cursor.execute(f"UPDATE crm_leads SET {', '.join(sets)} WHERE id = %s RETURNING *", params)
        updated = cursor.fetchone()

        counter_col = {"call": "daily_calls_remaining", "email": "daily_emails_remaining",
                       "fb": "daily_fb_remaining"}[data.kind]
        cursor.execute(f"""
            UPDATE crm_counters
               SET {counter_col} = GREATEST(0, {counter_col} - 1),
                   touch_ticker_remaining = GREATEST(0, touch_ticker_remaining - 1),
                   touch_ticker_last_action = %s, updated_at = %s
             WHERE id = 1 RETURNING *
        """, (data.kind, now))
        counters = cursor.fetchone()
        conn.commit()

    return {"lead": _lead_row(updated), "applied": applied,
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
