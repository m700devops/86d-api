"""Daily lead generator — independent bars that pour liquor.

The pipeline is four stages, deliberately separated so a failure in one never
starves the next:

  harvest  → OpenStreetMap, via Overpass, for bars/pubs/nightclubs in a city
  enrich   → fetch the venue's own website and find a real email on it
  qualify  → drop chains, drop anything without phone AND email, score the rest
  promote  → move the top N of the qualified pool into crm_leads each morning

The pool between qualify and promote is the whole reason this survives a bad
day. Harvesting runs ahead of consumption and banks candidates, so an Overpass
outage, a slow crawl or a city that turns out to be a dud costs nothing that
morning — promote just draws from the bank. "No failure points" isn't literally
achievable against three external systems, but this comes at it from the two
directions that matter: never fail *silently* (every run is recorded, and the
CRM shows days of runway left), and never emit a lead that doesn't meet the bar
(a row without a phone and a findable email is never promoted, ever).

Why OpenStreetMap rather than Google Places or Yelp: those two forbid storing
their place content beyond a short cache window, which is exactly what a
persistent lead pool does. OSM is ODbL — free to keep, with attribution — so
the pool is legal to hold indefinitely. It also needs no API key and no billing
account, which removes the most boring failure point of all: an expired card
silently stopping the pipeline.
"""

import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from database import get_db
from helpers import generate_id, now_iso
from callwindow import ZONE_OFFSETS, SERVICES, bucket_of, all_buckets
import venue as venue_facts
from contacts import email_kind, find_manager, strip_non_content, visible_text
from phones import normalize_us_phone, is_toll_free, format_us_phone_dashed

# ── Tunables ────────────────────────────────────────────────────────────────

DAILY_TARGET = int(os.getenv("LEADGEN_DAILY_TARGET", "25"))

# The call list is divided into cells: one per (service × timezone), i.e.
# lunch/dinner crossed with Eastern/Central/Mountain/Pacific. Eight of them.
#
# This number is the ceiling on unworked leads IN EACH CELL, not across the
# list. That distinction is the whole design: a single global cap of 100 spread
# over eight cells averages twelve per tab, so clicking "lunch → Eastern" shows
# a nearly empty screen and there is nothing to call. The cap exists so no ONE
# screen is overwhelming, and the per-screen number is what actually controls
# that.
#
# 50 a cell means about two days of calling visible in whichever tab is open,
# and up to 400 banked across all eight — of which the operator ever sees one
# cell at a time.
BUCKET_TARGET = int(os.getenv("LEADGEN_BUCKET_TARGET", "50"))

# Derived, for reporting and for the "is the whole thing full?" shortcut.
# Deliberately not an independent knob: a global number that disagreed with the
# per-cell one would starve some tabs to fill others.
MAX_ACTIVE = BUCKET_TARGET * len(SERVICES) * len(ZONE_OFFSETS)

# Qualified candidates kept banked behind the call list. Much smaller than the
# list itself: with eight capped cells holding days of work, the list is its own
# buffer and a deep second bank would just be crawling nobody asked for.
POOL_FLOOR = int(os.getenv("LEADGEN_POOL_FLOOR", "50"))
USER_AGENT = "86d-leadgen/1.0 (+https://my86d.com; bar inventory software)"

# Several mirrors because one of them is always having a bad day. Tried in
# order; a run only fails if every mirror fails.
#
# Every entry here must carry the FULL PLANET. overpass.osm.ch was in this list
# and had to come out: it's a regional mirror holding only European data, so a
# US query gets HTTP 200 and an empty element list — a confident, valid-looking
# "there are no bars in Austin". That is far more dangerous than an outage,
# because it looks like success: the city gets marked harvested, the pool never
# fills, and nothing anywhere reports an error. Don't add a regional mirror.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    # VK's public instance: full planet (answered a US query on 2026-09-24
    # while the main mirror was refusing and the other two timed out).
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
NOMINATIM = "https://nominatim.openstreetmap.org/search"

# Guessed paths, tried only after the homepage has been read and its own links
# followed. Kept short on purpose: each miss costs a round trip, and measured
# against real bar sites almost every address that exists at all turns up on
# the homepage, on a page the homepage links to, or at /contact.
CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/private-events"]
# Only tried when an email is already in hand and there's budget left — a name
# is a bonus, never worth spending the requests that find the email.
TEAM_PATHS = ["/about-us", "/our-team", "/team", "/about", "/staff"]

# Anchor text or href that suggests a page carrying an address.
CONTACT_LINK_RE = re.compile(
    r'href=["\']([^"\']{1,200})["\'][^>]{0,500}>(?:[^<]{0,80})'
    r'(contact|about|info|reach us|get in touch|private|events|book)',
    re.I,
)

PAGE_TIMEOUT = 12          # per request; a bar site that's slower isn't worth it
MAX_PAGES_PER_SITE = 6     # hard ceiling so one bad domain can't eat a run
ENRICH_WORKERS = int(os.getenv("LEADGEN_ENRICH_WORKERS", "8"))

# Every repeat bounded, and the local part may only START where a run of
# address characters starts (the lookbehind). Unbounded, a page with a long
# run of letters and no '@' — a minified blob, an inline SVG path — was
# re-scanned from every character in it: quadratic, holding the GIL, which
# on one 800KB page is minutes of a server answering nothing.
_LOCAL = r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}"
EMAIL_RE = re.compile(_LOCAL + r"@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}")
MAILTO_RE = re.compile(
    r'mailto:\s{0,5}([A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24})', re.I)
# Obfuscated forms — "info [at] barname [dot] com" and friends.
OBFUSCATED_RE = re.compile(
    r"(" + _LOCAL + r")\s{0,3}(?:\[at\]|\(at\)|\s{1,3}at\s{1,3})\s{0,3}([A-Za-z0-9.\-]{1,190}?)"
    r"\s{0,3}(?:\[dot\]|\(dot\)|\s{1,3}dot\s{1,3})\s{0,3}([A-Za-z]{2,24})",
    re.I,
)

# Addresses that are never a bar owner: platform noise, tracking, stock images.
#
# The @domain entries matter as much as the rest. Site builders leave their own
# addresses in template markup — a real harvest produced "wixofday@wix.com" as
# the contact for a Portland bar, which is a perfectly valid-looking email that
# reaches Wix's marketing team and never the venue. A lead nobody can reply to
# is worse than no lead, because it still costs a call slot and a follow-up.
# What makes an address worth having lives in contacts.py, so it can be
# tested without a database — this module can't be imported without one.
from contacts import EMAIL_BLOCKLIST, PLATFORM_DOMAINS  # noqa: F401

# Chains and franchises. A bar that belongs to one of these has a corporate
# inventory system and a district manager; it is not a prospect.
CHAIN_NAMES = {
    "applebee", "chili", "tgi friday", "fridays", "buffalo wild wings", "hooters",
    "outback", "olive garden", "texas roadhouse", "cheesecake factory", "yard house",
    "bj's restaurant", "dave & buster", "dave and buster", "twin peaks",
    "miller's ale house", "millers ale house", "red robin", "ruby tuesday",
    "chuy's", "chuys", "cracker barrel", "ihop", "denny", "hard rock cafe",
    "planet hollywood", "margaritaville", "tilted kilt", "wingstop", "hooter",
    "old chicago", "rock bottom", "gordon biersch", "chevys", "pf chang",
    "carrabba", "bonefish grill", "longhorn steakhouse", "logan's roadhouse",
    "beef o brady", "beef 'o' brady", "world of beer", "mellow mushroom",
    "buffalo wings", "wing house", "winghouse", "quaker steak", "fox and hound",
    "fox & hound", "bar louie", "granite city", "mcfadden",
    "walk-on", "walk on's", "walkons", "pluckers", "torchy",
    "punch bowl social", "lucky strike", "main event", "topgolf", "chuck e",
    "golden corral", "red lobster", "on the border", "uno pizzeria",
    "famous dave", "smokey bones", "hurricane grill", "duffy's sports",
    "tijuana flats", "first watch", "another broken egg", "marriott", "hilton",
    "hyatt", "sheraton", "doubletree", "embassy suites", "holiday inn",
    "courtyard by", "residence inn", 
}

# Words on a site that mean "this is one of many" — franchise language.
CHAIN_SITE_HINTS = re.compile(
    r"(find a location|find your (nearest|local)|store locator|restaurant locator|"
    r"\bfranchis(e|ing|ee)|nearest location|locations near you|corporate office|"
    r"become an? (owner|franchisee))",
    re.I,
)

# Explicit signals a venue does NOT pour, checked before anything else: a site
# saying this outright beats any inference from a keyword match, and BYOB
# specifically means there is no liquor license to sell against at all.
NO_LIQUOR_HINTS = re.compile(
    r"(\bbyob\b|bring your own (bottle|beer|wine)|no alcohol (is )?served|"
    r"non-alcoholic (restaurant|establishment)|we do not serve alcohol|"
    r"does not serve alcohol|\bdry\b (restaurant|county)|"
    r"no liquor license|not licensed (to serve|for alcohol))",
    re.I,
)


def _restaurant_pours(html_seen: str, tags: dict) -> tuple[bool, Optional[str]]:
    """(pours liquor?, why not) for a restaurant, from its raw pages — the
    liquor_verdict() rule. Kept for recheck_restaurant_leads()."""
    v = liquor_verdict(visible_text(html_seen or ""), tags, (tags or {}).get("name") or "",
                       "restaurant")
    return (True, None) if v["status"] == "spirits" else (False, v["reason"])


# ── Small HTTP helper ───────────────────────────────────────────────────────
# curl rather than httpx on purpose: it follows redirects, caps body size and
# hard-stops on a timeout without any of them being able to hang a worker
# thread, and it is already present everywhere this runs.

def _is_public_http_url(url: str) -> bool:
    """Reject anything that isn't a plain http(s) URL resolving to a public IP.

    This matters because the crawler follows a `website` tag out of
    OpenStreetMap, which ANYONE can edit. Without this check, someone could
    point a bar's website at http://169.254.169.254/ and have this server
    fetch its own cloud metadata, or sweep Render's private network, simply by
    editing a map. The pool is internal and the body is never returned to a
    caller, but "our server will fetch any URL a stranger writes down" is not a
    property worth having.

    A determined attacker could still beat this with DNS rebinding — the name
    resolves public here and private when curl resolves it again. Closing that
    properly means pinning the resolved address into the request, which is more
    machinery than an internal lead tool warrants; this stops the whole class of
    casual abuse, which is the realistic threat.
    """
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


# curl exit codes that mean the TLS handshake or certificate failed — the site
# may be fine in a browser (which fetches missing intermediate certificates),
# so _fetch_site tries again rather than calling the site dead.
_TLS_EXITS = {35, 51, 53, 54, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91}


def _http(url: str, timeout: int = 20, data: Optional[str] = None,
          verify_public: bool = False, insecure: bool = False,
          tls_status: bool = False) -> tuple[str, int]:
    """Returns (body, status). Never raises — a failed fetch is ('', 0).

    `verify_public` is set for anything crawled from map data; the fixed
    Overpass and Nominatim endpoints skip the extra DNS round trip.
    """
    if verify_public and not _is_public_http_url(url):
        print(f"[leadgen] refusing non-public URL: {url[:120]}", flush=True)
        return "", 0
    cmd = [
        "curl", "-sSL", "--compressed",
        "--max-time", str(timeout),
        "--connect-timeout", "10",
        "--max-filesize", "800000",
        "-A", USER_AGENT,
        "-w", "\n__STATUS__%{http_code}",
    ]
    if data is not None:
        cmd += ["-X", "POST", "--data-urlencode", f"data={data}"]
    if insecure:
        # Only ever for READING a venue's public pages after a certificate
        # error — see _fetch_site. Never for anything sent or signed in.
        cmd.append("-k")
    cmd.append(url)
    try:
        # +5, not more: curl already enforces --max-time, and this outer guard
        # exists only for the case where curl itself wedges. A generous margin
        # here multiplies across every page of every candidate.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except Exception as exc:
        print(f"[leadgen] fetch failed {url}: {exc}", flush=True)
        return "", 0
    out = proc.stdout or ""
    if "__STATUS__" not in out:
        # curl never got an HTTP response at all. Without its stderr the log
        # reads "-> 0", which is indistinguishable between DNS failure, a TLS
        # problem, a timeout and a reset mid-transfer — and those want
        # completely different fixes. Say which.
        why = (proc.stderr or "").strip().replace("\n", " ")[:160]
        print(f"[leadgen] no HTTP response from {url[:90]} "
              f"(curl exit {proc.returncode}{': ' + why if why else ''})", flush=True)
        if tls_status and proc.returncode in _TLS_EXITS:
            return out, -1
        return out, 0
    body, _, status = out.rpartition("__STATUS__")
    try:
        return body, int(status.strip())
    except ValueError:
        return body, 0


# ── Schema ──────────────────────────────────────────────────────────────────

def init_leadgen_tables():
    with get_db() as conn:
        cursor = conn.cursor()

        # The bank. Everything ever seen lands here, including rejects — a
        # rejected candidate has to be remembered or every harvest re-crawls
        # the same dead sites forever.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_lead_candidates (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'osm',
                source_ref TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                city TEXT, state TEXT,
                lat DOUBLE PRECISION, lon DOUBLE PRECISION,
                phone TEXT, website TEXT, email TEXT, email_source TEXT,
                amenity TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'new',
                reject_reason TEXT,
                tz_offset_hours INTEGER,
                raw_tags TEXT,
                opening_hours TEXT,
                opener TEXT,
                discovered_at TEXT NOT NULL,
                enriched_at TEXT,
                promoted_at TEXT,
                promoted_lead_id TEXT
            )
        """)
        # Columns added after the table first shipped. CREATE TABLE IF NOT
        # EXISTS silently skips them on any database that already has the
        # table, so they need the same information_schema gate the rest of the
        # app uses — this is exactly the bug that made opening_hours invisible
        # on an existing database while working fine on a fresh one.
        for col, col_type in [("opening_hours", "TEXT"), ("opener", "TEXT"),
                              ("email_kind", "TEXT"), ("tz_name", "TEXT"),
                              ("venue_facts", "TEXT"),
                              ("manager_name", "TEXT"), ("manager_role", "TEXT"),
                              ("manager_source", "TEXT"), ("manager_seen_at", "TEXT"),
                              # Whether the venue's own site vouches for the
                              # number: see judge_phone().
                              ("phone_status", "TEXT"), ("phone_note", "TEXT"),
                              # A site that didn't load is tried again on later
                              # runs (up to ENRICH_TRIES) instead of rejected.
                              ("enrich_attempts", "INTEGER"), ("retry_after", "TEXT"),
                              # The owner's rules (liquor, strip, chain): 'ok'
                              # or 'blocked', and the evidence / reason.
                              ("fit_status", "TEXT"), ("fit_note", "TEXT")]:
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'crm_lead_candidates' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE crm_lead_candidates ADD COLUMN {col} {col_type}")
                print(f"[leadgen] migrated crm_lead_candidates: added {col}", flush=True)

        for idx, col in [
            ("idx_cand_status", "status"),
            ("idx_cand_score", "score"),
            ("idx_cand_city", "city"),
        ]:
            cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON crm_lead_candidates({col})")
        # Two venues can share a phone (same owner, two rooms) but a duplicate
        # email means the same inbox gets pitched twice.
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cand_email "
            "ON crm_lead_candidates(LOWER(email)) WHERE email IS NOT NULL"
        )

        # The territory queue.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_leadgen_cities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                state TEXT,
                lat DOUBLE PRECISION, lon DOUBLE PRECISION,
                radius_m INTEGER NOT NULL DEFAULT 12000,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                last_harvested_at TEXT,
                harvest_runs INTEGER NOT NULL DEFAULT 0,
                candidates_found INTEGER NOT NULL DEFAULT 0,
                qualified_found INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        # When a city's harvest last failed on every mirror: it rests a day
        # rather than taking the first slot of every run while it keeps failing.
        cursor.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'crm_leadgen_cities' AND column_name = 'harvest_failed_at'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE crm_leadgen_cities ADD COLUMN harvest_failed_at TEXT")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_city_name "
            "ON crm_leadgen_cities(LOWER(name), LOWER(COALESCE(state, '')))"
        )

        # Every run, good or bad. This is what makes a silent failure loud.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_leadgen_runs (
                id TEXT PRIMARY KEY,
                phase TEXT NOT NULL,
                ok BOOLEAN NOT NULL DEFAULT FALSE,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                cities_harvested INTEGER NOT NULL DEFAULT 0,
                candidates_found INTEGER NOT NULL DEFAULT 0,
                enriched INTEGER NOT NULL DEFAULT 0,
                qualified INTEGER NOT NULL DEFAULT 0,
                promoted INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                detail TEXT
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_started ON crm_leadgen_runs(started_at DESC)"
        )

        # Never pitch these again: a "do not call", a competitor, a bad fit.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crm_suppressions (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_suppress "
            "ON crm_suppressions(kind, LOWER(value))"
        )

        conn.commit()

        # Seed on EVERY boot, not only into an empty table. The insert is
        # ON CONFLICT DO NOTHING against a unique index on (name, state), so
        # re-running is free — and gating it on "no cities yet" meant adding
        # forty metros to the list did nothing whatsoever to a database that
        # already had the first fifty-eight. Territory running out is a silent
        # failure; territory that silently never arrives is worse.
        _seed_cities(cursor)
        conn.commit()
        cursor.execute("SELECT COUNT(*) AS n FROM crm_leadgen_cities")
        city_count = cursor.fetchone()["n"]

        moved = _reconcile_timezones(cursor)
        bad_cands, bad_leads = _reconcile_bad_emails(cursor)
        conn.commit()
        # In its own transaction: it deletes rows, and a failure here must cost
        # only the de-duplication, never the boot or the fixes above.
        try:
            folded = _reconcile_duplicate_leads(cursor)
            conn.commit()
            if folded:
                print(f"[leadgen] LEADGEN_DEDUPED folded {folded} never-called duplicate "
                      f"lead(s) into the lead already in play", flush=True)
        except Exception as exc:
            conn.rollback()
            print(f"[leadgen] LEADGEN_DEDUPE_FAILED {exc}", flush=True)
        briefed = _backfill_venue_facts(cursor)
        if briefed:
            print(f"[leadgen] built call facts for {briefed} existing leads", flush=True)
        if bad_cands or bad_leads:
            print(f"[leadgen] cleared unusable emails: {bad_cands} candidates, "
                  f"{bad_leads} leads", flush=True)
        conn.commit()

        try:
            _rescore_map_penalties_once(cursor)
            conn.commit()
        except Exception as exc:
            # Rolls the marker back with the updates, so the next boot retries.
            conn.rollback()
            print(f"[leadgen] LEADGEN_RESCORE_FAILED {exc}", flush=True)

        # The owner's rules (tourist strips, chains) against what's already on
        # the list and in the bank — map data only, no crawling.
        try:
            removed, rejected = _reconcile_owner_rules(cursor)
            conn.commit()
            if removed or rejected:
                print(f"[leadgen] LEADGEN_OWNER_RULES removed_leads={removed} "
                      f"rejected_candidates={rejected}", flush=True)
        except Exception as exc:
            conn.rollback()
            print(f"[leadgen] LEADGEN_OWNER_RULES_FAILED {exc}", flush=True)

        try:
            reopened = _requalify_once(cursor)
            conn.commit()
            if reopened:
                print(f"[leadgen] LEADGEN_REQUALIFY reopened={reopened}", flush=True)
        except Exception as exc:
            conn.rollback()
            print(f"[leadgen] LEADGEN_REQUALIFY_FAILED {exc}", flush=True)

        # Numbers that predate the website check are NOT checked here. Doing
        # it at boot — every unchecked number at once, 8 crawls in parallel —
        # is what took the server down: a 0.5-CPU instance spent on crawling
        # can't answer anything else, and a restart started it all over
        # again. main.py's _phone_check_loop trickles through them instead.

        print(f"[leadgen] LEADGEN_TABLES_READY cities={city_count} "
              f"bucket_target={BUCKET_TARGET} max_active={MAX_ACTIVE} "
              f"daily_target={DAILY_TARGET} pool_floor={POOL_FLOOR}"
              + (f" retimezoned={moved}" if moved else ""), flush=True)


# When the Asian-cuisine and tourist-strip penalties went live (PR #29's
# merge). Anything enriched after this was scored with them already, and must
# not be charged twice.
MAP_PENALTY_CUTOFF = "2026-09-24T06:41:08+00:00"


# Rejections the September 2026 fixes overturn (see enrich_candidate,
# looks_like_chain, _fetch_site). Chain-name rejections are re-opened only for
# the names that were over-broad; a re-check re-rejects any real chain.
REQUALIFY_REASONS = (
    r"^(site unreachable|no email found on site|restaurant with no sign|"
    r"franchise language on site|duplicate email|"
    r"chain name \((tap house|brewhouse|casino|airport|chili|denny|applebee|hooter|"
    r"carrabba|famous dave|mcfadden|chuy)\))")


def _requalify_once(cursor) -> Optional[int]:
    """ONE-TIME: give every candidate rejected by a rule that has since been
    fixed another look — site unreachable (now retried, with 2xx/404/TLS
    recoveries), no email (now call-only), the drinks gate (now reads menu
    pages), franchise language and over-broad chain names (now narrower), a
    shared email (now kept). Back to 'new', so the next runs re-crawl them
    under today's rules; nothing is promoted without passing those. Marker row
    in crm_leadgen_oneshots, same transaction, so it runs once."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crm_leadgen_oneshots (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            detail TEXT
        )
    """)
    cursor.execute("""
        INSERT INTO crm_leadgen_oneshots (name, applied_at) VALUES (%s, %s)
        ON CONFLICT (name) DO NOTHING RETURNING name
    """, ("requalify_2026_09_fixes", now_iso()))
    if not cursor.fetchone():
        return None
    cursor.execute("""
        UPDATE crm_lead_candidates
           SET status = 'new', enrich_attempts = 0, retry_after = NULL
         WHERE status = 'rejected' AND promoted_lead_id IS NULL
           AND lower(COALESCE(reject_reason, '')) ~ %s
    """, (REQUALIFY_REASONS,))
    reopened = cursor.rowcount
    cursor.execute("UPDATE crm_leadgen_oneshots SET detail = %s WHERE name = %s",
                   (json.dumps({"reopened": reopened}), "requalify_2026_09_fixes"))
    return reopened


def _rescore_map_penalties_once(cursor) -> Optional[tuple[int, int]]:
    """ONE-TIME: apply the Asian-cuisine / tourist-strip penalties to rows
    scored before they existed. Unlike the `_reconcile_*` passes this is not
    meant to run every boot: a marker row in `crm_leadgen_oneshots` is written
    in the same transaction as the updates, so it runs once, and a failure
    rolls both back and it tries again on the next boot.

    Adds the penalty to the stored score rather than recomputing it: the
    site-based parts of the score came from crawled HTML that isn't kept.
    Touches banked candidates (`qualified`) and promoted leads nobody has
    called yet — never a lead someone has worked. Returns None if it has
    already run.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crm_leadgen_oneshots (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            detail TEXT
        )
    """)
    cursor.execute("""
        INSERT INTO crm_leadgen_oneshots (name, applied_at) VALUES (%s, %s)
        ON CONFLICT (name) DO NOTHING RETURNING name
    """, ("rescore_map_penalties_2026_09", now_iso()))
    if not cursor.fetchone():
        return None

    cursor.execute("""
        SELECT c.id, c.status, c.city, c.raw_tags, c.promoted_lead_id,
               (l.id IS NOT NULL) AS lead_unworked
          FROM crm_lead_candidates c
          LEFT JOIN crm_leads l
                 ON l.id = c.promoted_lead_id
                AND l.status = 'new' AND l.last_touch_at IS NULL
         WHERE c.status IN ('qualified', 'promoted')
           AND c.enriched_at IS NOT NULL AND c.enriched_at < %s
    """, (MAP_PENALTY_CUTOFF,))
    cands = leads = 0
    for row in cursor.fetchall():
        if row["status"] == "promoted" and not row["lead_unworked"]:
            continue
        try:
            tags = json.loads(row["raw_tags"] or "{}")
        except (TypeError, ValueError):
            continue
        penalty = _map_fit_penalty(tags, row["city"])
        if not penalty:
            continue
        cursor.execute("UPDATE crm_lead_candidates SET score = score + %s WHERE id = %s",
                       (penalty, row["id"]))
        cands += 1
        if row["status"] == "promoted":
            cursor.execute(
                "UPDATE crm_leads SET lead_score = COALESCE(lead_score, 0) + %s "
                "WHERE id = %s", (penalty, row["promoted_lead_id"]))
            leads += 1
    cursor.execute("UPDATE crm_leadgen_oneshots SET detail = %s WHERE name = %s",
                   (f"candidates={cands} leads={leads}", "rescore_map_penalties_2026_09"))
    print(f"[leadgen] LEADGEN_RESCORE_DONE candidates={cands} leads={leads}", flush=True)
    return cands, leads


# Words that say what KIND of place it is, not which one. "Olde Town Tavern
# & Grill" and "Olde Town Tavern" are the same bar; the words that tell a
# venue apart are what's left once these are gone.
_GENERIC_NAME_WORDS = {
    "the", "and", "of", "at", "on", "bar", "bars", "pub", "tavern", "grill", "grille",
    "restaurant", "lounge", "kitchen", "cafe", "saloon", "tap", "taproom", "taphouse",
    "brewing", "brewery", "brewpub", "cantina", "club", "eatery", "bistro", "diner",
    "co", "company", "inc", "llc", "ltd",
}


def _name_words(name: Optional[str]) -> list:
    return re.findall(r"[a-z0-9]+", (name or "").lower().replace("&", " and "))


def same_venue(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two names plausibly belong to one venue.

    Compared on the distinctive words only, and one set inside the other is
    enough: "Olde Town Tavern & Grill" and "Olde Town Tavern" match. Two bars
    one owner runs off one phone — "Blue Room" and "The Monkey Bar" — don't,
    which is why a shared phone alone never makes a duplicate. A name made of
    nothing but generic words ("The Tavern") has to match exactly.
    """
    wa, wb = _name_words(a), _name_words(b)
    ca = {w for w in wa if w not in _GENERIC_NAME_WORDS and len(w) > 1}
    cb = {w for w in wb if w not in _GENERIC_NAME_WORDS and len(w) > 1}
    if not ca or not cb:
        return bool(wa) and wa == wb
    return ca <= cb or cb <= ca


def _phone10(phone: Optional[str]) -> str:
    digits = re.sub(r"\D", "", phone or "")[-10:]
    return digits if len(digits) == 10 else ""


_PHONE_IN_TEXT = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")


def phones_in(text: Optional[str]) -> set:
    """Every US-shaped phone number written anywhere in `text`, as ten digits.

    A logged call's notes often carry more than one: Olde Town's read
    "(720) 242-9667 or (303) 467-1472", and the map's copy of the bar could
    be listed under either.
    """
    return {p for p in (_phone10(m) for m in _PHONE_IN_TEXT.findall(text or "")) if p}


def _worked(row: dict) -> bool:
    return bool(row.get("last_touch_at")) or (row.get("status") or "new") != "new"


def duplicate_folds(rows: list) -> list:
    """(keeper_id, duplicate_id) for every never-called copy of a bar that's
    already in the book: same phone number and the same name by `same_venue`.

    The keeper is the lead someone has worked, else the oldest. Only a
    never-contacted, auto-sourced copy is ever folded away — two worked rows,
    or the operator's own entries, are left for a person to sort out.
    """
    groups: dict = {}
    for row in rows:
        keys = {_phone10(row.get("phone"))}
        if _worked(row):
            # Numbers the operator wrote down on a worked lead count too.
            keys |= phones_in(row.get("notes"))
        for key in keys - {""}:
            groups.setdefault(key, []).append(row)
    folds = []
    folded: set = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        keeper = min(group, key=lambda r: (not _worked(r), r.get("created_at") or ""))
        for row in group:
            if (row["id"] != keeper["id"] and row["id"] not in folded
                    and not _worked(row) and row.get("source") == "leadgen"
                    and same_venue(row.get("name"), keeper.get("name"))):
                folds.append((keeper["id"], row["id"]))
                folded.add(row["id"])
    return folds


def _reconcile_duplicate_leads(cursor) -> int:
    """Fold never-called copies of a bar into the lead already in play.

    The same venue could land twice: quick-add created a fresh row for a bar
    the generator had already put on the call list, and the generator's own
    check never compared phone numbers, so a map listing named a little
    differently from the one the operator logged ("Olde Town Tavern" /
    "Olde Town Tavern & Grill") came through as a new bar. The copy nobody
    had called sat on the call list while the real one, with the call on it,
    was in the CRM tab.

    What the copy knew and the keeper doesn't (email, hours, timezone, the
    venue's facts, a manager's name) is filled in first; its candidate and
    any queued email move to the keeper, so the generator can't promote it
    again and nothing scheduled is lost. Idempotent and cheap, so every boot.
    """
    cursor.execute("""
        SELECT id, name, phone, status, last_touch_at, source, created_at, notes
          FROM crm_leads WHERE phone IS NOT NULL OR notes IS NOT NULL
    """)
    folds = duplicate_folds(cursor.fetchall())
    for keeper_id, dup_id in folds:
        cursor.execute("""
            UPDATE crm_leads k SET
                email = COALESCE(k.email, d.email),
                email_kind = COALESCE(k.email_kind, d.email_kind),
                loc = COALESCE(k.loc, d.loc),
                opening_hours = COALESCE(k.opening_hours, d.opening_hours),
                tz_offset_hours = COALESCE(k.tz_offset_hours, d.tz_offset_hours),
                tz_name = COALESCE(k.tz_name, d.tz_name),
                venue_facts = COALESCE(k.venue_facts, d.venue_facts),
                opener = COALESCE(k.opener, d.opener),
                lead_score = COALESCE(k.lead_score, d.lead_score),
                manager_role = CASE WHEN k.manager_name IS NULL
                                    THEN d.manager_role ELSE k.manager_role END,
                manager_source = CASE WHEN k.manager_name IS NULL
                                      THEN d.manager_source ELSE k.manager_source END,
                manager_seen_at = CASE WHEN k.manager_name IS NULL
                                       THEN d.manager_seen_at ELSE k.manager_seen_at END,
                manager_name = COALESCE(k.manager_name, d.manager_name)
              FROM crm_leads d
             WHERE k.id = %s AND d.id = %s
        """, (keeper_id, dup_id))
        cursor.execute("UPDATE crm_lead_candidates SET promoted_lead_id = %s "
                       "WHERE promoted_lead_id = %s", (keeper_id, dup_id))
        cursor.execute("UPDATE crm_scheduled_emails SET lead_id = %s WHERE lead_id = %s",
                       (keeper_id, dup_id))
        cursor.execute("DELETE FROM crm_leads WHERE id = %s", (dup_id,))
    return len(folds)


def _backfill_venue_facts(cursor) -> int:
    """Give leads harvested before the extractor existed their facts anyway.

    The map tags were stored at harvest time, so cuisine, hours, address, age
    and size can all be recovered without re-crawling anything. Site-derived
    facts (taps, seats, "since 1974") can't — those need the page, and the
    next enrichment pass will add them.
    """
    cursor.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'crm_leads' AND column_name = 'venue_facts'
    """)
    if not cursor.fetchone():
        return 0

    cursor.execute("""
        SELECT l.id, c.raw_tags, l.opening_hours
          FROM crm_leads l
          JOIN crm_lead_candidates c ON c.promoted_lead_id = l.id
         WHERE l.venue_facts IS NULL AND c.raw_tags IS NOT NULL
         LIMIT 2000
    """)
    filled = 0
    for row in cursor.fetchall():
        try:
            tags = json.loads(row["raw_tags"] or "{}")
        except (TypeError, ValueError):
            continue
        blob = venue_facts.dumps(
            venue_facts.extract_facts(tags, "", row["opening_hours"]))
        if blob:
            cursor.execute("UPDATE crm_leads SET venue_facts = %s WHERE id = %s",
                           (blob, row["id"]))
            filled += 1
    return filled


def _reconcile_bad_emails(cursor) -> tuple[int, int]:
    """Strip addresses nothing can vouch for, from candidates and from leads.

    Blocking these at promote only helps rows promoted from now on. Anything
    already on the call list keeps its junk address until something takes it
    off, and the operator sees `bank<uuid>@test.com` next to a real bar.

    The lead itself is kept, not deleted: the venue is real and its phone
    number is real and validated. Only the address goes, and the candidate
    behind it is put back to 'new' so the next run crawls it again and can
    find a genuine one.
    """
    cleaned_candidates = cleaned_leads = 0

    cursor.execute("""
        SELECT id, email, email_source FROM crm_lead_candidates
         WHERE email IS NOT NULL AND email <> ''
    """)
    for row in cursor.fetchall():
        unsourced = not (row["email_source"] or "").strip()
        if unsourced or EMAIL_BLOCKLIST.search(row["email"]):
            # Back to 'new' so enrichment re-crawls it; 'rejected' would bank
            # it forever on the strength of an address that was never real.
            cursor.execute("""
                UPDATE crm_lead_candidates
                   SET email = NULL, email_source = NULL, email_kind = NULL,
                       status = CASE WHEN status = 'promoted' THEN status ELSE 'new' END,
                       enriched_at = NULL,
                       reject_reason = 'email had no source — re-crawling'
                 WHERE id = %s
            """, (row["id"],))
            cleaned_candidates += 1

    cursor.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'crm_leads' AND column_name = 'email_kind'
    """)
    if cursor.fetchone():
        cursor.execute("""
            SELECT id, email, email_kind FROM crm_leads
             WHERE source = 'leadgen' AND email IS NOT NULL AND email <> ''
        """)
        for row in cursor.fetchall():
            # Re-classify while we're here. email_kind decides where a lead
            # sits in the call list, so a row classified by an older rule is
            # sorted by a rule that no longer applies — and the rules have
            # already changed once, when "any run of letters" stopped counting
            # as a person's name.
            kind = email_kind(row["email"])
            if kind != row["email_kind"] and not EMAIL_BLOCKLIST.search(row["email"]):
                cursor.execute("UPDATE crm_leads SET email_kind = %s WHERE id = %s",
                               (kind, row["id"]))
            if EMAIL_BLOCKLIST.search(row["email"]):
                cursor.execute("""
                    UPDATE crm_leads
                       SET email = NULL, email_kind = NULL,
                           notes = COALESCE(notes || E'\n', '') || %s
                     WHERE id = %s
                """, (f"[{now_iso()[:10]}] removed an unusable email address "
                      f"({row['email']}) — placeholder or machine-generated, "
                      f"never a real mailbox. Phone is unaffected.", row["id"]))
                cleaned_leads += 1

    return cleaned_candidates, cleaned_leads


def _reconcile_timezones(cursor) -> int:
    """Re-file any row whose stored zone disagrees with the current rule.

    Idempotent and cheap, so it runs every boot rather than once. A one-shot
    migration would fix today's rows and then be wrong again the next time a
    boundary is corrected — and a lead in the wrong tab is called during its
    dinner service, which is the mistake the tabs exist to prevent. Nothing
    sets these by hand, so there is no operator edit to clobber.
    """
    moved = 0
    cursor.execute("""
        SELECT id, lat, lon, state, tz_offset_hours, tz_name FROM crm_lead_candidates
         WHERE lon IS NOT NULL
    """)
    for row in cursor.fetchall():
        correct = us_tz_offset(row["lon"], row["state"], row["lat"])
        zone = us_tz_name(row["lon"], row["state"], row["lat"])
        if correct is not None and (correct != row["tz_offset_hours"]
                                    or zone != row.get("tz_name")):
            cursor.execute(
                "UPDATE crm_lead_candidates SET tz_offset_hours=%s, tz_name=%s WHERE id=%s",
                (correct, zone, row["id"]))
            moved += 1

    # crm_leads keeps no coordinates, only "City, ST" — enough for the state
    # rule, which is the part that was wrong. A lead whose loc has no state
    # code is left alone rather than guessed at.
    #
    # Guarded because the CRM's own schema step may not have run yet: the two
    # init functions are independent by design (a CRM schema failure must not
    # stop the product API booting), so their order is not something this can
    # assume. If the column isn't there, the next boot picks it up.
    cursor.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'crm_leads' AND column_name = 'tz_name'
    """)
    if not cursor.fetchone():
        return moved

    cursor.execute("""
        SELECT id, loc, tz_offset_hours, tz_name FROM crm_leads
         WHERE source = 'leadgen' AND loc IS NOT NULL AND loc <> ''
    """)
    for row in cursor.fetchall():
        state = (row["loc"].rsplit(",", 1)[-1] or "").strip().upper()
        if len(state) != 2 or state in _SPLIT_STATES or state in _LAT_SPLIT_STATES:
            continue          # no state, or one that needs coordinates we lack
        correct = _STATE_TZ.get(state)
        zone = us_tz_name(None, state)
        if correct is not None and (correct != row["tz_offset_hours"]
                                    or zone != row.get("tz_name")):
            cursor.execute(
                "UPDATE crm_leads SET tz_offset_hours=%s, tz_name=%s WHERE id=%s",
                (correct, zone, row["id"]))
            moved += 1
    return moved


# A starting territory list: US metros with real bar density. Coordinates are
# baked in so the first harvest doesn't depend on a geocoder being up.
# Territory. Sized against consumption, not ambition: at roughly a dozen
# qualified leads per metro from bars alone, the first 58 cities were about a
# month of calling before the well ran dry — and running dry is silent, which
# makes it the worst kind of failure. Widening the harvest to restaurants
# multiplies each city; these add breadth on top, biased towards mid-size
# metros with real independent bar scenes and away from the chain-heavy sunbelt
# sprawl the qualifier would mostly reject anyway.
SEED_CITIES_EXTRA = [
    ("Madison", "WI", 43.0731, -89.4012), ("Ann Arbor", "MI", 42.2808, -83.7430),
    ("Asheville", "NC", 35.5951, -82.5515), ("Savannah", "GA", 32.0809, -81.0912),
    ("Charleston", "SC", 32.7765, -79.9311), ("Providence", "RI", 41.8240, -71.4128),
    ("Burlington", "VT", 44.4759, -73.2121), ("Portsmouth", "NH", 43.0718, -70.7626),
    ("Ithaca", "NY", 42.4440, -76.5019), ("Lancaster", "PA", 40.0379, -76.3055),
    ("Roanoke", "VA", 37.2710, -79.9414), ("Knoxville", "TN", 35.9606, -83.9207),
    ("Chattanooga", "TN", 35.0456, -85.3097), ("Birmingham", "AL", 33.5186, -86.8104),
    ("Athens", "GA", 33.9519, -83.3576), ("Greenville", "SC", 34.8526, -82.3940),
    ("Lexington", "KY", 38.0406, -84.5037), ("Dayton", "OH", 39.7589, -84.1916),
    ("Grand Rapids", "MI", 42.9634, -85.6681), ("Fort Collins", "CO", 40.5853, -105.0844),
    ("Boise", "ID", 43.6150, -116.2023), ("Missoula", "MT", 46.8721, -113.9940),
    ("Bend", "OR", 44.0582, -121.3153), ("Spokane", "WA", 47.6588, -117.4260),
    ("Bellingham", "WA", 48.7519, -122.4787), ("Eugene", "OR", 44.0521, -123.0868),
    ("Santa Cruz", "CA", 36.9741, -122.0308), ("Santa Fe", "NM", 35.6870, -105.9378),
    ("Flagstaff", "AZ", 35.1983, -111.6513), ("Boulder", "CO", 40.0150, -105.2705),
    ("Iowa City", "IA", 41.6611, -91.5302), ("Omaha", "NE", 41.2565, -95.9345),
    ("Sioux Falls", "SD", 43.5446, -96.7311), ("Duluth", "MN", 46.7867, -92.1005),
    ("Traverse City", "MI", 44.7631, -85.6206), ("Bloomington", "IN", 39.1653, -86.5264),
    ("Columbia", "MO", 38.9517, -92.3341), ("Fayetteville", "AR", 36.0626, -94.1574),
    ("Wilmington", "NC", 34.2257, -77.9447), ("Frederick", "MD", 39.4143, -77.4105),
]

SEED_CITIES = [
    ("Austin", "TX", 30.2672, -97.7431), ("Nashville", "TN", 36.1627, -86.7816),
    ("Denver", "CO", 39.7392, -104.9903), ("Portland", "OR", 45.5152, -122.6784),
    ("Seattle", "WA", 47.6062, -122.3321), ("Chicago", "IL", 41.8781, -87.6298),
    ("Philadelphia", "PA", 39.9526, -75.1652), ("Atlanta", "GA", 33.7490, -84.3880),
    ("Charleston", "SC", 32.7765, -79.9311), ("Savannah", "GA", 32.0809, -81.0912),
    ("New Orleans", "LA", 29.9511, -90.0715), ("Kansas City", "MO", 39.0997, -94.5786),
    ("Minneapolis", "MN", 44.9778, -93.2650), ("Milwaukee", "WI", 43.0389, -87.9065),
    ("Pittsburgh", "PA", 40.4406, -79.9959), ("Cleveland", "OH", 41.4993, -81.6944),
    ("Columbus", "OH", 39.9612, -82.9988), ("Cincinnati", "OH", 39.1031, -84.5120),
    ("Indianapolis", "IN", 39.7684, -86.1581), ("St. Louis", "MO", 38.6270, -90.1994),
    ("Louisville", "KY", 38.2527, -85.7585), ("Memphis", "TN", 35.1495, -90.0490),
    ("Richmond", "VA", 37.5407, -77.4360), ("Baltimore", "MD", 39.2904, -76.6122),
    ("Boston", "MA", 42.3601, -71.0589), ("Providence", "RI", 41.8240, -71.4128),
    ("Buffalo", "NY", 42.8864, -78.8784), ("Detroit", "MI", 42.3314, -83.0458),
    ("Madison", "WI", 43.0731, -89.4012), ("Des Moines", "IA", 41.5868, -93.6250),
    ("Omaha", "NE", 41.2565, -95.9345), ("Oklahoma City", "OK", 35.4676, -97.5164),
    ("Tulsa", "OK", 36.1540, -95.9928), ("San Antonio", "TX", 29.4241, -98.4936),
    ("Houston", "TX", 29.7604, -95.3698), ("Dallas", "TX", 32.7767, -96.7970),
    ("Fort Worth", "TX", 32.7555, -97.3308), ("Phoenix", "AZ", 33.4484, -112.0740),
    ("Tucson", "AZ", 32.2226, -110.9747), ("Albuquerque", "NM", 35.0844, -106.6504),
    ("Salt Lake City", "UT", 40.7608, -111.8910), ("Boise", "ID", 43.6150, -116.2023),
    ("Spokane", "WA", 47.6588, -117.4260), ("Sacramento", "CA", 38.5816, -121.4944),
    ("San Diego", "CA", 32.7157, -117.1611), ("Las Vegas", "NV", 36.1699, -115.1398),
    ("Reno", "NV", 39.5296, -119.8138), ("Tampa", "FL", 27.9506, -82.4572),
    ("Orlando", "FL", 28.5383, -81.3792), ("Jacksonville", "FL", 30.3322, -81.6557),
    ("Raleigh", "NC", 35.7796, -78.6382), ("Charlotte", "NC", 35.2271, -80.8431),
    ("Asheville", "NC", 35.5951, -82.5515), ("Greenville", "SC", 34.8526, -82.3940),
    ("Birmingham", "AL", 33.5186, -86.8104), ("Little Rock", "AR", 34.7465, -92.2896),
    ("Boulder", "CO", 40.0150, -105.2705), ("Colorado Springs", "CO", 38.8339, -104.8214),
]


def _seed_cities(cursor):
    now = now_iso()
    for name, state, lat, lon in SEED_CITIES + SEED_CITIES_EXTRA:
        cursor.execute("""
            INSERT INTO crm_leadgen_cities (id, name, state, lat, lon, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (generate_id(), name, state, lat, lon, now))
    added = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    total = len(SEED_CITIES) + len(SEED_CITIES_EXTRA)
    print(f"[leadgen] city list checked: {total} in the seed list", flush=True)


# ── Classification helpers ──────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()


# Compiled once, matched on WORD boundaries. It used to be a plain substring
# test, so "casino" rejected Austin's Casino El Camino (an independent dive),
# "chili" rejected Chili Pepper Grill and "tap house" / "brewhouse" / "airport"
# swept up independents that merely use the words.
# Brands whose NAME is a possessive: the stem alone is an ordinary word or a
# first name ("Chili Pepper Grill", "Denny's Tavern" run by a Denny), so only
# the possessive form — "chilis" once normalize_name drops the apostrophe —
# is the chain.
_POSSESSIVE_CHAINS = {"chili", "denny", "applebee", "hooter", "carrabba", "famous dave",
                      "mcfadden", "chuy"}


def _chain_alt(c: str) -> str:
    return re.escape(c) + ("s" if c in _POSSESSIVE_CHAINS else "(?:s)?")


_CHAIN_NAME_RE = re.compile(
    r"\b(" + "|".join(_chain_alt(c) for c in sorted(CHAIN_NAMES, key=len, reverse=True))
    + r")\b")


def looks_like_chain(name: str, website: str = "", site_html: str = "") -> Optional[str]:
    """Why this is a chain, or None if it looks independent.

    The site test only fires on STORE-LOCATOR / franchise language ("find a
    location", "franchise", "corporate office"). "Our locations" / "all
    locations" used to reject too, and measured on Austin it threw out
    Pinthouse Pizza ("all locations open at 10:30AM for the game") and a brewpub
    whose beer description said "nationwide" — a local owner with two or three
    bars is still the independent 86'd is for.
    """
    norm = normalize_name(name)
    m = _CHAIN_NAME_RE.search(norm)
    if m:
        return f"chain name ({m.group(1)})"
    # "Sullivan's #14", "Tavern Store 3"
    if re.search(r"\b(#\s*\d+|store\s*\d+|location\s*\d+|unit\s*\d+)\b", norm):
        return "numbered location"
    if site_html and CHAIN_SITE_HINTS.search(site_html):
        return "franchise language on site"
    return None


def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", (url or "").strip(), re.I)
    if not m:
        return ""
    return m.group(1).lower().removeprefix("www.")


# Dominant timezone per US state. The Central/Mountain boundary is not a
# meridian — it runs through west Texas, Kansas, Nebraska and the Dakotas — so
# longitude alone cannot get Texas right. A plain -97.5 cutoff put AUSTIN
# (-97.74), SAN ANTONIO (-98.49) and OKLAHOMA CITY (-97.51) in Mountain, all
# three an hour wrong. That was survivable when the offset only tinted a hint;
# now that the zones are tabs and the whole workflow is "work west as the rush
# moves", an hour wrong is exactly the mistake the tabs exist to prevent.
_STATE_TZ = {
    # Eastern
    "CT": -5, "DC": -5, "DE": -5, "GA": -5, "MA": -5, "MD": -5, "ME": -5,
    "NC": -5, "NH": -5, "NJ": -5, "NY": -5, "OH": -5, "PA": -5, "RI": -5,
    "SC": -5, "VA": -5, "VT": -5, "WV": -5,
    # Central
    "AL": -6, "AR": -6, "IA": -6, "IL": -6, "LA": -6, "MN": -6, "MO": -6,
    "MS": -6, "OK": -6, "TX": -6, "WI": -6,
    # Mountain
    "AZ": -7, "CO": -7, "MT": -7, "NM": -7, "UT": -7, "WY": -7,
    # Pacific
    "CA": -8, "NV": -8, "WA": -8,
}
# States genuinely split by the line, where longitude decides which side.
# (state, cutoff, west_of_cutoff, east_of_cutoff)
_SPLIT_STATES = {
    "TX": (-104.9, -7, -6),    # only El Paso and Hudspeth are Mountain
    "FL": (-85.0, -6, -5),     # the panhandle west of the Apalachicola
    "IN": (-86.7, -6, -5),     # the Gary and Evansville corners
    "KY": (-86.0, -6, -5),     # Louisville and Lexington Eastern, Bowling Green Central
    "TN": (-85.5, -6, -5),     # Nashville Central, Knoxville and Chattanooga Eastern
    "MI": (-87.5, -6, -5),     # the four Central counties at the Wisconsin end of the UP
    "KS": (-101.5, -7, -6),
    "NE": (-101.5, -7, -6),
    "ND": (-102.0, -7, -6),    # Bismarck is Central; the Mountain counties are the SW corner
    "SD": (-100.5, -7, -6),
    "OR": (-117.5, -8, -7),
    "NV": (-114.1, -8, -7),
}
# Idaho is the one state the line crosses horizontally rather than vertically:
# the northern panhandle is Pacific, everything from the Salmon River south —
# Boise included — is Mountain. Splitting it by longitude puts Boise (-116.2)
# in Pacific, an hour wrong for the state's largest city.
_LAT_SPLIT_STATES = {
    "ID": (45.5, -8, -7),      # (cutoff, north of it, south of it)
}


# The IANA zone behind each offset. Stored alongside the offset because the
# offset alone can't answer "what time is it there now?": it's a standard-time
# number, so from March to November every US zone is an hour off it, and
# Arizona never moves at all. An hour's error decides whether a call lands in
# the pre-open lull or the middle of service.
_ZONE_NAMES = {-5: "America/New_York", -6: "America/Chicago",
               -7: "America/Denver", -8: "America/Los_Angeles"}
# States that sit out daylight saving, so their offset and their zone disagree
# for two thirds of the year.
_NO_DST_STATES = {"AZ": "America/Phoenix"}


def us_tz_name(lon: Optional[float], state: Optional[str] = None,
               lat: Optional[float] = None) -> Optional[str]:
    """IANA zone name for a venue, e.g. 'America/Chicago'."""
    code = (state or "").strip().upper()[:2]
    if code in _NO_DST_STATES:
        # The Navajo Nation does observe DST, but it isn't where the bars are.
        return _NO_DST_STATES[code]
    return _ZONE_NAMES.get(us_tz_offset(lon, state, lat))


def us_tz_offset(lon: Optional[float], state: Optional[str] = None,
                 lat: Optional[float] = None) -> Optional[int]:
    """UTC offset for the call window, from the state where we know it.

    Standard time; DST is not modelled, which is fine — it shifts every zone
    together, so the ORDER the afternoon rolls west is unchanged and that
    ordering is what the tabs are for.
    """
    code = (state or "").strip().upper()[:2]
    if code in _LAT_SPLIT_STATES and lat is not None:
        cutoff, north, south = _LAT_SPLIT_STATES[code]
        return north if lat > cutoff else south
    if code in _SPLIT_STATES and lon is not None:
        cutoff, west, east = _SPLIT_STATES[code]
        return west if lon < cutoff else east
    if code in _STATE_TZ:
        return _STATE_TZ[code]
    if code in _LAT_SPLIT_STATES:
        return _LAT_SPLIT_STATES[code][2]      # no latitude: the bigger half

    # No usable state: fall back to longitude. Still wrong for the split
    # states, but every seeded city carries a state, so this is the path for
    # anything added later without one.
    if lon is None:
        return None
    if lon > -82.5:
        return -5   # Eastern
    if lon > -97.5:
        return -6   # Central
    if lon > -112.5:
        return -7   # Mountain
    return -8       # Pacific


# Script and style bodies, and HTML comments. Addresses in here were written
# by a developer or a library, never by the venue: jQuery validation messages
# ("Please use the format email@example.com"), analytics payloads, widget
# config, commented-out markup from a previous designer. Scanning them is how
# a crawler ends up with machine-shaped addresses nobody can reply to.
# Stripped by contacts.strip_non_content(), a linear scan.


def extract_emails(html: str) -> list[str]:
    """Every plausible human address on a page, best first.

    Only from what a visitor could actually read. `mailto:` links are pulled
    from the raw HTML first, because those are an address a human deliberately
    published; everything else is matched against the page with script, style
    and comment blocks removed.
    """
    found: list[str] = []
    seen = set()

    def add(addr: str):
        addr = addr.strip().lower().rstrip(".,;:")
        if addr in seen or EMAIL_BLOCKLIST.search(addr):
            return
        if len(addr) > 100 or addr.count("@") != 1:
            return
        seen.add(addr)
        found.append(addr)

    # mailto: first — it's a link a human put there on purpose.
    for m in MAILTO_RE.findall(html or ""):
        add(m)
    visible = strip_non_content(html)
    for m in EMAIL_RE.findall(visible):
        add(m)
    for user, dom, tld in OBFUSCATED_RE.findall(visible):
        add(f"{user}@{dom}.{tld}")

    # An address on the venue's own domain beats a free mailbox, which beats
    # anything else — but a gmail address is still a perfectly good sign for a
    # small independent bar, so it stays in the list.
    def rank(addr: str) -> int:
        local = addr.split("@")[0]
        if local in ("info", "hello", "contact", "hi", "bar", "events", "manager", "owner"):
            return 0
        return 1

    return sorted(found, key=rank)


# Things worth mentioning in an opener, cheapest signal first.
OPENER_SIGNALS = [
    (re.compile(r"craft cocktail|cocktail program|mixolog", re.I), "craft cocktail program"),
    (re.compile(r"(?<!\d)\d{2,3}\s{0,3}(taps|beers on tap|draft lines)", re.I), "a big tap list"),
    (re.compile(r"whisk(e)?y (bar|list|selection)|bourbon (bar|list)", re.I), "a whiskey list"),
    (re.compile(r"tequila|mezcal (bar|list)", re.I), "an agave list"),
    (re.compile(r"wine (list|bar|cellar)", re.I), "a wine list"),
    (re.compile(r"happy hour", re.I), "happy hour"),
    (re.compile(r"live music|live band", re.I), "live music"),
    (re.compile(r"private event|private hire|book(ing)? (the )?(space|room)", re.I), "private events"),
]


def opener_line(site_html: str, amenity: Optional[str]) -> Optional[str]:
    """A short, true thing about this venue to open the call with.

    Not a pitch — just something that shows the call isn't a random dial. Only
    ever taken from the venue's own site, so it can't be wrong about them.
    """
    hits = [label for pattern, label in OPENER_SIGNALS if pattern.search(site_html or "")]
    if not hits:
        return None
    return ", ".join(hits[:2])


# Signals that a venue already runs a modern, integrated system — which is
# where this pitch has the least room. None of these EXCLUDE a lead; they push
# it down the list. A reservation platform is the strongest of them: a venue
# that takes bookings through Resy or SevenRooms is running a stack, and the
# stack usually came with something that claims to do inventory.
POS_STACK_HINTS = re.compile(
    r"resy\.com|opentable\.com|exploretock\.com|sevenrooms\.com|yelp\.com/reservations"
    r"|toasttab\.com|order\.online/store|square(?:up)?\.site|clover\.com/online-ordering"
    r"|bentobox|popmenu|spoton\.com",
    re.I,
)
# White-tablecloth signals. Same idea, gentler weight — a fine-dining room can
# still be counting bottles on a clipboard, it's just less likely.
UPSCALE_HINTS = re.compile(
    r"tasting menu|prix[- ]fixe|sommelier|chef&#39;s table|chef.s table|michelin"
    r"|wine pairing|dress code|jacket required|omakase|degustation",
    re.I,
)
# Same idea again, read straight off the OSM `cuisine` tag instead of crawled
# text — no site fetch needed, it's already on the harvested candidate. Per
# Stephan's own sales experience: an Asian restaurant (sushi bar, ramen shop,
# izakaya, hot pot, ...) runs a materially higher rate of already having SOME
# system in place — POS-bundled inventory, a supplier relationship through a
# restaurant group — than an ordinary neighbourhood bar does. Gentler weight,
# same as UPSCALE_HINTS: plenty still count sake and well liquor by hand, and
# they stay on the list, just lower.
ASIAN_CUISINE_HINTS = re.compile(
    r"\basian\b|\bchinese\b|\bjapanese\b|\bsushi\b|\bthai\b|\bvietnamese\b"
    r"|\bkorean\b|\bkorean_bbq\b|\bdim_sum\b|\bramen\b|\bteppanyaki\b|\bhibachi\b"
    r"|\bpho\b|\bhot_pot\b|\bhotpot\b|\bizakaya\b|\bdumpling\b|\bpan_asian\b",
    re.I,
)
# The opposite end, and the sweet spot for this product: a room with a real
# liquor inventory and nobody to count it but the manager, after close, by
# hand. These are the calls that go well.
NEIGHBOURHOOD_HINTS = re.compile(
    r"\bdive\b|\btavern\b|\bsaloon\b|\bale ?house\b|\bpub\b|sports bar"
    r"|\bicehouse\b|watering hole|\bhonky[- ]?tonk\b|pool table|billiards"
    r"|\bdarts\b|karaoke|jukebox|shuffleboard|\bdive bar\b|beer garden"
    r"|happy hour|\bwell drinks\b|\bbuckets?\b of beer",
    re.I,
)
NEIGHBOURHOOD_NAME = re.compile(
    r"\btavern\b|\bsaloon\b|\bpub\b|\bdive\b|\bale ?house\b|\blounge\b"
    r"|\bicehouse\b|\bbar ?& ?grill\b|\bsports\b|\bwatering\b|\bhonky\b"
    r"|\bcantina\b|\bbrewhouse\b|\bpourhouse\b|\btaproom\b",
    re.I,
)

# ── The owner's rules for who belongs on the call list (2026-09-25) ────────
#
# In order: 1. no chains or corporate venues, 2. very confident it pours
# LIQUOR, 3. no main-strip tourist bars, 4. an email if they have one. The
# first three are exclusions, not score adjustments: Honky Tonk Central (329
# Broadway, one of four Broadway bars under one owner) and Sweedeedee (a
# beer-and-wine brunch café that "matched" liquor on the word Martinique in a
# hidden country list) both sat at the top of the call list because every one
# of these used to be a few points either way, and a personal-looking email
# outranked all of them.

# The main tourist strip in each metro, keyed by the city name `_seed_cities`
# stores on every candidate. Keyed by city because "Broadway" and "6th St" are
# ordinary streets elsewhere, and bounded by house number where the strip is
# only part of a street: East Austin's 6th St and San Diego's Hillcrest end of
# 5th Ave are exactly the independents the list is for. A missing house
# number on a strip street counts as on it. Not exhaustive: add a metro's
# strip here as it comes up.
#   (label, addr:street pattern, (lowest, highest house number) or None)
TOURIST_STRIPS = {
    "las vegas": [
        ("the Las Vegas Strip", r"\blas vegas (?:blvd|boulevard)\b", (1900, 4299)),
        ("Fremont Street", r"\bfremont (?:st|street)\b", (1, 799)),
    ],
    "nashville": [
        ("Lower Broadway", r"^(?:lower )?broadway$", (1, 699)),
        ("2nd Avenue", r"^(?:2nd|second) (?:ave|avenue)(?: (?:n|s|north|south))?$", (1, 499)),
        ("Printers Alley", r"\bprinters? alley\b", None),
    ],
    "new orleans": [
        ("Bourbon Street", r"\bbourbon (?:st|street)\b", None),
        ("Decatur Street", r"\bdecatur (?:st|street)\b", (1, 1299)),
    ],
    "austin": [
        ("Dirty Sixth", r"^(?:e|east)\.? 6th (?:st|street)$", (1, 799)),
        ("Rainey Street", r"\brainey (?:st|street)\b", None),
    ],
    "memphis": [("Beale Street", r"\bbeale (?:st|street)\b", (1, 399))],
    "san antonio": [("the River Walk", r"\briver ?walk\b|\bpaseo del rio\b", None)],
    "orlando": [
        ("International Drive", r"\binternational (?:dr|drive)\b", None),
        ("Universal CityWalk", r"\buniversal (?:blvd|boulevard)\b|\bcitywalk\b", None),
    ],
    "chicago": [
        ("Rush Street", r"^(?:n\.? |north )?rush (?:st|street)$", None),
        ("Division Street", r"^(?:w|west)\.? division (?:st|street)$", (1, 99)),
        ("Navy Pier", r"^(?:e|east)\.? grand (?:ave|avenue)$", (500, 999)),
    ],
    "san diego": [("the Gaslamp Quarter",
                   r"^(?:4th|5th|6th|fourth|fifth|sixth) (?:ave|avenue)$", (300, 999))],
    "savannah": [("River Street", r"^(?:(?:e|w|east|west)\.? )?river (?:st|street)$", None)],
    "baltimore": [("Power Plant Live", r"^market (?:pl|place)$", None)],
    "louisville": [("Fourth Street Live", r"^(?:s\.? |south )?(?:4th|fourth) (?:st|street)$",
                    (400, 499))],
    "fort worth": [("the Stockyards",
                    r"\bexchange (?:ave|avenue)\b|\brodeo plaza\b|\bstockyards (?:blvd|boulevard)\b",
                    None)],
    "boston": [("Faneuil Hall", r"\bfaneuil\b|\bquincy market\b|^(?:north|south) market (?:st|street)$"
                r"|^union (?:st|street)$", None)],
    "reno": [("the casino core", r"^(?:n\.? |north )?virginia (?:st|street)$", (1, 499))],
    "tampa": [("Ybor City's 7th Avenue", r"^(?:e|east)\.? (?:7th|seventh) (?:ave|avenue)$",
               (1300, 2099))],
    "charleston": [("City Market", r"^(?:n|s|north|south)\.? market (?:st|street)$", None)],
    "detroit": [("Greektown", r"^(?:e\.? |east )?monroe (?:st|street)$", (300, 699))],
    "santa cruz": [("the Beach Boardwalk", r"^beach (?:st|street)$", None)],
}

# Where a venue has no street address — casino-interior bars on the Strip
# often don't — or the district isn't one street, its map position decides.
# Paths are (lat, lon) centrelines with a width in metres either side; boxes
# are (south, north, west, east). Drawn from OpenStreetMap, 2026-09-25.
TOURIST_ZONES = {
    "las vegas": [
        # The resort corridor: casinos sit up to ~400 m back from the Blvd.
        ("the Las Vegas Strip", "path", ((36.0866, -115.1727), (36.1162, -115.1722),
                                         (36.1265, -115.1680), (36.1440, -115.1575),
                                         (36.1478, -115.1552)), 450),
        ("Fremont Street", "path", ((36.17173, -115.14632), (36.16830, -115.13880)), 90),
    ],
    "nashville": [("Lower Broadway", "path",
                   ((36.1621, -86.7745), (36.1603, -86.7797)), 110)],
    "san antonio": [("the River Walk", "box", (29.4200, 29.4290, -98.4935, -98.4845), 0)],
    "san diego": [("the Gaslamp Quarter", "box", (32.7062, 32.7160, -117.1625, -117.1580), 0)],
    "baltimore": [("Power Plant Live", "box", (39.2882, 39.2901, -76.6084, -76.6063), 0)],
    "kansas city": [("the Power & Light District", "box",
                     (39.0955, 39.0995, -94.5850, -94.5790), 0)],
    "louisville": [("Fourth Street Live", "box", (38.2508, 38.2533, -85.7585, -85.7561), 0)],
    "boston": [("Faneuil Hall", "box", (42.3590, 42.3615, -71.0575, -71.0522), 0)],
    "oklahoma city": [("Bricktown", "box", (35.4624, 35.4697, -97.5123, -97.5000), 0)],
    "santa cruz": [("the Beach Boardwalk", "box", (36.9633, 36.9658, -122.0212, -122.0129), 0)],
    "orlando": [("Universal CityWalk", "box", (28.4715, 28.4752, -81.4692, -81.4639), 0)],
}

_STRIP_RES = {city: [(label, re.compile(pat), rng) for label, pat, rng in strips]
              for city, strips in TOURIST_STRIPS.items()}


def _house_number(raw) -> Optional[int]:
    m = re.match(r"\s*(\d{1,6})", str(raw or ""))
    return int(m.group(1)) if m else None


def _metres_to_segment(p, a, b) -> float:
    k = math.cos(math.radians(p[0])) * 111320
    ax, ay, bx, by = a[1] * k, a[0] * 111320, b[1] * k, b[0] * 111320
    px, py = p[1] * k, p[0] * 111320
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy)
                                             / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def tourist_strip(tags: dict, city: Optional[str], lat=None, lon=None) -> Optional[str]:
    """Which main tourist strip the venue is on, or None.

    By its own street address first (and house number, where only part of
    the street is the strip), then by where it sits on the map."""
    key = (city or "").strip().lower()
    street = re.sub(r"\s+", " ", str((tags or {}).get("addr:street") or "")).strip().lower()
    if street:
        for label, pattern, rng in _STRIP_RES.get(key, ()):
            # North Las Vegas Blvd is dives and strip clubs, not the Strip.
            if not pattern.search(street) or (label == "the Las Vegas Strip"
                                              and re.search(r"\bn(?:orth)?\b", street)):
                continue
            number = _house_number((tags or {}).get("addr:housenumber"))
            if rng is None or number is None or rng[0] <= number <= rng[1]:
                return label
    try:
        point = (float(lat), float(lon))
    except (TypeError, ValueError):
        return None
    for label, kind, shape, width in TOURIST_ZONES.get(key, ()):
        if kind == "box":
            south, north, west, east = shape
            if south <= point[0] <= north and west <= point[1] <= east:
                return label
        elif any(_metres_to_segment(point, a, b) <= width for a, b in zip(shape, shape[1:])):
            return label
    return None


def _on_tourist_strip(tags: dict, city: Optional[str], lat=None, lon=None) -> bool:
    return tourist_strip(tags, city, lat, lon) is not None


# ── 2. Does it pour LIQUOR? ─────────────────────────────────────────────────
#
# Spirits, not beer and wine: a beer-and-wine room has no back bar to count.
# Read from what a visitor SEES on the venue's own pages (visible_text: no
# scripts, styles or markup) — the old check read raw HTML, and Squarespace
# ships a country picker in every page's script whose "Martinique" matched
# "martini", so every Squarespace restaurant "poured liquor". "Wine list",
# "draft beer" and "tap list" counted too, which is how a beer-and-wine brunch
# café reached the top of the list.
#
# Three kinds of evidence, all matched on lower-cased text:
#   - a site saying it doesn't pour at all (NO_LIQUOR_HINTS) or pours beer and
#     wine only (BEER_WINE_ONLY) — decides against, whatever else it says;
#   - DEFINITE: a phrase only a liquor programme uses (full bar, cocktail
#     menu, craft cocktails, a whiskey list, beer, wine & spirits);
#   - named spirits and spirit cocktails, each counted once. Food words after
#     one ("bourbon pecan pie", "vodka sauce", "rum cake") don't count.
# A bar or pub needs one definite phrase or one named spirit on its own site;
# a restaurant, a brewery, a wine bar or a taproom needs a definite phrase or
# two different named spirits. Nothing found is "not confident" — excluded.
BEER_WINE_ONLY = re.compile(
    r"\b(?:beers?|wines?) {0,3}(?:and|&|\+|/) {0,3}(?:beers?|wines?) {0,3}only\b"
    r"|\b(?:only|just) {1,3}(?:serve {1,3}|offer {1,3}|pour {1,3})?(?:beers?|wines?)"
    r" {0,3}(?:and|&|/) {0,3}(?:beers?|wines?)\b"
    r"|\b(?:beer|wine) {0,3}(?:and|&|/) {0,3}(?:beer|wine) {1,3}(?:license|licence|permit)\b"
    r"|\b(?:soju|sake|wine)[- ](?:based|infused)\b|\b(?:soju|wine|sake) cocktails?\b"
    r"|\bagave wine\b|\bwine margaritas?\b"
    r"|\b(?:no|don'?t serve|do not serve|doesn'?t serve) {1,3}(?:hard {1,3})?(?:liquor|spirits)\b")

_NOT_DRINK_AFTER = (r"(?![- ]{1,3}(?:sauce|shrimp|party|parties|attire|dress|hour|napkins?"
                    r"|tables?|glass(?:es)?\b))")
DEFINITE_LIQUOR = re.compile(
    r"\bfull[- ](?:service )?bar\b|\bfull liquor\b|\bwell (?:drinks?|liquor)\b"
    r"|\b(?:craft|signature|house|classic|handcrafted|hand[- ]crafted|specialty|seasonal"
    r"|premium) cocktails?\b" + _NOT_DRINK_AFTER +
    r"|\bcocktail (?:menu|list|program|programme|bar|lounge)\b"
    r"|\b(?:whiske?y|bourbon|scotch|tequila|mezcal|spirits?|liquor) "
    r"(?:list|selection|menu|collection|library|flights?|bar)\b"
    r"|\b(?:selection|list|flights?|collection) of (?:[a-z]{1,20} ){0,2}"
    r"(?:whiske?ys?|whiskies|bourbons?|scotch|tequilas?|mezcals?|rums?|gins?|vodkas?"
    r"|spirits|liquors?)\b"
    r"|\b(?:beers?|wines?) {0,2}(?:,|&|and|\+) {0,2}(?:(?:beers?|wines?) {0,2}(?:,|&|and|\+)"
    r" {0,2})?(?:spirits|liquor|cocktails)\b")

_FOOD_AFTER = (r"(?![- ]{1,3}(?:glaze[ds]?|sauce|cream sauce|cakes?|raisins?|pecan|pie|caramel"
               r"|vanilla|butter|bbq|barbe?cue|chicken|shrimp|salmon|cured|braised|brined"
               r"|marinated|bread|beans|maple|mustard|onion|jam|ribs|wings|steak|burger"
               r"|brownies?|cheesecake|fudge|syrup|soaked|reduction|aioli|vinaigrette"
               r"|sausage|pork|beef|pasta|penne|rigatoni|street|st\b|row\b|balls?|eggs?"
               r"|bonnet|tape|pizza|flatbread|mix|pop ?tarts?|cream cheese|frosting|icing"
               r"|donuts?|doughnuts?|cupcakes?|cookies?|ice cream|truffles?|french toast"
               r"|pancakes?|shake|milkshake)\b)")
NAMED_SPIRITS = re.compile(
    r"(?<!alla )\b(whiske?ys?|whiskies|bourbons?|scotch|tequilas?|mezcals?|mescal|vodkas?|gin"
    r"|rums?|cognac|pisco|cacha[cç]a|aquavit|akvavit|grappa|amaro|amari|absinthe|fernet"
    r"|campari|aperol|negronis?|martinis?|margaritas?|mojitos?|daiquiris?|moscow mules?"
    r"|mai tais?|sazeracs?|gimlets?|mint juleps?|long island iced teas?|liquors?)\b"
    + _FOOD_AFTER)
_SPIRIT_KEY = {"whisky": "whiskey", "whiskies": "whiskey", "mescal": "mezcal",
               "cachaça": "cachaca", "akvavit": "aquavit", "amari": "amaro"}

_BEER_WINE_NAME = re.compile(
    r"\b(?:wine bar|winery|wine room|wine house|wine shop|wine cellar|vino|enoteca|vinoteca"
    r"|taproom|tap room|brewing|brewery|brewpub|brew pub|beer garden|biergarten|beer hall"
    r"|bottle ?shop|cidery|cider house|meadery|tasting room)\b", re.I)
_DRINK_TAGS = ("drink:spirits", "drink:liquor", "drink:cocktail", "drink:cocktails")


def _spirit_key(word: str) -> str:
    word = _SPIRIT_KEY.get(word, word)
    return word[:-1] if word.endswith("s") and word not in ("scotch",) else word


def liquor_verdict(text: str, tags: Optional[dict] = None, name: str = "",
                   amenity: str = "") -> dict:
    """{"status": spirits | beer_wine | no_alcohol | unknown, "evidence": [...],
    "reason": why not, when it isn't spirits}. `text` is VISIBLE text."""
    tags = tags or {}
    low = (text or "").lower()
    if NO_LIQUOR_HINTS.search(low):
        return {"status": "no_alcohol", "evidence": [], "reason": "site says no alcohol served"}
    beer_wine = BEER_WINE_ONLY.search(low)
    if beer_wine:
        return {"status": "beer_wine", "evidence": [beer_wine.group(0)],
                "reason": f"beer and wine only (their site: “{beer_wine.group(0)}”)"}
    evidence: list = []
    definite = DEFINITE_LIQUOR.search(low)
    if definite:
        evidence.append(definite.group(0))
    named: dict = {}
    for m in NAMED_SPIRITS.finditer(low):
        named.setdefault(_spirit_key(m.group(1)), m.group(0))
        if len(named) >= 3:
            break
    if any(tags.get(k) in ("yes", "served", "only") for k in _DRINK_TAGS) \
            or tags.get("cocktails") == "yes":
        named.setdefault("map", "the map lists cocktails or spirits")
    evidence += [v for k, v in named.items() if v not in evidence]
    pours_first = ((amenity or tags.get("amenity")) in ("bar", "pub", "nightclub")
                   and not _BEER_WINE_NAME.search(name or tags.get("name") or "")
                   and tags.get("craft") not in ("brewery", "winery", "cider")
                   and tags.get("microbrewery") != "yes")
    if definite or len(named) >= 2 or (pours_first and named):
        return {"status": "spirits", "evidence": evidence[:3], "reason": None}
    return {"status": "unknown", "evidence": evidence[:3],
            "reason": "no sign on their own site that they pour liquor"}


# ── 1. Chains and corporate venues ──────────────────────────────────────────
#
# CHAIN_NAMES and franchise language (looks_like_chain) catch the brands we
# know. Two signals catch the ones we don't, from the harvest itself:
#   - a `brand` tag (or an `operator` that is one) on two or more venues:
#     McMenamins' pubs carry it; a single independent that a mapper tagged
#     with its own name (Aalto Lounge) does not repeat;
#   - one website domain used by venues in two or more harvest cities.
# A domain shared by a few bars in ONE town is a local owner with two or
# three rooms — still who 86'd is for — so that alone is not corporate.
# Shared hosts are no one's own domain and never count.
SHARED_HOSTS = re.compile(
    r"(?:^|\.)(?:" + PLATFORM_DOMAINS + r"|linktr\.ee|square\.site|business\.site"
    r"|google\.com|goo\.gl|bit\.ly|toasttab\.com|order\.online|menufy\.com|chownow\.com"
    r"|tripadvisor\.com|untappd\.com|beermenus\.com|singleplatform\.com|popmenu\.com"
    r"|clover\.com|spoton\.com|bentobox\.com|wixsite\.com|carrd\.co|tumblr\.com"
    r"|blogspot\.com|github\.io|myshopify\.com|mapquest\.com|yellowpages\.com)$", re.I)


def corporate_index(cursor) -> dict:
    """The brands and domains the harvest shows running more than one venue."""
    # Counted per VENUE: an independent whose map entry names itself as both
    # brand and operator is one venue, not two.
    cursor.execute("""
        SELECT lower(b) AS v FROM (
            SELECT id, substring(raw_tags from '"brand": "([^"]{1,100})"') AS b
              FROM crm_lead_candidates
            UNION ALL
            SELECT id, substring(raw_tags from '"operator": "([^"]{1,100})"')
              FROM crm_lead_candidates) x
         WHERE b IS NOT NULL GROUP BY lower(b) HAVING COUNT(DISTINCT id) >= 2
    """)
    brands = {r["v"] for r in cursor.fetchall()}
    cursor.execute("""
        SELECT d FROM (
            SELECT lower(substring(website from '^[A-Za-z]+://(?:www\\.)?([^/:?#]+)')) AS d,
                   lower(city) AS c
              FROM crm_lead_candidates) x
         WHERE d IS NOT NULL GROUP BY d HAVING COUNT(DISTINCT c) >= 2
    """)
    domains = {r["d"] for r in cursor.fetchall() if not SHARED_HOSTS.search(r["d"] or "")}
    return {"brands": brands, "domains": domains}


def corporate_reason(tags: dict, website: Optional[str], index: Optional[dict]) -> Optional[str]:
    """Why the venue is a chain or corporate-run, or None."""
    tags = tags or {}
    for key in ("name", "brand", "operator"):
        value = tags.get(key)
        if value and key != "name":
            m = _CHAIN_NAME_RE.search(normalize_name(value))
            if m:
                return f"chain ({key} {value})"
    index = index or {}
    for key in ("brand", "operator"):
        value = (tags.get(key) or "").strip()
        if value and value.lower() in index.get("brands", ()):
            return f"chain ({value}, on several venues)"
    domain = domain_of(website or "")
    if domain and domain in index.get("domains", ()):
        return f"chain (its website {domain} serves venues in several cities)"
    return None


def _stack_signals(site_html: str) -> dict:
    """What their own site says about the systems they already run."""
    out = {}
    if not site_html:
        return out
    if POS_STACK_HINTS.search(site_html):
        which = POS_STACK_HINTS.search(site_html).group(0).split(".")[0]
        out["runs_platform"] = {"value": which, "source": "their website"}
    if UPSCALE_HINTS.search(site_html):
        out["upscale"] = {"value": True, "source": "their website"}
    if NEIGHBOURHOOD_HINTS.search(site_html):
        out["neighbourhood"] = {"value": True, "source": "their website"}
    return out


def score_candidate(tags: dict, email: Optional[str], site_html: str,
                    manager: Optional[dict] = None,
                    city: Optional[str] = None) -> int:
    """How well this fits a bar-inventory pitch. Higher is better.

    Two things were added once the call list started sorting by this rather
    than just filtering on it.

    FIT. A venue that already runs Resy and Toast probably has something that
    claims to handle inventory, so the pitch lands in a crowded room. A
    neighbourhood bar with a pool table and a happy hour has a real liquor
    inventory and, most likely, a clipboard. Neither is a rule — plenty of
    fancy rooms still count by hand, and they stay on the list — but when
    there are fifty names in front of you, order matters more than inclusion.
    A tourist-strip address is the same idea from a different signal: not the
    venue's own words, but WHERE it is. Asian cuisine (OSM `cuisine` tag) is
    the same idea from a THIRD signal — not words, not location, but what
    kind of restaurant it is.

    REACH. A name to ask for and a human's mailbox both mean the call has
    somewhere to land, and both are rare enough to be worth putting first.
    """
    score = 0
    amenity = tags.get("amenity", "")
    name = tags.get("name", "") or ""
    if amenity in ("bar", "pub"):
        score += 3          # pours liquor by definition
    elif amenity == "nightclub":
        score += 2
    elif amenity == "restaurant":
        score += 1          # may or may not have a real bar program

    if email:
        score += 3
        kind = email_kind(email)
        if kind == "personal":
            score += 4      # dave@divebar.com: one human, answers, replies
        elif kind == "owner":
            score += 2      # owner@ / gm@: reaches a decision-maker, unnamed

    # A name to ask for turns a cold call into asking for someone.
    if manager:
        score += 5

    if site_html and liquor_verdict(visible_text(site_html[:400000]), tags, name,
                                    amenity)["status"] == "spirits":
        score += 2
    if tags.get("brand") or tags.get("operator"):
        score -= 2          # branded/operated usually means a group
    if tags.get("opening_hours"):
        score += 1          # a maintained listing is a real, live business

    # Fit signals, weakest to strongest.
    if NEIGHBOURHOOD_NAME.search(name):
        score += 2
    if site_html and NEIGHBOURHOOD_HINTS.search(site_html):
        score += 2
    if site_html and UPSCALE_HINTS.search(site_html):
        score -= 2
    if site_html and POS_STACK_HINTS.search(site_html):
        score -= 3
    return score + _map_fit_penalty(tags, city)


def _map_fit_penalty(tags: dict, city: Optional[str]) -> int:
    """The fit penalties read off the map alone — no crawled text needed.

    Kept apart from score_candidate() so `_rescore_map_penalties_once()` can
    apply exactly the same numbers to rows scored before these existed.
    """
    penalty = 0
    if ASIAN_CUISINE_HINTS.search(tags.get("cuisine") or ""):
        penalty -= 2
    if _on_tourist_strip(tags, city):
        penalty -= 4         # Vegas Strip, Lower Broadway — already has a system
    return penalty


def geocode_city(name: str, state: Optional[str] = None) -> Optional[tuple[float, float]]:
    """City name → (lat, lon) via Nominatim. None when it can't be found.

    Only used when someone adds territory by name; the seed list carries its own
    coordinates so first harvest never depends on a geocoder being reachable.
    """
    query = ", ".join(x for x in [name, state, "USA"] if x)
    import urllib.parse
    url = f"{NOMINATIM}?q={urllib.parse.quote(query)}&format=json&limit=1"
    body, status = _http(url, timeout=25)
    if status != 200:
        return None
    try:
        results = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not results:
        return None
    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, ValueError, TypeError):
        return None


def find_venue_website(name: str, loc: Optional[str] = None) -> Optional[str]:
    """A venue's own website from OpenStreetMap, looked up by name (+ town).

    For a bar the operator found themselves: the notes say "their email is on
    the website" without the URL, and OSM usually has the `website` tag.
    """
    query = ", ".join(x for x in [name, loc] if x)
    url = (f"{NOMINATIM}?q={urllib.parse.quote(query)}"
           "&format=json&limit=3&countrycodes=us&extratags=1")
    body, status = _http(url, timeout=15)
    if status != 200:
        return None
    try:
        results = json.loads(body)
    except json.JSONDecodeError:
        return None
    for r in results:
        tags = r.get("extratags") or {}
        site = (tags.get("website") or tags.get("contact:website") or "").strip()
        if site:
            return site if site.lower().startswith("http") else "http://" + site
    return None


def find_email_on_site(website: str, max_pages: int = 4) -> tuple[Optional[str], Optional[str]]:
    """(email, page it was read on) from a venue's own site, or (None, None).

    Same order enrich_candidate uses: homepage, then the site's own contact
    links, then the usual guessed paths. Bounded so a click in the UI stays
    a few seconds, not a minute.
    """
    home, status = _http(website, timeout=PAGE_TIMEOUT, verify_public=True)
    if status != 200 or not home:
        return None, None
    found = extract_emails(home)
    if found:
        return found[0], website
    urls: list[str] = []
    for href, _kind in CONTACT_LINK_RE.findall(home):
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        full = urllib.parse.urljoin(website, href)
        if full.lower().startswith(("http://", "https://")) \
                and domain_of(full) == domain_of(website) and full not in urls:
            urls.append(full)
    for path in CONTACT_PATHS:
        full = website.rstrip("/") + path
        if full not in urls:
            urls.append(full)
    for url in urls[:max_pages - 1]:
        body, status = _http(url, timeout=PAGE_TIMEOUT, verify_public=True)
        if status == 200 and body:
            found = extract_emails(body)
            if found:
                return found[0], url
    return None, None


# ── Stage 1: harvest ────────────────────────────────────────────────────────

def _overpass_answer(body: str, status: int) -> Optional[dict]:
    """The parsed answer if it's usable: JSON with at least one element."""
    if status != 200 or not body.strip().startswith("{"):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return data if data.get("elements") else None


def _overpass(query: str) -> Optional[dict]:
    """Query the first mirror that returns a usable answer.

    An empty element list counts as NOT usable and moves to the next mirror.
    A city genuinely containing zero bars is possible but vanishingly rare, and
    the cost of being wrong about it is one extra request — whereas accepting
    an empty answer means trusting a mirror that may simply not hold this part
    of the planet (see the note on OVERPASS_MIRRORS). Silent emptiness is the
    failure this pipeline can least afford, so it is never treated as data.

    Each mirror gets a POST and then a GET. Measured 2026-09-24: the main
    mirror answered our POST with a 504 and a connection reset and the same
    query as a GET with 200, while the next two timed out — one try per
    mirror, POST only, meant no new bars that day. A busy answer (429/504)
    gets a short pause before the retry.
    """
    for mirror in OVERPASS_MIRRORS:
        body, status = _http(mirror, timeout=90, data=query)
        data = _overpass_answer(body, status)
        if data:
            return data
        if status in (429, 503, 504):
            time.sleep(5)
        url = f"{mirror}?data={urllib.parse.quote(query)}"
        body2, status2 = _http(url, timeout=90)
        data = _overpass_answer(body2, status2)
        if data:
            return data
        print(f"[leadgen] overpass mirror {mirror} -> POST {status}, GET {status2}"
              + (" (200 but 0 elements: treated as a miss)" if 200 in (status, status2) else ""),
              flush=True)
        time.sleep(1)
    return None


_MULTI_SPLIT = re.compile(r"\s*(?:;|,|/|\bor\b|\|)\s*", re.I)


def first_phone(raw: Optional[str]) -> Optional[str]:
    """The first dialable, non-toll-free US number in an OSM phone tag.

    Mappers put two numbers in one field ("+1 512-555-0100; +1 512-555-0101",
    "... or ..."), and the strict validator, handed the whole string, rejected
    the venue outright. Each piece is validated on its own instead.
    """
    for piece in _MULTI_SPLIT.split(raw or "")[:6]:
        phone = normalize_us_phone(piece)
        if phone and not is_toll_free(phone):
            return phone
    return None


def first_website(raw: Optional[str]) -> Optional[str]:
    """The first usable URL in an OSM website tag ("a.com;b.com" happens)."""
    for piece in re.split(r"[;\s]+", (raw or "").strip())[:4]:
        piece = piece.strip().strip(",")
        if "." not in piece or len(piece) > 300:
            continue
        return piece if piece.lower().startswith("http") else "http://" + piece
    return None


# Rejections a fresh look can overturn: the site was down, or a rule that has
# since been fixed. A re-harvest with changed data re-opens these; anything the
# operator or the book decided (deleted by hand, suppressed, already a lead or
# a customer, a chain by name) stays closed.
REOPENABLE = ("site unreachable", "no email found", "restaurant with no sign",
              "no sign on their own site",
              "phone not a dialable", "franchise language", "no phone on their site")


def harvest_city(city: dict) -> tuple[int, int]:
    """Pull venues for one city into the pool. Returns (seen, inserted)."""
    # Restaurants are in here deliberately, and they are most of the market.
    #
    # This used to take bar, pub and nightclub only, which is a small slice of
    # the places that pour liquor: an independent restaurant with a licence has
    # a back bar to count exactly like a tavern does, and in OSM it is tagged
    # `amenity=restaurant`. Across a seeded metro that is several times the
    # venue count — and the seeded territory was otherwise a month of calling
    # before it ran dry.
    #
    # The cost of casting wider is that most restaurants have no bar worth
    # counting. That is handled at qualify time, not here: a restaurant has to
    # SHOW a drinks programme on its own site before it can be promoted (see
    # enrich_candidate). Harvesting is cheap; promoting is what matters.
    kinds = "bar|pub|nightclub|restaurant"
    query = f"""
[out:json][timeout:90];
(
  node["amenity"~"^({kinds})$"](around:{int(city['radius_m'])},{city['lat']},{city['lon']});
  way["amenity"~"^({kinds})$"](around:{int(city['radius_m'])},{city['lat']},{city['lon']});
);
out center tags;
"""
    data = _overpass(query)
    if not data:
        # Every mirror failed. Deliberately raised rather than returned as
        # (0, 0): a silent zero would mark the city harvested, push it to the
        # back of the queue and look identical to "this city has no bars".
        # The caller counts this as an error and the city stays at the front.
        raise RuntimeError(f"all Overpass mirrors failed for {city['name']}")

    elements = data.get("elements", [])
    inserted = 0
    now = now_iso()

    with get_db() as conn:
        cursor = conn.cursor()
        for el in elements:
            tags = el.get("tags", {}) or {}
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            # A venue marked permanently closed is not a prospect, and OSM
            # says so plainly often enough to be worth checking before anything
            # else costs a request.
            if (tags.get("opening_hours") or "").strip().lower() in ("closed", "off"):
                continue
            raw_phone = " ; ".join(t for t in (tags.get("phone"), tags.get("contact:phone"),
                                               tags.get("contact:mobile")) if t)
            # Validated at the door, each number in the field on its own. An
            # unusable number can never reach the call list; a toll-free one
            # on an independent bar is a platform line.
            phone = first_phone(raw_phone)
            website = first_website(tags.get("website") or tags.get("contact:website")
                                    or tags.get("url"))
            # The website is the one thing it can't do without: it's where the
            # number gets checked and the email found. A venue with a website
            # and no phone tag is kept — its own site supplies the number
            # (judge_phone's from_site), which is better provenance than a
            # map tag ever is. In Austin that was ~180 venues out of ~530 with
            # a website, all thrown away at the door.
            if not website:
                continue

            source_ref = f"{el.get('type')}/{el.get('id')}"
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")

            # A venue seen before is REFRESHED, not skipped: a bar that changed
            # its number or website on the map kept the stale one forever, and
            # a candidate rejected because its site was down never came back.
            # Only rows nobody has acted on are touched, and a rejection only
            # re-opens when the data changed and the reason was one a fresh
            # look can overturn (REOPENABLE).
            cursor.execute("""
                INSERT INTO crm_lead_candidates AS c
                    (id, source, source_ref, name, city, state, lat, lon, phone, website,
                     amenity, raw_tags, opening_hours, tz_offset_hours, tz_name,
                     status, discovered_at)
                VALUES (%s, 'osm', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', %s)
                ON CONFLICT (source_ref) DO UPDATE SET
                    raw_tags = EXCLUDED.raw_tags,
                    opening_hours = EXCLUDED.opening_hours,
                    website = EXCLUDED.website,
                    phone = COALESCE(EXCLUDED.phone, c.phone),
                    status = CASE WHEN c.status = 'rejected'
                                   AND (c.website IS DISTINCT FROM EXCLUDED.website
                                        OR c.phone IS DISTINCT FROM COALESCE(EXCLUDED.phone, c.phone))
                                   AND lower(COALESCE(c.reject_reason, '')) ~ %s
                                  THEN 'new' ELSE c.status END
                 WHERE c.status IN ('new', 'rejected', 'retry')
                RETURNING (xmax = 0) AS inserted
            """, (
                generate_id(), source_ref, name, city["name"], city.get("state"),
                lat, lon, phone, website, tags.get("amenity"),
                json.dumps(tags)[:8000], (tags.get("opening_hours") or "").strip() or None,
                us_tz_offset(lon, city.get("state"), lat),
                us_tz_name(lon, city.get("state"), lat), now,
                "^(" + "|".join(re.escape(r) for r in REOPENABLE) + ")",
            ))
            row = cursor.fetchone()
            inserted += 1 if row and row["inserted"] else 0

        cursor.execute("""
            UPDATE crm_leadgen_cities
               SET last_harvested_at = %s,
                   harvest_runs = harvest_runs + 1,
                   candidates_found = candidates_found + %s
             WHERE id = %s
        """, (now, inserted, city["id"]))
        conn.commit()

    return len(elements), inserted


# ── Stage 2+3: enrich and qualify ───────────────────────────────────────────

# ── Is the number really theirs? ────────────────────────────────────────────
#
# The phone comes off the map, and the map ages: bars change numbers, old ones
# get reassigned to somebody's house, a mapper types in an owner's cell.
# Measured on 102 real Denver bars (2026-09-24): where the bar's own website
# listed a number, the map's disagreed about one time in five, and one map
# entry carried a Chicago area code. phones.py can only prove a number is
# SHAPED right. This proves it's theirs: a number reaches the call list only if
# the venue's own site shows it, or was taken from the site. The rule the email
# already follows — provenance, not plausibility.

PHONE_OK = ("confirmed", "from_site")

_TEL_HREF_RE = re.compile(r"""href\s{0,5}=\s{0,5}["']\s{0,5}tel:([^"']{1,60})["']""", re.I)
_JSON_PHONE_RE = re.compile(
    r'"(?:telephone|phone|phoneNumber|phone_number)"\s*:\s*"([^"]{7,40})"', re.I)
# Every repeat here is BOUNDED, and none sits beside another that can match the
# same characters. The first version of this pattern had a lazy [^>]*? before
# an optional group and a greedy [^>]* after it, which backtracks
# quadratically on a tag that runs on without a '>' — and Python's re holds
# the GIL while it grinds, so one bad page froze the whole server, product API
# included. test_phone_check.py times these against hostile input.
_ITEMPROP_PHONE_RE = re.compile(
    r"""itemprop\s{0,5}=\s{0,5}["']telephone["']([^>]{0,400})>([^<]{0,40})""", re.I)
_CONTENT_ATTR_RE = re.compile(r"""content\s{0,5}=\s{0,5}["']([^"']{0,60})["']""", re.I)
# How much of one page the phone check reads: the top and the bottom, because
# a bar's number is in its header or its footer. 800KB of page-builder output
# is what curl's cap allows, not what any number needs, and every regex below
# runs over whatever it's handed.
SITE_HTML_HEAD = 150_000
SITE_HTML_TAIL = 100_000


def _head_and_tail(html: str) -> str:
    if len(html) <= SITE_HTML_HEAD + SITE_HTML_TAIL:
        return html
    return html[:SITE_HTML_HEAD] + " " + html[-SITE_HTML_TAIL:]


def site_phones(html: Optional[str]) -> list:
    """Every dialable number a venue's own page publishes, best evidence first.

    A tel: link (a number somebody deliberately made tappable), then
    structured data — schema.org `telephone` and site builders' own JSON, read
    even inside <script> because it's the venue's own listing, unlike the
    developer placeholders that keep emails out of scripts — then the visible
    text. Toll-free lines are left out: on one bar they're a platform or a
    head office, never the bar.
    """
    html = _head_and_tail(html or "")
    raw = [urllib.parse.unquote(t) for t in _TEL_HREF_RE.findall(html)]
    raw += _JSON_PHONE_RE.findall(html)
    for attrs, text in _ITEMPROP_PHONE_RE.findall(html):
        content = _CONTENT_ATTR_RE.search(attrs)
        raw += [content.group(1) if content else "", text]
    visible = re.sub(r"<[^<>]{0,2000}>", " ", strip_non_content(html))
    raw += _PHONE_IN_TEXT.findall(visible)
    out: list = []
    for r in raw:
        digits = normalize_us_phone(r) if r else None
        if digits and not is_toll_free(digits) and digits not in out:
            out.append(digits)
    return out


def local_area_codes(phones) -> set:
    """The area codes a metro's bars actually use, read off every candidate
    harvested there, so there's no area-code table to keep current: Denver's
    come out as 303 and 720 because that's what Denver's bars list. With too
    few candidates to tell it's empty, and the map number's own code is all
    that counts as local."""
    counts = Counter(p[:3] for p in phones if p and len(p) == 10)
    total = sum(counts.values())
    if total < 20:
        return set()
    return {code for code, n in counts.items() if n >= max(2, total * 0.02)}


def judge_phone(map_phone: Optional[str], site_numbers: list, local_codes=()) -> dict:
    """Which number may be dialled, and why: {phone, status, note}.

    confirmed    the map's number is on their site.
    from_site    it isn't, but the site shows exactly one local number: that
                 one. A venue keeps its own site current; nobody keeps its map
                 entry current.
    conflict     the site shows numbers, but not the map's and not exactly one
                 local one — another location's line, a group office, two we
                 can't choose between. Not dialled.
    unconfirmed  the site shows no number at all. Not dialled either: an
                 unchecked map number is wrong about one time in five.
    """
    local = set(local_codes or ())
    if map_phone:
        local.add(map_phone[:3])
    if map_phone and map_phone in site_numbers:
        return {"phone": map_phone, "status": "confirmed", "note": None}
    on_site = [p for p in site_numbers if p[:3] in local]
    if len(on_site) == 1:
        return {"phone": on_site[0], "status": "from_site",
                "note": (f"Map listed {format_us_phone_dashed(map_phone)}; their "
                         f"website lists {format_us_phone_dashed(on_site[0])}" if map_phone
                         else f"Their website lists {format_us_phone_dashed(on_site[0])}")}
    if site_numbers:
        listed = ", ".join(format_us_phone_dashed(p) for p in site_numbers[:3])
        return {"phone": map_phone, "status": "conflict",
                "note": (f"Their website lists {listed}, not the map's "
                         f"{format_us_phone_dashed(map_phone)}" if map_phone
                         else f"Their website lists {listed}; can't tell which is this bar")}
    return {"phone": map_phone, "status": "unconfirmed",
            "note": ("Their website shows no phone number to check it against" if map_phone
                     else "No phone number on the map or their website")}


def _contact_urls(website: str, home: str) -> list:
    """The venue's own contact-ish links, then the usual guessed paths."""
    urls: list = []
    for href, _kind in CONTACT_LINK_RE.findall(home or ""):
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        full = urllib.parse.urljoin(website, href)
        # http(s) only — urljoin will happily carry a file:// or data: href
        # straight through from the page.
        if not full.lower().startswith(("http://", "https://")):
            continue
        # Stay on the venue's own site; an off-site link is a social profile
        # or a booking platform, not their contact page.
        if domain_of(full) != domain_of(website):
            continue
        if full not in urls:
            urls.append(full)
    for path in CONTACT_PATHS:
        full = website.rstrip("/") + path
        if full not in urls:
            urls.append(full)
    return urls


def find_site_phones(website: str, map_phone: Optional[str] = None,
                     budget: int = 3) -> tuple[list, bool]:
    """(numbers the venue's site publishes, whether it loaded at all): the
    homepage, then contact pages until the map's number turns up or `budget`
    pages are spent."""
    home, status = _http(website, timeout=PAGE_TIMEOUT, verify_public=True)
    if status != 200 or not home:
        return [], False
    numbers = site_phones(home)
    for url in _contact_urls(website, home)[:budget]:
        if map_phone and map_phone in numbers:
            break
        body, status = _http(url, timeout=PAGE_TIMEOUT, verify_public=True)
        if status == 200 and body:
            numbers += [p for p in site_phones(body) if p not in numbers]
    return numbers, True


def _local_codes_by_city(cursor, cities) -> dict:
    cities = sorted({c for c in cities if c})
    if not cities:
        return {}
    cursor.execute("SELECT city, phone FROM crm_lead_candidates WHERE city = ANY(%s)",
                   (cities,))
    by_city: dict = {}
    for r in cursor.fetchall():
        by_city.setdefault(r["city"], []).append(r["phone"])
    return {c: local_area_codes(p) for c, p in by_city.items()}


_verify_lock = threading.Lock()


# The re-check is a background chore sharing a 0.5-CPU box with the product
# API, so it goes slowly on purpose: a small batch, two sites at a time, and a
# deadline after which no new site is started. What's left waits for the next
# batch; nothing is lost by being late, because an unchecked lead just isn't
# offered for dialling yet.
VERIFY_WORKERS = max(1, int(os.getenv("LEADGEN_VERIFY_WORKERS", "2")))
VERIFY_BATCH = max(1, int(os.getenv("LEADGEN_VERIFY_BATCH", "20")))
VERIFY_BUDGET_S = max(10, int(os.getenv("LEADGEN_VERIFY_BUDGET_S", "90")))


def verify_phones(lead_limit: int = VERIFY_BATCH, bank_limit: int = VERIFY_BATCH,
                  budget_s: int = VERIFY_BUDGET_S) -> dict:
    """Check numbers taken off the map before the website check existed.

    Never-called leads on the call list first. One whose number their site
    shows is marked confirmed; one their site corrects gets the site's number,
    with the map's kept in the notes; one their site can't vouch for goes back
    to the bank, off the call list, so a checked lead is promoted into its
    place. Worked leads are left alone — whoever rang them already knows. Then
    banked candidates, best first, so the next promotions are checked too.

    Only rows with no phone_status are looked at, so it costs nothing once
    done, and every write re-checks that the row is still unworked: the
    operator may ring a lead in the minutes the crawl takes.

    One BATCH per call, not everything: `lead_limit` + `bank_limit` rows at
    most, and no site started after `budget_s` seconds. Rows not reached keep
    phone_status NULL and are picked up by the next batch.
    """
    if not _verify_lock.acquire(blocking=False):
        return {"skipped": "a check is already running"}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT l.id AS lead_id, c.id, c.city, c.website, c.phone
                  FROM crm_leads l
                  JOIN crm_lead_candidates c ON c.promoted_lead_id = l.id
                 WHERE l.source = 'leadgen' AND l.phone_status IS NULL
                   AND l.status = 'new' AND l.last_touch_at IS NULL
                 LIMIT %s
            """, (lead_limit,))
            leads = [dict(r) for r in cursor.fetchall()]
            cursor.execute("""
                SELECT id, city, website, phone FROM crm_lead_candidates
                 WHERE status = 'qualified' AND phone_status IS NULL
                 ORDER BY score DESC LIMIT %s
            """, (bank_limit,))
            bank = [dict(r) for r in cursor.fetchall()]
            codes = _local_codes_by_city(cursor, [r["city"] for r in leads + bank])

        deadline = time.monotonic() + budget_s

        def check(row):
            if time.monotonic() > deadline:
                return row, None
            map_phone = normalize_us_phone(row.get("phone"))
            try:
                numbers, loaded = find_site_phones(row["website"], map_phone)
            except Exception:
                numbers, loaded = [], False
            verdict = judge_phone(map_phone, numbers, codes.get(row.get("city"), set()))
            if not loaded:
                verdict = {**verdict, "status": "unconfirmed",
                           "note": "Their website didn't load when the number was checked"}
            return row, verdict

        with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
            lead_results = [r for r in pool.map(check, leads) if r[1]]
            bank_results = [r for r in pool.map(check, bank) if r[1]]

        tally: Counter = Counter()
        now = now_iso()
        with get_db() as conn:
            cursor = conn.cursor()
            for row, v in lead_results:
                tally[v["status"]] += 1
                _apply_lead_verdict(cursor, row, v, now, tally)
            for row, v in bank_results:
                cursor.execute("""
                    UPDATE crm_lead_candidates
                       SET phone = %s, phone_status = %s, phone_note = %s
                     WHERE id = %s AND phone_status IS NULL
                """, (v["phone"] or row["phone"], v["status"], v["note"], row["id"]))
            conn.commit()
        out = {"leads_checked": len(lead_results), "bank_checked": len(bank_results),
               **dict(tally)}
        print(f"[leadgen] LEADGEN_PHONES_VERIFIED {out}", flush=True)
        return out
    finally:
        _verify_lock.release()


def _apply_lead_verdict(cursor, row: dict, v: dict, now: str, tally: Counter) -> None:
    """One call-list lead's verdict. Every write is conditional on the lead
    still being unchecked and unworked."""
    unworked = ("phone_status IS NULL AND status = 'new' AND last_touch_at IS NULL")
    if v["status"] in PHONE_OK:
        if v["status"] == "from_site":
            cursor.execute(f"""
                UPDATE crm_leads
                   SET phone = %s, phone_status = %s, phone_note = %s, updated_at = %s,
                       notes = COALESCE(notes || E'\\n', '') || %s
                 WHERE id = %s AND {unworked}
            """, (v["phone"], v["status"], v["note"], now,
                  f"[{now[:10]}] Phone corrected from their website — {v['note']}",
                  row["lead_id"]))
        else:
            cursor.execute(f"UPDATE crm_leads SET phone_status = %s WHERE id = %s AND {unworked}",
                           (v["status"], row["lead_id"]))
        cursor.execute("UPDATE crm_lead_candidates SET phone = %s, phone_status = %s, "
                       "phone_note = %s WHERE id = %s", (v["phone"], v["status"], v["note"], row["id"]))
        return
    # Their site can't vouch for it: off the call list and back to the bank.
    # Not if an email is queued to them — that row stays, and the call list
    # hides it by its status instead.
    cursor.execute(f"DELETE FROM crm_leads WHERE id = %s AND {unworked} "
                   "AND queued_email_at IS NULL", (row["lead_id"],))
    if cursor.rowcount:
        cursor.execute("""
            UPDATE crm_lead_candidates
               SET status = 'qualified', promoted_lead_id = NULL, promoted_at = NULL,
                   phone_status = %s, phone_note = %s
             WHERE id = %s
        """, (v["status"], v["note"], row["id"]))
        tally["off_call_list"] += 1
    else:
        cursor.execute(f"UPDATE crm_leads SET phone_status = %s, phone_note = %s "
                       f"WHERE id = %s AND {unworked}", (v["status"], v["note"], row["lead_id"]))


def phone_check_step() -> int:
    """One background batch; how many rows it checked (0 = nothing left).
    Call-list leads before banked candidates: an unchecked lead is hidden from
    the call list, so those are the ones someone is waiting on."""
    out = _verify_phones_safe(bank_limit=0) or {}
    if not out.get("leads_checked") and not out.get("skipped"):
        out = _verify_phones_safe(lead_limit=0) or {}
    return out.get("leads_checked", 0) + out.get("bank_checked", 0)


def _verify_phones_safe(**kw) -> Optional[dict]:
    try:
        return verify_phones(**kw)
    except Exception as exc:
        print(f"[leadgen] LEADGEN_PHONES_VERIFY_FAILED {exc}", flush=True)


# ── The owner's rules, applied to what's already banked or listed ─────────

def _reconcile_owner_rules(cursor) -> tuple[int, int]:
    """Every boot: never-called leads and banked candidates on a tourist strip
    or run by a chain come off the list / out of the bank. Map data and the
    harvest only — no crawling, so it's cheap and safe at boot. Called and
    emailed leads are never touched; a lead with an email queued stays but is
    hidden from the call list. Returns (leads removed, candidates rejected)."""
    corporate = corporate_index(cursor)
    cursor.execute("""
        SELECT c.id, c.city, c.lat, c.lon, c.website, c.raw_tags, c.status,
               l.id AS lead_id, l.queued_email_at
          FROM crm_lead_candidates c
          LEFT JOIN crm_leads l ON l.id = c.promoted_lead_id
         WHERE c.status = 'qualified'
            OR (c.status = 'promoted' AND l.source = 'leadgen' AND l.status = 'new'
                AND l.last_touch_at IS NULL)
    """)
    removed = rejected = 0
    for row in cursor.fetchall():
        tags = _cand_tags(row)
        strip = tourist_strip(tags, row["city"], row["lat"], row["lon"])
        why = f"tourist strip ({strip})" if strip else corporate_reason(
            tags, row["website"], corporate)
        if not why:
            continue
        if row["status"] == "promoted":
            cursor.execute("""
                DELETE FROM crm_leads WHERE id = %s AND status = 'new'
                   AND last_touch_at IS NULL AND queued_email_at IS NULL
            """, (row["lead_id"],))
            if not cursor.rowcount:
                cursor.execute("UPDATE crm_leads SET fit_status = 'blocked', fit_note = %s "
                               "WHERE id = %s", (why, row["lead_id"]))
                continue
            removed += 1
        else:
            rejected += 1
        cursor.execute("""
            UPDATE crm_lead_candidates
               SET status = 'rejected', reject_reason = %s, fit_status = 'blocked',
                   fit_note = %s, promoted_lead_id = NULL, promoted_at = NULL
             WHERE id = %s
        """, (why, why, row["id"]))
    return removed, rejected


_fit_lock = threading.Lock()


def check_fit(row: dict, corporate: Optional[dict] = None) -> dict:
    """The owner's rules for one venue, crawling its homepage and drinks pages:
    {"status": ok | blocked | unreachable, "note": evidence or reason}."""
    tags = _cand_tags(row)
    strip = tourist_strip(tags, row.get("city"), row.get("lat"), row.get("lon"))
    if strip:
        return {"status": "blocked", "note": f"tourist strip ({strip})"}
    why = corporate_reason(tags, row.get("website"), corporate)
    if why:
        return {"status": "blocked", "note": why}
    home, website, status = _fetch_site(row.get("website") or "")
    if not home:
        return {"status": "unreachable", "note": f"site unreachable (HTTP {status})"}
    text = visible_text(home[:200000])
    chain = looks_like_chain(row.get("name") or "", website, text)
    if chain:
        return {"status": "blocked", "note": chain}
    name, amenity = row.get("name") or "", (row.get("amenity") or "").lower()
    verdict = liquor_verdict(text, tags, name, amenity)
    if verdict["status"] == "unknown":
        for url in _drink_links(website, home, limit=3):
            body, code = _http(url, timeout=PAGE_TIMEOUT, verify_public=True)
            if _ok(code, body):
                text += " \n" + visible_text(body[:200000])
                verdict = liquor_verdict(text, tags, name, amenity)
                if verdict["status"] != "unknown":
                    break
    if verdict["status"] == "spirits":
        return {"status": "ok", "note": _evidence_note(verdict)}
    return {"status": "blocked", "note": verdict["reason"]}


def _in_window_now(row: dict) -> bool:
    """Whether the call list would show this lead this minute — those are the
    ones someone is waiting on, so they're checked first."""
    from zoneinfo import ZoneInfo
    from callwindow import call_window
    try:
        local = (datetime.now(ZoneInfo(row["tz_name"])) if row.get("tz_name") else
                 datetime.now(timezone.utc) + timedelta(hours=row.get("tz_offset_hours") or 0))
        return bool(call_window(row.get("opening_hours"), local)["good_now"])
    except Exception:
        return False


def verify_fit(lead_limit: int = VERIFY_BATCH, bank_limit: int = VERIFY_BATCH,
               budget_s: int = VERIFY_BUDGET_S) -> dict:
    """Apply the owner's rules to leads and candidates qualified before them.

    Never-called call-list leads first — the ones in a calling window this
    minute before the rest — then the bank's best. Until checked, a generated
    lead isn't offered for dialling (crm._fit_ok), so nothing unchecked is
    ever called. Passes are stamped 'ok' with the evidence; a fail comes off
    the call list (its candidate rejected, with the reason); a site that
    doesn't load goes back to the bank to be re-crawled from scratch. One
    small batch per call, same pace as verify_phones: it shares a 0.5-CPU box
    with the product API."""
    if not _fit_lock.acquire(blocking=False):
        return {"skipped": "a check is already running"}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with get_db() as conn:
            cursor = conn.cursor()
            leads: list = []
            if lead_limit:
                cursor.execute("""
                    SELECT l.id AS lead_id, l.tz_name, l.tz_offset_hours, l.opening_hours,
                           c.id, c.name, c.city, c.lat, c.lon, c.website, c.raw_tags,
                           c.amenity
                      FROM crm_leads l
                      JOIN crm_lead_candidates c ON c.promoted_lead_id = l.id
                     WHERE l.source = 'leadgen' AND l.fit_status IS NULL
                       AND l.status = 'new' AND l.last_touch_at IS NULL
                """)
                leads = [dict(r) for r in cursor.fetchall()]
                leads.sort(key=lambda r: not _in_window_now(r))
                leads = leads[:lead_limit]
            bank: list = []
            if bank_limit:
                cursor.execute("""
                    SELECT id, name, city, lat, lon, website, raw_tags, amenity
                      FROM crm_lead_candidates
                     WHERE status = 'qualified' AND fit_status IS NULL
                     ORDER BY score DESC LIMIT %s
                """, (bank_limit,))
                bank = [dict(r) for r in cursor.fetchall()]
            corporate = corporate_index(cursor) if (leads or bank) else {}

        deadline = time.monotonic() + budget_s

        def check(row):
            if time.monotonic() > deadline:
                return row, None
            try:
                return row, check_fit(row, corporate)
            except Exception as exc:
                return row, {"status": "unreachable", "note": f"check failed: {exc}"[:200]}

        with ThreadPoolExecutor(max_workers=VERIFY_WORKERS) as pool:
            lead_results = [r for r in pool.map(check, leads) if r[1]]
            bank_results = [r for r in pool.map(check, bank) if r[1]]

        tally: Counter = Counter()
        retry_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with get_db() as conn:
            cursor = conn.cursor()
            for row, v in lead_results:
                tally["lead_" + v["status"]] += 1
                _apply_fit_to_lead(cursor, row, v, retry_at)
            for row, v in bank_results:
                tally["bank_" + v["status"]] += 1
                _apply_fit_to_candidate(cursor, row["id"], v, retry_at)
            conn.commit()
        out = {"leads_checked": len(lead_results), "bank_checked": len(bank_results),
               **dict(tally)}
        print(f"[leadgen] LEADGEN_FIT_CHECKED {out}", flush=True)
        return out
    finally:
        _fit_lock.release()


def _apply_fit_to_candidate(cursor, cand_id: str, v: dict, retry_at: str) -> None:
    if v["status"] == "ok":
        cursor.execute("UPDATE crm_lead_candidates SET fit_status = 'ok', fit_note = %s "
                       "WHERE id = %s", (v["note"], cand_id))
    elif v["status"] == "blocked":
        cursor.execute("""
            UPDATE crm_lead_candidates
               SET status = 'rejected', reject_reason = %s, fit_status = 'blocked',
                   fit_note = %s, promoted_lead_id = NULL, promoted_at = NULL
             WHERE id = %s
        """, (v["note"], v["note"], cand_id))
    else:
        # Back to the crawl queue: the next daily run re-enriches it under
        # today's rules, or rejects it after ENRICH_TRIES.
        cursor.execute("""
            UPDATE crm_lead_candidates
               SET status = 'retry', reject_reason = %s, retry_after = %s,
                   promoted_lead_id = NULL, promoted_at = NULL
             WHERE id = %s
        """, (v["note"], retry_at, cand_id))


def _apply_fit_to_lead(cursor, row: dict, v: dict, retry_at: str) -> None:
    """Every write re-checks the lead is still unworked and unchecked — the
    operator may ring it in the minute the crawl takes."""
    unworked = "status = 'new' AND last_touch_at IS NULL AND fit_status IS NULL"
    if v["status"] == "ok":
        cursor.execute(f"UPDATE crm_leads SET fit_status = 'ok', fit_note = %s "
                       f"WHERE id = %s AND {unworked}", (v["note"], row["lead_id"]))
        _apply_fit_to_candidate(cursor, row["id"], v, retry_at)
        return
    cursor.execute(f"DELETE FROM crm_leads WHERE id = %s AND {unworked} "
                   "AND queued_email_at IS NULL", (row["lead_id"],))
    if cursor.rowcount:
        _apply_fit_to_candidate(cursor, row["id"], v, retry_at)
    else:
        # Worked meanwhile, or an email is queued to them: kept, but never
        # offered for dialling.
        cursor.execute(f"UPDATE crm_leads SET fit_status = 'blocked', fit_note = %s "
                       f"WHERE id = %s AND {unworked}", (v["note"], row["lead_id"]))


def fit_check_step() -> int:
    """One background batch of verify_fit; how many rows it checked."""
    try:
        out = verify_fit(bank_limit=0)
        if not out.get("leads_checked") and not out.get("skipped"):
            out = verify_fit(lead_limit=0)
    except Exception as exc:
        print(f"[leadgen] LEADGEN_FIT_CHECK_FAILED {exc}", flush=True)
        return 0
    return out.get("leads_checked", 0) + out.get("bank_checked", 0)


# A homepage that didn't answer for one of these reasons is tried again on a
# later run rather than rejected: a site down for an afternoon, a rate limit,
# a bot wall having a bad day, a network blip. Measured on 90 Austin venues,
# 21 were "unreachable", and one of them loaded on the very next try — every
# one of those used to be thrown away for good.
TRANSIENT_STATUSES = {0, -1, 403, 408, 425, 429, 500, 502, 503, 504,
                      520, 521, 522, 523, 524, 525, 526}
ENRICH_TRIES = 3                    # then it really is rejected
RETRY_DAYS = (1, 3)                 # after the 1st and 2nd failure


def _ok(status: int, body: str) -> bool:
    # Any 2xx with a body: a host answering 202 with the page in it was
    # rejected as "unreachable (HTTP 202)".
    return 200 <= status < 300 and bool((body or "").strip())


def _site_root(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}/"


def _fetch_site(website: str) -> tuple[str, str, int]:
    """(homepage html, the URL that answered, status). html is '' when
    nothing did, and the status says why.

    Three recoveries a browser makes without anyone noticing:
    - the map's link is a stale deep page (404) → the site's own root;
    - a certificate the browser would accept (it fetches missing
      intermediates) but curl won't → plain http://, then reading the public
      page without the certificate check. Reading only: nothing is sent.
    """
    body, status = _http(website, timeout=PAGE_TIMEOUT, verify_public=True, tls_status=True)
    if _ok(status, body):
        return body, website, status
    root = _site_root(website)
    if status in (404, 410) and root.rstrip("/") != website.rstrip("/"):
        body2, status2 = _http(root, timeout=PAGE_TIMEOUT, verify_public=True)
        if _ok(status2, body2):
            return body2, root, status2
    if status == -1:
        if website.lower().startswith("https://"):
            plain = "http://" + website[len("https://"):]
            body2, status2 = _http(plain, timeout=PAGE_TIMEOUT, verify_public=True)
            if _ok(status2, body2):
                return body2, plain, status2
        body2, status2 = _http(website, timeout=PAGE_TIMEOUT, verify_public=True, insecure=True)
        if _ok(status2, body2):
            return body2, website, status2
    return "", website, status


_DRINK_LINK_RE = re.compile(
    r'href\s{0,5}=\s{0,5}["\']([^"\'#\s]{1,200})["\'][^<>]{0,300}>([^<]{0,80})', re.I)
_DRINK_WORDS = re.compile(r"drink|cocktail|bar[-_ ]?menu|wine|beer|happy[-_ ]?hour|spirits|"
                          r"libation|\bmenus?\b", re.I)


def _drink_links(website: str, home: str, limit: int = 2) -> list:
    """The site's own drinks / menu pages, drinks first. Most restaurants keep
    the cocktail list on a menu page, not the homepage, and the drinks gate
    only ever read the homepage and contact pages."""
    found: list = []
    for href, text in _DRINK_LINK_RE.findall(home or ""):
        if not (_DRINK_WORDS.search(href) or _DRINK_WORDS.search(text)):
            continue
        full = urllib.parse.urljoin(website, href)
        if (not full.lower().startswith(("http://", "https://"))
                or domain_of(full) != domain_of(website)
                or re.search(r"\.(pdf|jpe?g|png|gif|webp)(\?|$)", full, re.I)
                or full in found):
            continue
        found.append(full)
    found.sort(key=lambda u: 0 if re.search(r"drink|cocktail|bar|wine|beer", u, re.I) else 1)
    return found[:limit]


def _rejected(reason: str, status: str = "rejected") -> dict:
    return {"status": status, "reject_reason": reason,
            "email": None, "email_source": None, "email_kind": None,
            "manager_name": None, "manager_role": None,
            "manager_source": None, "manager_seen_at": None,
            "venue_facts": None, "opener": None, "score": 0,
            "fit_status": None, "fit_note": None}


def enrich_candidate(cand: dict) -> dict:
    """Crawl the venue's site for an email, then judge it. Never raises.

    Homepage first, and if the homepage doesn't load the site is treated as
    dead immediately. That early exit matters more than anything else here: a
    parked or broken domain used to cost one request per guessed path before
    being abandoned, which is most of a minute spent proving nothing.

    When the homepage does load, its own links are followed in preference to
    guessed paths — a link the venue wrote pointing at "Contact" beats any
    assumption about their URL scheme.
    """
    website = cand["website"]
    html_seen = ""
    pages: list[tuple[str, str]] = []      # (url, html) for the manager scan
    email = None
    email_source = None
    fetched = 0

    # Free win first: the map entry itself sometimes carries a contact address.
    try:
        osm_tags = json.loads(cand.get("raw_tags") or "{}")
    except json.JSONDecodeError:
        osm_tags = {}

    # The owner's exclusions that need no crawl: a main tourist strip, or a
    # brand / operator that is a known chain. Decided before a single request.
    strip = tourist_strip(osm_tags, cand.get("city"), cand.get("lat"), cand.get("lon"))
    if strip:
        return _rejected(f"tourist strip ({strip})")
    corporate = corporate_reason(osm_tags, website, None)
    if corporate:
        return _rejected(corporate)
    tagged = (osm_tags.get("email") or osm_tags.get("contact:email") or "").strip()
    if tagged and not EMAIL_BLOCKLIST.search(tagged) and "@" in tagged:
        email, email_source = tagged.lower(), "OpenStreetMap tag"

    home, website, status = _fetch_site(website)
    fetched += 1
    if not home:
        # 'retry' for a failure a later run can get past; run_daily counts the
        # tries and rejects only after ENRICH_TRIES.
        return _rejected(f"site unreachable (HTTP {status})",
                         "retry" if status in TRANSIENT_STATUSES else "rejected")

    html_seen += home[:200000]
    pages.append((website, home))
    if not email:
        emails = extract_emails(home)
        if emails:
            email, email_source = emails[0], website

    if not email:
        for url in _contact_urls(website, home):
            if fetched >= MAX_PAGES_PER_SITE:
                break
            body, status = _http(url, timeout=PAGE_TIMEOUT, verify_public=True)
            fetched += 1
            if status != 200 or not body:
                continue
            html_seen += body[:200000]
            pages.append((url, body))
            found = extract_emails(body)
            if found:
                email, email_source = found[0], url
                break

    # A name to ask for, from pages already fetched. Measured across 22
    # reachable bar sites, one in twenty publishes one — so this is a bonus
    # and never a reason to spend more requests than are already spare.
    manager = None
    for url, body in pages:
        manager = find_manager(body, url)
        if manager:
            break
    if not manager and email and fetched < MAX_PAGES_PER_SITE:
        seen_urls = {u for u, _ in pages}
        for path in TEAM_PATHS[:2]:
            if fetched >= MAX_PAGES_PER_SITE:
                break
            url = website.rstrip("/") + path
            if url in seen_urls:
                continue
            body, status = _http(url, timeout=PAGE_TIMEOUT, verify_public=True)
            fetched += 1
            if status != 200 or not body:
                continue
            html_seen += body[:200000]
            manager = find_manager(body, url)
            if manager:
                break

    # Is the map's number theirs? The pages already fetched first; a contact
    # page or two more only if it hasn't turned up and there's budget left.
    numbers: list = []
    for _url, body in pages:
        numbers += [p for p in site_phones(body) if p not in numbers]
    if cand.get("phone") not in numbers:
        seen_urls = {u for u, _ in pages}
        for url in _contact_urls(website, home):
            if cand.get("phone") in numbers or fetched >= MAX_PAGES_PER_SITE:
                break
            if url in seen_urls:
                continue
            body, status = _http(url, timeout=PAGE_TIMEOUT, verify_public=True)
            fetched += 1
            if status == 200 and body:
                numbers += [p for p in site_phones(body) if p not in numbers]
    verdict = judge_phone(cand.get("phone"), numbers, cand.get("local_codes") or ())

    tags = osm_tags
    # Franchise language is read from what a visitor sees, not from scripts:
    # an ordering widget's "find a location" is not the venue talking.
    seen_text = " \n".join(visible_text(body[:200000]) for _url, body in pages)
    chain_reason = looks_like_chain(cand["name"], website, seen_text)
    if chain_reason:
        return _rejected(chain_reason)

    # Must pour LIQUOR, shown on its own site — bars included: a wine bar or a
    # taproom is tagged a bar too. The drinks are usually on a menu page, so
    # those are read before deciding (see liquor_verdict).
    amenity = (cand.get("amenity") or "").lower()
    liquor = liquor_verdict(seen_text, tags, cand.get("name") or "", amenity)
    if liquor["status"] == "unknown":
        for url in _drink_links(website, home, limit=3):
            if fetched >= MAX_PAGES_PER_SITE + 3:
                break
            body, status = _http(url, timeout=PAGE_TIMEOUT, verify_public=True)
            fetched += 1
            if _ok(status, body):
                html_seen += body[:200000]
                seen_text += " \n" + visible_text(body[:200000])
                liquor = liquor_verdict(seen_text, tags, cand.get("name") or "", amenity)
                if liquor["status"] != "unknown":
                    break
    if liquor["status"] != "spirits":
        return _rejected(liquor["reason"])

    # No email is no longer a rejection. It's a CALL list: a bar whose own
    # website vouches for its number is worth ringing whether or not it
    # publishes an address, and on Austin 15 of 90 venues were thrown away for
    # this alone. It still sorts below a lead with an email (score_candidate
    # gives an email +3 to +7), and a number the site can't vouch for is still
    # never promoted (PHONE_OK).

    return {
        "status": "qualified",
        "reject_reason": None,
        # The URL that actually answered: the map's stale deep link, or http
        # where https failed, is replaced by one that works.
        "website": website,
        # Qualified whatever the verdict — banked, not thrown away — but only
        # PHONE_OK numbers are ever promoted to the call list.
        "phone": verdict["phone"],
        "phone_status": verdict["status"],
        "phone_note": verdict["note"],
        "email": email,
        "email_source": email_source,
        "email_kind": email_kind(email),
        "manager_name": manager["name"] if manager else None,
        "manager_role": manager["role"] if manager else None,
        "manager_source": manager["source"] if manager else None,
        "manager_seen_at": now_iso() if manager else None,
        "opener": opener_line(html_seen, cand.get("amenity")),
        "venue_facts": venue_facts.dumps(dict(
            venue_facts.extract_facts(tags, html_seen, cand.get("opening_hours")),
            # The same signals scoring uses, kept rather than thrown away: they
            # change how the call OPENS, not just where the lead sorts. Walking
            # into "what are you using now?" without knowing their site runs
            # Toast is how you get told something you could have read.
            **_stack_signals(html_seen))),
        "score": score_candidate(tags, email, html_seen, manager, cand.get("city")),
        # Passed the owner's rules; the note is the liquor evidence, shown on
        # the prep sheet so the operator can see why it's on the list.
        "fit_status": "ok",
        "fit_note": _evidence_note(liquor),
    }


def _evidence_note(liquor: dict) -> str:
    return "Pours liquor — their site: " + ", ".join(
        f"\u201c{e}\u201d" for e in liquor.get("evidence") or [])


def _record_retry(cand: dict, reason: str) -> None:
    """A site that didn't load: try again later, or give up after
    ENRICH_TRIES."""
    tries = (cand.get("enrich_attempts") or 0) + 1
    with get_db() as conn:
        cursor = conn.cursor()
        if tries >= ENRICH_TRIES:
            cursor.execute("""
                UPDATE crm_lead_candidates
                   SET status = 'rejected', reject_reason = %s, enrich_attempts = %s,
                       enriched_at = %s
                 WHERE id = %s
            """, (f"{reason}, {tries} tries", tries, now_iso(), cand["id"]))
        else:
            wait = RETRY_DAYS[min(tries - 1, len(RETRY_DAYS) - 1)]
            cursor.execute("""
                UPDATE crm_lead_candidates
                   SET status = 'retry', reject_reason = %s, enrich_attempts = %s,
                       retry_after = %s
                 WHERE id = %s
            """, (reason, tries,
                  (datetime.now(timezone.utc) + timedelta(days=wait)).isoformat(), cand["id"]))
        conn.commit()


def _enrich_safe(cand: dict) -> Optional[dict]:
    """enrich_candidate for use in a thread pool — returns None on failure."""
    try:
        return enrich_candidate(cand)
    except Exception as exc:
        print(f"[leadgen] enrich failed for {cand.get('name')}: {exc}", flush=True)
        return None


def _is_suppressed(cursor, name: str, email: str, phone: str, website: str) -> Optional[str]:
    domain = domain_of(website)
    checks = [("email", email), ("phone", phone), ("domain", domain), ("name", name)]
    for kind, value in checks:
        if not value:
            continue
        cursor.execute(
            "SELECT reason FROM crm_suppressions WHERE kind = %s AND LOWER(value) = LOWER(%s)",
            (kind, value),
        )
        row = cursor.fetchone()
        if row:
            return row["reason"] or f"suppressed {kind}"
    return None


# ── Stage 4: promote ────────────────────────────────────────────────────────

def _cand_tags(cand: dict) -> dict:
    try:
        return json.loads(cand.get("raw_tags") or "{}")
    except (TypeError, ValueError):
        return {}


def _promote_one(cursor, cand: dict, now: str, corporate: Optional[dict] = None) -> Optional[str]:
    """Promote a single candidate, or reject it and return None.

    Split out of the loop so the bucket filler can walk past a candidate that
    turns out to be suppressed or a duplicate and try the next one in the same
    cell, instead of leaving that cell short.
    """
    # The owner's rules, checked again at the last gate: the strip list and
    # the chain index both grow after a candidate was crawled.
    tags = _cand_tags(cand)
    blocked = tourist_strip(tags, cand.get("city"), cand.get("lat"), cand.get("lon"))
    blocked = f"tourist strip ({blocked})" if blocked else corporate_reason(
        tags, cand.get("website"), corporate)
    if blocked:
        cursor.execute("UPDATE crm_lead_candidates SET status='rejected', reject_reason=%s, "
                       "fit_status='blocked', fit_note=%s WHERE id=%s",
                       (blocked, blocked, cand["id"]))
        return None
    # Re-checked even though harvest validates: rows banked by an older build
    # predate the validator, and this is the last gate before a number reaches
    # a dialer.
    clean_phone = normalize_us_phone(cand["phone"])
    if not clean_phone or is_toll_free(clean_phone):
        cursor.execute(
            "UPDATE crm_lead_candidates SET status='rejected', "
            "reject_reason='phone not a dialable US number' WHERE id=%s",
            (cand["id"],))
        return None
    cand = {**cand, "phone": clean_phone}

    # Provenance, not just plausibility. Every address this pipeline produces
    # records the page it was read off; one that doesn't was never crawled, so
    # nothing can vouch for it. Fourteen rows in the test database carried a
    # `bank<uuid>@test.com` with no source and no enrichment timestamp, and
    # sailed all the way to the top of the call list — because "has an email"
    # was the only test standing in the way.
    #
    # An unsourced address is therefore disqualifying on its own, whatever it
    # looks like. The blocklist catches the shapes we've seen; this catches the
    # ones we haven't.
    #
    # The ADDRESS is what goes, not the lead: since a bar with no email can be
    # a call-only lead, an address nothing vouches for is simply dropped and
    # the venue — whose number its own site confirmed — is still promoted.
    if cand.get("email") and (not (cand.get("email_source") or "").strip()
                              or EMAIL_BLOCKLIST.search(cand["email"])):
        cand = {**cand, "email": None, "email_source": None, "email_kind": None}

    reason = _is_suppressed(cursor, cand["name"], cand["email"],
                            cand["phone"], cand["website"])
    if reason:
        cursor.execute(
            "UPDATE crm_lead_candidates SET status='rejected', reject_reason=%s WHERE id=%s",
            (reason, cand["id"]),
        )
        return None

    # Already in the pipeline under this email or name? Never pitch the same
    # bar twice — this is the check standing between the operator and ringing
    # a restaurant they already called.
    #
    # It was comparing crm_leads.loc against the candidate's CITY, but loc is
    # written as "City, ST". "portland" never equals "portland, or", so the
    # name half of this test has never once matched and only the email half
    # was doing any work — a venue that changed its published address between
    # harvests came straight back onto the list. Build the same string the
    # INSERT below uses and compare that.
    loc = ", ".join(x for x in [cand["city"], cand["state"]] if x)
    cursor.execute(
        "SELECT id FROM crm_leads WHERE (%s IS NOT NULL AND LOWER(email) = LOWER(%s)) "
        "OR (LOWER(name) = LOWER(%s) AND LOWER(COALESCE(loc,'')) = LOWER(%s))",
        (cand["email"], cand["email"], cand["name"], loc),
    )
    already = cursor.fetchone()
    if not already:
        # The same phone and the same name is the same bar, however the map
        # spells it. Email and exact name+town missed "Olde Town Tavern"
        # already in the book as "Olde Town Tavern & Grill", and it went
        # straight back onto the call list. A shared phone alone isn't enough:
        # one owner can run two bars off one number.
        cursor.execute(
            "SELECT name FROM crm_leads "
            "WHERE RIGHT(regexp_replace(COALESCE(phone, ''), '\\D', '', 'g'), 10) = %s",
            (clean_phone,))
        already = any(same_venue(r["name"], cand["name"]) for r in cursor.fetchall())
    if already:
        cursor.execute(
            "UPDATE crm_lead_candidates SET status='rejected', "
            "reject_reason='already in pipeline' WHERE id=%s", (cand["id"],)
        )
        return None

    # Already a customer? Pitching an existing user is the worst call you can
    # make.
    if cand["email"]:
        cursor.execute(
            "SELECT id FROM users WHERE LOWER(email) = LOWER(%s) AND deleted_at IS NULL",
            (cand["email"],),
        )
    if cand["email"] and cursor.fetchone():
        cursor.execute(
            "UPDATE crm_lead_candidates SET status='rejected', "
            "reject_reason='already a customer' WHERE id=%s", (cand["id"],)
        )
        return None

    lead_id = generate_id()
    notes = (
        f"Auto-sourced {now[:10]} · {cand['amenity'] or 'bar'} · score {cand['score']}\n"
        f"{cand['website']}\n"
        + (f"Email found on: {cand['email_source'] or 'site'}" if cand.get("email")
           else "No email on their site — a call-only lead")
    )
    if cand.get("phone_status") == "from_site" and cand.get("phone_note"):
        notes += f"\nPhone taken from their website — {cand['phone_note']}"
    if cand.get("fit_note"):
        notes += f"\n{cand['fit_note']}"
    if cand.get("manager_name"):
        # Dated on purpose. A name read off a website is only ever "this is
        # what their site said on this day".
        notes += (f"\n{cand['manager_role']}: {cand['manager_name']} "
                  f"— per {cand.get('manager_source') or 'their site'}, "
                  f"read {(cand.get('manager_seen_at') or now)[:10]}")
    cursor.execute("""
        INSERT INTO crm_leads (id, name, loc, status, phone, email, notes,
                               source, tz_offset_hours, opening_hours, opener,
                               lead_score, email_kind, manager_name, manager_role,
                               manager_source, manager_seen_at, tz_name, venue_facts,
                               phone_status, phone_note, fit_status, fit_note,
                               created_at, updated_at)
        VALUES (%s, %s, %s, 'new', %s, %s, %s, 'leadgen', %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (lead_id, cand["name"], loc, cand["phone"], cand["email"], notes,
          cand.get("tz_offset_hours"), cand.get("opening_hours"),
          cand.get("opener"), cand.get("score"),
          cand.get("email_kind") or email_kind(cand.get("email")),
          cand.get("manager_name"), cand.get("manager_role"),
          cand.get("manager_source"), cand.get("manager_seen_at"),
          cand.get("tz_name"),
          # Falls back to the map tags alone when the row predates the
          # extractor — address, hours and cuisine are still worth having.
          cand.get("venue_facts") or venue_facts.dumps(
              venue_facts.extract_facts(_cand_tags(cand), "",
                                        cand.get("opening_hours"))),
          cand.get("phone_status"), cand.get("phone_note"),
          cand.get("fit_status"), cand.get("fit_note"),
          now, now))
    cursor.execute("""
        UPDATE crm_lead_candidates
           SET status='promoted', promoted_at=%s, promoted_lead_id=%s
         WHERE id=%s
    """, (now, lead_id, cand["id"]))
    return lead_id


def bucket_counts(cursor=None) -> dict:
    """Unworked leads per (service, zone), every cell present even at zero.

    Same filter the call list uses, so what this counts is exactly what the
    operator would see under that tab.
    """
    def _count(cur) -> dict:
        cur.execute("""
            SELECT tz_offset_hours, opening_hours FROM crm_leads
             WHERE status = 'new' AND last_touch_at IS NULL
        """)
        counts = {b: 0 for b in all_buckets()}
        for row in cur.fetchall():
            bucket = bucket_of(row["tz_offset_hours"], row["opening_hours"])
            if bucket in counts:
                counts[bucket] += 1
        return counts

    if cursor is not None:
        return _count(cursor)
    with get_db() as conn:
        return _count(conn.cursor())


def bucket_deficits(cursor=None) -> dict:
    """How many more leads each cell needs to reach BUCKET_TARGET."""
    return {b: max(0, BUCKET_TARGET - n) for b, n in bucket_counts(cursor).items()}


def recheck_restaurant_leads(limit: int = 200) -> dict:
    """Re-run the (now tightened) liquor gate against restaurant rows that
    were qualified or promoted under the old, looser LIQUOR_HINTS.

    A one-time correction, not a boot-time reconciliation like
    `_reconcile_bad_emails()`: an email string or a lat/lon can be
    recomputed from what's already stored, but this decision needs the
    venue's own site again, so fixing it means re-crawling every restaurant
    row rather than a cheap recompute. Too heavy to run on every boot —
    triggered on demand instead, from the Lead engine panel.

    Only ever touches restaurant-tagged rows nobody has worked yet:
      - banked candidates (`status='qualified'`) are re-rejected in place,
        exactly as if they'd failed `enrich_candidate()` today.
      - promoted-but-never-touched leads (`status='new' AND last_touch_at
        IS NULL`) are deleted the same way the operator's own Delete button
        deletes one — which also retires the candidate row, so the
        generator can't re-promote the same venue tomorrow.
    A lead that's already been called or logged is left alone: a keyword
    list changing doesn't undo a call that already happened.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, website, raw_tags FROM crm_lead_candidates
             WHERE amenity = 'restaurant' AND status = 'qualified'
             ORDER BY discovered_at ASC LIMIT %s
        """, (limit,))
        banked = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
            SELECT c.id AS candidate_id, c.website, c.raw_tags, c.promoted_lead_id
              FROM crm_lead_candidates c
              JOIN crm_leads l ON l.id = c.promoted_lead_id
             WHERE c.amenity = 'restaurant' AND l.status = 'new'
               AND l.last_touch_at IS NULL
             ORDER BY c.discovered_at ASC LIMIT %s
        """, (limit,))
        promoted = [dict(r) for r in cursor.fetchall()]

    rows = banked + promoted
    result = {"checked": 0, "banked_rejected": 0, "leads_removed": 0}
    if not rows:
        return result

    def _fetch(row: dict) -> str:
        home, status = _http(row["website"], timeout=PAGE_TIMEOUT, verify_public=True)
        return home[:200000] if status == 200 and home else ""

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        htmls = list(pool.map(_fetch, rows))

    with get_db() as conn:
        cursor = conn.cursor()
        for row, html in zip(rows, htmls):
            result["checked"] += 1
            try:
                tags = json.loads(row.get("raw_tags") or "{}")
            except json.JSONDecodeError:
                tags = {}
            qualifies, reason = _restaurant_pours(html, tags)
            if qualifies:
                continue
            if "candidate_id" in row:   # a promoted, never-touched lead
                cursor.execute("DELETE FROM crm_leads WHERE id=%s", (row["promoted_lead_id"],))
                cursor.execute("""
                    UPDATE crm_lead_candidates
                       SET status='rejected', reject_reason=%s, promoted_lead_id=NULL
                     WHERE id=%s
                """, (reason, row["candidate_id"]))
                result["leads_removed"] += 1
            else:                        # still banked, never promoted
                cursor.execute("""
                    UPDATE crm_lead_candidates
                       SET status='rejected', reject_reason=%s
                     WHERE id=%s
                """, (reason, row["id"]))
                result["banked_rejected"] += 1
        conn.commit()

    return result


def promote_leads(limit: int = DAILY_TARGET) -> int:
    """Move the best qualified candidates into crm_leads. Returns how many.

    Promotion is per-cell, not first-come. Sorting the whole bank by score and
    taking the top N fills whichever cells the harvest happened to favour and
    starves the rest — the measured Pacific/Eastern split was 131 to 12, so a
    score-ordered promote would have handed the operator a full Pacific tab and
    an empty Eastern one. Instead the emptiest cell is always served first, so
    the cells converge rather than diverge.

    Score still decides WHO gets promoted within a cell; it just no longer
    decides which cells get filled.
    """
    promoted = 0
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        deficits = bucket_deficits(cursor)
        if not any(deficits.values()):
            return 0

        # Only numbers the venue's own site vouches for, and only venues that
        # passed the owner's rules (liquor shown on their site). The rest stay
        # banked until the background check gets to them.
        cursor.execute("""
            SELECT * FROM crm_lead_candidates
             WHERE status = 'qualified' AND phone_status IN %s AND fit_status = 'ok'
             ORDER BY score DESC, discovered_at ASC
        """, (PHONE_OK,))
        corporate = corporate_index(cursor)
        # Bank the candidates by the cell they would land in. Best-first within
        # each cell, preserved from the query order.
        by_bucket: dict = {b: [] for b in all_buckets()}
        zoneless: list = []
        for cand in cursor.fetchall():
            bucket = bucket_of(cand["tz_offset_hours"], cand.get("opening_hours"))
            if bucket in by_bucket:
                by_bucket[bucket].append(dict(cand))
            else:
                # No longitude, so no zone, so no sub-tab to file it under.
                # Held back rather than dropped: it still gets promoted once
                # every real cell is full, and shows under "Unknown".
                zoneless.append(dict(cand))

        while promoted < limit:
            # Emptiest cell with something left in the bank.
            candidates_left = [b for b in by_bucket if deficits[b] > 0 and by_bucket[b]]
            if not candidates_left:
                break
            bucket = max(candidates_left, key=lambda b: deficits[b])
            cand = by_bucket[bucket].pop(0)
            if _promote_one(cursor, cand, now, corporate):
                promoted += 1
                deficits[bucket] -= 1

        # Only once the real cells are served: leads with no timezone can't be
        # worked zone by zone, so they must never displace one that can.
        while promoted < limit and zoneless:
            if _promote_one(cursor, zoneless.pop(0), now, corporate):
                promoted += 1

        conn.commit()
    return promoted


# ── The daily job ───────────────────────────────────────────────────────────

def active_lead_count() -> int:
    """Unworked leads in the call list — the same filter the screen uses."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS n FROM crm_leads "
            "WHERE status = 'new' AND last_touch_at IS NULL"
        )
        return cursor.fetchone()["n"]


def pool_depth() -> dict:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status, COUNT(*) AS n FROM crm_lead_candidates GROUP BY status
        """)
        by_status = {r["status"]: r["n"] for r in cursor.fetchall()}
        cursor.execute("SELECT COUNT(*) AS n FROM crm_leadgen_cities WHERE enabled AND last_harvested_at IS NULL")
        fresh_cities = cursor.fetchone()["n"]
        cursor.execute(
            "SELECT COUNT(*) AS n FROM crm_leads "
            "WHERE status = 'new' AND last_touch_at IS NULL"
        )
        active = cursor.fetchone()["n"]
    # Banked candidates their site can't vouch for are kept but never promoted,
    # so they aren't stock. Counting them made a bank look deep that couldn't
    # fill one cell, and the harvest that would have fixed it never ran.
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) AS n FROM crm_lead_candidates
             WHERE status = 'qualified'
               AND (phone_status IS NULL OR phone_status IN %s)
               AND (fit_status IS NULL OR fit_status = 'ok')
        """, (PHONE_OK,))
        qualified = cursor.fetchone()["n"]
    counts = bucket_counts()
    deficits = {b: max(0, BUCKET_TARGET - n) for b, n in counts.items()}
    headroom = sum(deficits.values())
    # A global "leads: 312" says nothing about whether the tab the operator is
    # about to open has anything in it. The thin cells are the number that
    # matters, so they get named.
    thin = sorted((b for b in counts if counts[b] < BUCKET_TARGET),
                  key=lambda b: counts[b])
    return {
        "by_status": by_status,
        "qualified": qualified,
        "active_leads": active,
        "bucket_target": BUCKET_TARGET,
        "buckets": [{"service": b[0], "zone": b[1], "count": counts[b],
                     "short_by": deficits[b]} for b in all_buckets()],
        "thinnest": [{"service": b[0], "zone": b[1], "count": counts[b]}
                     for b in thin[:3]],
        "max_active": MAX_ACTIVE,
        "headroom": headroom,
        "at_capacity": headroom == 0,
        "days_of_runway": round((active + qualified) / DAILY_TARGET, 1) if DAILY_TARGET else 0,
        "unharvested_cities": fresh_cities,
    }


CITY_RETRY_HOURS = 20


def _next_cities(limit: int) -> list[dict]:
    """Which cities to harvest next — the ones feeding the emptiest tabs.

    Round-robin by longitude was never the rule, and plain oldest-first isn't
    either: the seed list is ordered roughly by population, which put most of
    the Eastern metros at the back. One measured run had Pacific sitting on 131
    qualified leads while Eastern had 12, because 15 of 16 Eastern cities had
    never been touched. Sorting by the zone's shortfall first fixes that
    without anyone having to notice it happened.
    """
    per_zone: dict = {}
    for (_service, zone), short in bucket_deficits().items():
        per_zone[zone] = per_zone.get(zone, 0) + short

    with get_db() as conn:
        cursor = conn.cursor()
        rested = (datetime.now(timezone.utc) - timedelta(hours=CITY_RETRY_HOURS)).isoformat()
        cursor.execute("""
            SELECT * FROM crm_leadgen_cities
             WHERE enabled AND (harvest_failed_at IS NULL OR harvest_failed_at < %s)
             ORDER BY last_harvested_at ASC NULLS FIRST
        """, (rested,))
        cities = [dict(r) for r in cursor.fetchall()]

    def rank(city: dict):
        zone = us_tz_offset(city.get("lon"), city.get("state"),
                            city.get("lat"))
        # Negated: biggest shortfall first. Never-harvested breaks the tie,
        # then oldest — so a short zone still rotates through its own cities
        # instead of re-harvesting one of them forever.
        return (-per_zone.get(zone, 0),
                city.get("last_harvested_at") is not None,
                city.get("last_harvested_at") or "")

    cities.sort(key=rank)
    return cities[:limit]


def run_daily(target: int = DAILY_TARGET, max_cities: int = 4,
              max_enrich: int = 120) -> dict:
    """Harvest → enrich → promote, recording everything it did.

    Ordered so the day's leads are promoted from the existing bank first and
    harvesting only tops the bank back up: the morning list never waits on a
    slow crawl or a flaky mirror.
    """
    run_id = generate_id()
    started = now_iso()
    detail: dict = {"cities": [], "errors": []}
    cities_done = candidates = enriched = qualified = promoted = errors = 0

    with get_db() as conn:
        cursor = conn.cursor()
        # Reconcile runs that never finished. A process killed mid-run — which
        # on Render's free tier happens every time the service spins down —
        # leaves its row saying 'running' forever. Left alone those pile up and
        # quietly misreport what the pipeline has actually been doing.
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        cursor.execute("""
            UPDATE crm_leadgen_runs
               SET phase = 'abandoned', ok = FALSE, finished_at = %s,
                   detail = COALESCE(detail, '') ||
                            '{"note":"process exited before this run finished"}'
             WHERE phase = 'running' AND started_at < %s
        """, (now_iso(), cutoff))
        abandoned = cursor.rowcount
        if abandoned:
            print(f"[leadgen] marked {abandoned} interrupted run(s) as abandoned", flush=True)

        cursor.execute("""
            INSERT INTO crm_leadgen_runs (id, phase, started_at) VALUES (%s, 'running', %s)
        """, (run_id, started))
        conn.commit()

    try:
        # 0. How much room is there, cell by cell? Everything below is sized
        #    by this. Summed only for the "is it all full?" test — the shape
        #    matters more than the total, because a run that promotes 25 leads
        #    into an already-full Pacific tab has delivered nothing.
        deficits = bucket_deficits()
        headroom = sum(deficits.values())
        detail["headroom_at_start"] = headroom
        detail["short_cells"] = {f"{s_}/{z}": d for (s_, z), d in deficits.items() if d}

        # Nothing to add and nothing worth banking: stop before touching the
        # network at all. This is the whole point of the cap — when the list is
        # full, the generator does no work rather than quietly piling up leads
        # that will never be called.
        if headroom == 0 and pool_depth()["qualified"] >= POOL_FLOOR:
            detail["skipped"] = (
                f"every tab is full ({BUCKET_TARGET} in each of "
                f"{len(all_buckets())} cells) and the bank is stocked — "
                "no harvesting, no crawling, nothing promoted"
            )
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE crm_leadgen_runs
                       SET phase='done', ok=TRUE, finished_at=%s, detail=%s
                     WHERE id=%s
                """, (now_iso(), json.dumps({**detail, "pool": pool_depth()})[:8000], run_id))
                conn.commit()
            print(f"[leadgen] LEADGEN_RUN ok=True skipped=all_tabs_full "
                  f"active={MAX_ACTIVE}/{MAX_ACTIVE} "
                  f"per_cell={BUCKET_TARGET}", flush=True)
            return {"run_id": run_id, "ok": True, "promoted": 0, "enriched": 0,
                    "qualified": 0, "candidates_found": 0, "cities_harvested": 0,
                    "errors": 0, "detail": detail}

        # 1. Promote first, from what's already banked.
        #
        #    Sized by the holes, not by the daily target. `target` is a pace,
        #    not a ceiling: on a cold start eight empty cells need 400 leads
        #    and metering that out 25 a day would leave the tabs unusable for a
        #    fortnight. In steady state the two coincide anyway — the cap means
        #    only as many leads can land as were called off the list.
        # Banked candidates enriched before the website check can't be promoted
        # until their number is checked; check enough of the best to fill the
        # holes. A no-op once the bank is all checked.
        verify_phones(lead_limit=0, bank_limit=max(60, headroom * 2))
        verify_fit(lead_limit=0, bank_limit=max(60, headroom * 2))
        promoted = promote_leads(headroom)

        # 2. Top the bank back up if it's getting shallow, or if what's banked
        #    can't reach the cells that are actually short. A bank of 200
        #    Pacific candidates is a deep bank and an empty Eastern tab.
        depth = pool_depth()
        remaining_deficit = sum(bucket_deficits().values())
        # Candidates that have been harvested but never crawled. These are the
        # stock that matters here: turning one into a callable lead needs a
        # crawl, not another trip to Overpass.
        unenriched = depth["by_status"].get("new", 0)
        # Only harvest when that stock is actually thin. Keyed on `qualified`
        # alone this fired with thousands of uncrawled candidates already
        # banked — a cold start has 0 qualified by definition, so every run
        # opened with a dozen Overpass sweeps before crawling a single site.
        # That is minutes of network in front of the one stage that produces
        # leads, and on a small instance it is where the run gets killed: the
        # observed result was a candidate pile that kept growing, enriched
        # stuck at 0, and runs that never reached their own final write.
        stock_thin = unenriched < max(max_enrich, POOL_FLOOR)
        if stock_thin and (depth["qualified"] < POOL_FLOOR
                           or remaining_deficit > depth["qualified"]):
            # A metro yields roughly 15-20 qualified leads. Filling eight
            # empty cells needs several of them, so a cold start harvests wide
            # and a topped-up list harvests one or two. Capped so a single run
            # can't sit on Overpass all evening.
            wanted = max(max_cities, -(-remaining_deficit // 17))
            cities = _next_cities(min(wanted, 12))
            detail["harvest_order"] = [c["name"] for c in cities]

            for city in cities:
                try:
                    seen, added = harvest_city(city)
                    candidates += added
                    cities_done += 1
                    detail["cities"].append(
                        {"city": city["name"], "seen": seen, "new": added}
                    )
                except Exception as exc:
                    errors += 1
                    detail["errors"].append(f"harvest {city['name']}: {exc}")
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute("UPDATE crm_leadgen_cities SET harvest_failed_at = %s "
                                       "WHERE id = %s", (now_iso(), city["id"]))
                        conn.commit()

        # 3. Enrich whatever is still unexamined.
        with get_db() as conn:
            cursor = conn.cursor()
            # New candidates, and ones whose site didn't load last time and are
            # due another try.
            cursor.execute("""
                SELECT * FROM crm_lead_candidates
                 WHERE status = 'new' OR (status = 'retry' AND retry_after <= %s)
                 ORDER BY (status = 'retry') DESC, discovered_at ASC
                 LIMIT %s
            """, (now_iso(), max_enrich))
            pending = [dict(r) for r in cursor.fetchall()]

        # Enrichment is pure network wait and each candidate is independent, so
        # it runs wide. Sequentially this was the whole runtime of a daily run.
        from concurrent.futures import ThreadPoolExecutor
        results: list[tuple[dict, dict]] = []
        if pending:
            with get_db() as conn:
                codes = _local_codes_by_city(conn.cursor(), [c.get("city") for c in pending])
            for c in pending:
                c["local_codes"] = codes.get(c.get("city"), set())
            with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
                for cand, result in zip(pending, pool.map(_enrich_safe, pending)):
                    results.append((cand, result))

        for cand, result in results:
            try:
                if result is None:
                    errors += 1
                    continue
                enriched += 1
                if result["status"] == "retry":
                    _record_retry(cand, result["reject_reason"])
                    continue
                if result["status"] == "qualified":
                    qualified += 1
                with get_db() as conn:
                    cursor = conn.cursor()
                    try:
                        cursor.execute("""
                            UPDATE crm_lead_candidates
                               SET status=%s, reject_reason=%s, email=%s,
                                   email_source=%s, email_kind=%s, opener=%s,
                                   manager_name=%s, manager_role=%s,
                                   manager_source=%s, manager_seen_at=%s,
                                   venue_facts=%s, score=%s, enriched_at=%s,
                                   phone=COALESCE(%s, phone), phone_status=%s,
                                   phone_note=%s, website=COALESCE(%s, website),
                                   fit_status=%s, fit_note=%s
                             WHERE id=%s
                        """, (result["status"], result["reject_reason"], result["email"],
                              result["email_source"], result.get("email_kind"),
                              result.get("opener"), result.get("manager_name"),
                              result.get("manager_role"), result.get("manager_source"),
                              result.get("manager_seen_at"),
                              result.get("venue_facts"),
                              result["score"], now_iso(), result.get("phone"),
                              result.get("phone_status"), result.get("phone_note"),
                              result.get("website"), result.get("fit_status"),
                              result.get("fit_note"), cand["id"]))
                        conn.commit()
                    except Exception:
                        # Almost always the unique-email index: another venue
                        # already claimed this address. It used to REJECT the
                        # venue — but one management company's info@ serves
                        # several separate bars (Mean Eyed Cat, Lala's Little
                        # Nugget and Lavaca Street Bar share one), each worth
                        # its own call. Kept, without the shared address.
                        conn.rollback()
                        cursor.execute("""
                            UPDATE crm_lead_candidates
                               SET status=%s, reject_reason=%s, email=NULL,
                                   email_source=NULL, email_kind=NULL, opener=%s,
                                   manager_name=%s, manager_role=%s,
                                   manager_source=%s, manager_seen_at=%s,
                                   venue_facts=%s, score=%s, enriched_at=%s,
                                   phone=COALESCE(%s, phone), phone_status=%s,
                                   phone_note=%s, website=COALESCE(%s, website),
                                   fit_status=%s, fit_note=%s
                             WHERE id=%s
                        """, (result["status"], result["reject_reason"],
                              result.get("opener"), result.get("manager_name"),
                              result.get("manager_role"), result.get("manager_source"),
                              result.get("manager_seen_at"), result.get("venue_facts"),
                              max(0, result["score"] - 3), now_iso(), result.get("phone"),
                              result.get("phone_status"), result.get("phone_note"),
                              result.get("website"), result.get("fit_status"),
                              result.get("fit_note"), cand["id"]))
                        conn.commit()
            except Exception as exc:
                errors += 1
                detail["errors"].append(f"enrich {cand.get('name')}: {exc}")

        # 4. If the first promote came up short and enriching just produced
        #    fresh stock, top the list up rather than under-delivering — still
        #    bounded by whatever room is left right now.
        remaining_room = sum(bucket_deficits().values())
        if remaining_room:
            promoted += promote_leads(remaining_room)

        ok = promoted > 0 or errors == 0
    except Exception as exc:
        ok = False
        errors += 1
        detail["errors"].append(f"run: {exc}")

    detail["pool"] = pool_depth()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crm_leadgen_runs
               SET phase='done', ok=%s, finished_at=%s, cities_harvested=%s,
                   candidates_found=%s, enriched=%s, qualified=%s, promoted=%s,
                   errors=%s, detail=%s
             WHERE id=%s
        """, (ok, now_iso(), cities_done, candidates, enriched, qualified,
              promoted, errors, json.dumps(detail)[:8000], run_id))
        conn.commit()

    print(f"[leadgen] LEADGEN_RUN ok={ok} promoted={promoted} enriched={enriched} "
          f"qualified={qualified} new_candidates={candidates} errors={errors} "
          f"active={detail['pool']['active_leads']}/{MAX_ACTIVE} "
          f"thinnest={detail['pool']['thinnest']} "
          f"runway_days={detail['pool']['days_of_runway']}", flush=True)

    return {
        "run_id": run_id, "ok": ok, "promoted": promoted, "enriched": enriched,
        "qualified": qualified, "candidates_found": candidates,
        "cities_harvested": cities_done, "errors": errors, "detail": detail,
    }
