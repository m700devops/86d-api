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
import os
import re
import socket
import subprocess
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

from database import get_db
from helpers import generate_id, now_iso
from callwindow import ZONE_OFFSETS, SERVICES, bucket_of, all_buckets
from contacts import email_kind, find_manager
from phones import normalize_us_phone, is_toll_free

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
    r'href=["\']([^"\']{1,200})["\'][^>]*>(?:[^<]{0,80})'
    r'(contact|about|info|reach us|get in touch|private|events|book)',
    re.I,
)

PAGE_TIMEOUT = 12          # per request; a bar site that's slower isn't worth it
MAX_PAGES_PER_SITE = 6     # hard ceiling so one bad domain can't eat a run
ENRICH_WORKERS = int(os.getenv("LEADGEN_ENRICH_WORKERS", "8"))

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r'mailto:\s*([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', re.I)
# Obfuscated forms — "info [at] barname [dot] com" and friends.
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\s+at\s+)\s*([A-Za-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*([A-Za-z]{2,})",
    re.I,
)

# Addresses that are never a bar owner: platform noise, tracking, stock images.
#
# The @domain entries matter as much as the rest. Site builders leave their own
# addresses in template markup — a real harvest produced "wixofday@wix.com" as
# the contact for a Portland bar, which is a perfectly valid-looking email that
# reaches Wix's marketing team and never the venue. A lead nobody can reply to
# is worse than no lead, because it still costs a call slot and a follow-up.
PLATFORM_DOMAINS = (
    r"wix\.com|wixpress\.com|squarespace\.com|weebly\.com|godaddy\.com|"
    r"shopify\.com|cloudflare\.com|sentry\.io|wordpress\.com|bluehost\.com|"
    r"hostgator\.com|networksolutions\.com|register\.com|domainsbyproxy\.com|"
    r"squareup\.com|toasttab\.com|opentable\.com|resy\.com|yelp\.com|"
    r"doordash\.com|grubhub\.com|ubereats\.com|facebook\.com|instagram\.com"
)
# Placeholder addresses left in a website template. These are the worst kind
# of bad lead: they look personal, so they sorted to the TOP of the call list,
# and they reach nobody at all. Two turned up in a single screenful of real
# harvested data — `your@email.com` and `mymail@mailservice.com`, both stock
# text the venue never replaced.
TEMPLATE_EMAILS = (
    r"^(your|youremail|yourname|my|mymail|myemail|email|e-?mail|name|username|user"
    r"|someone|somebody|john\.?doe|jane\.?doe|firstname|lastname|test|testing"
    r"|sample|demo|address|mail)@"
)
TEMPLATE_DOMAINS = (
    r"mailservice\.com|yourdomain|yourwebsite|yoursite|domain\.com|website\.com"
    r"|email\.com|mysite\.com|company\.com|business\.com|sentry\.io"
    # Two real bar sites yielded `bank<uuid>@test.com` — a script's throwaway
    # address sitting in the page source. @test.com is never a venue.
    r"|test\.com|test\.org|localhost|invalid|dummy\.com"
)
# A local part that is a machine identifier rather than a mailbox: a UUID, a
# long hex blob, a session token. Generated by scripts, read by nobody.
MACHINE_LOCAL = r"^[a-z]*[0-9a-f]{8}-[0-9a-f]{4}-|^[0-9a-f]{16,}@|^[a-z0-9]{24,}@"
EMAIL_BLOCKLIST = re.compile(
    r"(sentry|wixpress|squarespace|godaddy|shopify|cloudflare|example\.(com|org)|"
    r"your(email|name)|email@|name@|user@|test@|no-?reply|donotreply|postmaster|"
    r"abuse@|webmaster@|@2x|\.png|\.jpe?g|\.gif|\.svg|\.webp|\.css|\.js"
    rf"|{TEMPLATE_EMAILS}|{MACHINE_LOCAL}|@(?:{TEMPLATE_DOMAINS})$"
    rf"|@(?:{PLATFORM_DOMAINS})$)",
    re.I,
)

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
    "fox & hound", "bar louie", "brewhouse", "granite city", "mcfadden",
    "tap house", "walk-on", "walk on's", "walkons", "pluckers", "torchy",
    "punch bowl social", "lucky strike", "main event", "topgolf", "chuck e",
    "golden corral", "red lobster", "on the border", "uno pizzeria",
    "famous dave", "smokey bones", "hurricane grill", "duffy's sports",
    "tijuana flats", "first watch", "another broken egg", "marriott", "hilton",
    "hyatt", "sheraton", "doubletree", "embassy suites", "holiday inn",
    "courtyard by", "residence inn", "casino", "airport",
}

# Words on a site that mean "this is one of many" — franchise language.
CHAIN_SITE_HINTS = re.compile(
    r"(find a location|our locations|all locations|franchis|nearest location|"
    r"select a location|locations near|corporate office|nationwide)",
    re.I,
)

# Words that confirm a full liquor program rather than a beer-and-wine cafe.
LIQUOR_HINTS = re.compile(
    r"(cocktail|full bar|craft beer|spirits?|whisk(e)?y|bourbon|tequila|mezcal|"
    r"martini|margarita|happy hour|liquor|distiller|mixolog|draft|draught|"
    r"wine list|bar menu|drink menu|shots?\b|tap list)",
    re.I,
)


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


def _http(url: str, timeout: int = 20, data: Optional[str] = None,
          verify_public: bool = False) -> tuple[str, int]:
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
                              ("manager_name", "TEXT"), ("manager_role", "TEXT"),
                              ("manager_source", "TEXT"), ("manager_seen_at", "TEXT")]:
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

        cursor.execute("SELECT COUNT(*) AS n FROM crm_leadgen_cities")
        city_count = cursor.fetchone()["n"]
        if city_count == 0:
            _seed_cities(cursor)
            conn.commit()
            cursor.execute("SELECT COUNT(*) AS n FROM crm_leadgen_cities")
            city_count = cursor.fetchone()["n"]

        moved = _reconcile_timezones(cursor)
        conn.commit()

        print(f"[leadgen] LEADGEN_TABLES_READY cities={city_count} "
              f"bucket_target={BUCKET_TARGET} max_active={MAX_ACTIVE} "
              f"daily_target={DAILY_TARGET} pool_floor={POOL_FLOOR}"
              + (f" retimezoned={moved}" if moved else ""), flush=True)


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
    for name, state, lat, lon in SEED_CITIES:
        cursor.execute("""
            INSERT INTO crm_leadgen_cities (id, name, state, lat, lon, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (generate_id(), name, state, lat, lon, now))
    print(f"[leadgen] seeded {len(SEED_CITIES)} cities", flush=True)


# ── Classification helpers ──────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()


def looks_like_chain(name: str, website: str = "", site_html: str = "") -> Optional[str]:
    """Why this is a chain, or None if it looks independent."""
    norm = normalize_name(name)
    for chain in CHAIN_NAMES:
        if chain in norm:
            return f"chain name ({chain})"
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


def extract_emails(html: str) -> list[str]:
    """Every plausible human address on a page, best first."""
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
    for m in EMAIL_RE.findall(html or ""):
        add(m)
    for user, dom, tld in OBFUSCATED_RE.findall(html or ""):
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
    (re.compile(r"\d{2,}\s*(taps|beers on tap|draft lines)", re.I), "a big tap list"),
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


def score_candidate(tags: dict, email: Optional[str], site_html: str,
                    manager: Optional[dict] = None) -> int:
    """How well this fits a bar-inventory pitch. Higher is better.

    Two things were added once the call list started sorting by this rather
    than just filtering on it.

    FIT. A venue that already runs Resy and Toast probably has something that
    claims to handle inventory, so the pitch lands in a crowded room. A
    neighbourhood bar with a pool table and a happy hour has a real liquor
    inventory and, most likely, a clipboard. Neither is a rule — plenty of
    fancy rooms still count by hand, and they stay on the list — but when
    there are fifty names in front of you, order matters more than inclusion.

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

    if site_html and LIQUOR_HINTS.search(site_html):
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
    return score


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


# ── Stage 1: harvest ────────────────────────────────────────────────────────

def _overpass(query: str) -> Optional[dict]:
    """Query the first mirror that returns a usable answer.

    An empty element list counts as NOT usable and moves to the next mirror.
    A city genuinely containing zero bars is possible but vanishingly rare, and
    the cost of being wrong about it is one extra request — whereas accepting
    an empty answer means trusting a mirror that may simply not hold this part
    of the planet (see the note on OVERPASS_MIRRORS). Silent emptiness is the
    failure this pipeline can least afford, so it is never treated as data.
    """
    for mirror in OVERPASS_MIRRORS:
        body, status = _http(mirror, timeout=90, data=query)
        if status == 200 and body.strip().startswith("{"):
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = None
            if data is not None:
                if data.get("elements"):
                    return data
                print(f"[leadgen] overpass mirror {mirror} -> 200 but 0 elements, "
                      f"treating as a miss and trying the next", flush=True)
                time.sleep(1)
                continue
        print(f"[leadgen] overpass mirror {mirror} -> {status}", flush=True)
        time.sleep(1)
    return None


def harvest_city(city: dict) -> tuple[int, int]:
    """Pull venues for one city into the pool. Returns (seen, inserted)."""
    query = f"""
[out:json][timeout:60];
(
  node["amenity"~"^(bar|pub|nightclub)$"](around:{int(city['radius_m'])},{city['lat']},{city['lon']});
  way["amenity"~"^(bar|pub|nightclub)$"](around:{int(city['radius_m'])},{city['lat']},{city['lon']});
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
            raw_phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
            # Validated at the door. An unusable number can never reach the call
            # list, because dialling a stranger costs more than dropping a lead.
            phone = normalize_us_phone(raw_phone)
            if phone and is_toll_free(phone):
                continue   # toll-free on an independent bar is a platform line
            website = (tags.get("website") or tags.get("contact:website")
                       or tags.get("url") or "").strip()
            # No phone or no website means it can never satisfy the brief, so
            # it isn't worth a row or a later crawl.
            if not phone or not website:
                continue
            if not website.lower().startswith("http"):
                website = "http://" + website

            source_ref = f"{el.get('type')}/{el.get('id')}"
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")

            cursor.execute("""
                INSERT INTO crm_lead_candidates
                    (id, source, source_ref, name, city, state, lat, lon, phone, website,
                     amenity, raw_tags, opening_hours, tz_offset_hours, tz_name,
                     status, discovered_at)
                VALUES (%s, 'osm', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', %s)
                ON CONFLICT (source_ref) DO NOTHING
            """, (
                generate_id(), source_ref, name, city["name"], city.get("state"),
                lat, lon, phone, website, tags.get("amenity"),
                json.dumps(tags)[:8000], (tags.get("opening_hours") or "").strip() or None,
                us_tz_offset(lon, city.get("state"), lat),
                us_tz_name(lon, city.get("state"), lat), now,
            ))
            inserted += cursor.rowcount

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
    tagged = (osm_tags.get("email") or osm_tags.get("contact:email") or "").strip()
    if tagged and not EMAIL_BLOCKLIST.search(tagged) and "@" in tagged:
        email, email_source = tagged.lower(), "OpenStreetMap tag"

    home, status = _http(website, timeout=PAGE_TIMEOUT, verify_public=True)
    fetched += 1
    if status != 200 or not home:
        return {"status": "rejected", "reject_reason": f"site unreachable (HTTP {status})",
                "email": None, "email_source": None, "email_kind": None,
                "manager_name": None, "manager_role": None,
                "manager_source": None, "manager_seen_at": None,
                "opener": None, "score": 0}

    html_seen += home[:200000]
    pages.append((website, home))
    if not email:
        emails = extract_emails(home)
        if emails:
            email, email_source = emails[0], website

    if not email:
        import urllib.parse
        candidates_urls: list[str] = []
        for href, _kind in CONTACT_LINK_RE.findall(home):
            if href.startswith(("mailto:", "tel:", "#", "javascript:")):
                continue
            full = urllib.parse.urljoin(website, href)
            # http(s) only — urljoin will happily carry a file:// or data: href
            # straight through from the page.
            if not full.lower().startswith(("http://", "https://")):
                continue
            # Stay on the venue's own site; an off-site link is a social
            # profile or a booking platform, not their contact page.
            if domain_of(full) != domain_of(website):
                continue
            if full not in candidates_urls:
                candidates_urls.append(full)
        for path in CONTACT_PATHS:
            full = website.rstrip("/") + path
            if full not in candidates_urls:
                candidates_urls.append(full)

        for url in candidates_urls:
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

    chain_reason = looks_like_chain(cand["name"], website, html_seen)
    tags = {}
    try:
        tags = json.loads(cand.get("raw_tags") or "{}")
    except json.JSONDecodeError:
        pass

    if chain_reason:
        return {"status": "rejected", "reject_reason": chain_reason,
                "email": None, "email_source": None, "email_kind": None,
                "manager_name": None, "manager_role": None,
                "manager_source": None, "manager_seen_at": None,
                "opener": None, "score": 0}
    if not email:
        return {"status": "rejected", "reject_reason": "no email found on site",
                "email": None, "email_source": None, "email_kind": None,
                "manager_name": None, "manager_role": None,
                "manager_source": None, "manager_seen_at": None,
                "opener": None, "score": 0}

    return {
        "status": "qualified",
        "reject_reason": None,
        "email": email,
        "email_source": email_source,
        "email_kind": email_kind(email),
        "manager_name": manager["name"] if manager else None,
        "manager_role": manager["role"] if manager else None,
        "manager_source": manager["source"] if manager else None,
        "manager_seen_at": now_iso() if manager else None,
        "opener": opener_line(html_seen, cand.get("amenity")),
        "score": score_candidate(tags, email, html_seen, manager),
    }


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

def _promote_one(cursor, cand: dict, now: str) -> Optional[str]:
    """Promote a single candidate, or reject it and return None.

    Split out of the loop so the bucket filler can walk past a candidate that
    turns out to be suppressed or a duplicate and try the next one in the same
    cell, instead of leaving that cell short.
    """
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
        "SELECT id FROM crm_leads WHERE LOWER(email) = LOWER(%s) "
        "OR (LOWER(name) = LOWER(%s) AND LOWER(COALESCE(loc,'')) = LOWER(%s))",
        (cand["email"], cand["name"], loc),
    )
    if cursor.fetchone():
        cursor.execute(
            "UPDATE crm_lead_candidates SET status='rejected', "
            "reject_reason='already in pipeline' WHERE id=%s", (cand["id"],)
        )
        return None

    # Already a customer? Pitching an existing user is the worst call you can
    # make.
    cursor.execute(
        "SELECT id FROM users WHERE LOWER(email) = LOWER(%s) AND deleted_at IS NULL",
        (cand["email"],),
    )
    if cursor.fetchone():
        cursor.execute(
            "UPDATE crm_lead_candidates SET status='rejected', "
            "reject_reason='already a customer' WHERE id=%s", (cand["id"],)
        )
        return None

    lead_id = generate_id()
    notes = (
        f"Auto-sourced {now[:10]} · {cand['amenity'] or 'bar'} · score {cand['score']}\n"
        f"{cand['website']}\n"
        f"Email found on: {cand['email_source'] or 'site'}"
    )
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
                               manager_source, manager_seen_at, tz_name,
                               created_at, updated_at)
        VALUES (%s, %s, %s, 'new', %s, %s, %s, 'leadgen', %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (lead_id, cand["name"], loc, cand["phone"], cand["email"], notes,
          cand.get("tz_offset_hours"), cand.get("opening_hours"),
          cand.get("opener"), cand.get("score"),
          cand.get("email_kind") or email_kind(cand.get("email")),
          cand.get("manager_name"), cand.get("manager_role"),
          cand.get("manager_source"), cand.get("manager_seen_at"),
          cand.get("tz_name"), now, now))
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

        cursor.execute("""
            SELECT * FROM crm_lead_candidates
             WHERE status = 'qualified'
             ORDER BY score DESC, discovered_at ASC
        """)
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
            if _promote_one(cursor, cand, now):
                promoted += 1
                deficits[bucket] -= 1

        # Only once the real cells are served: leads with no timezone can't be
        # worked zone by zone, so they must never displace one that can.
        while promoted < limit and zoneless:
            if _promote_one(cursor, zoneless.pop(0), now):
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
    qualified = by_status.get("qualified", 0)
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
        cursor.execute("""
            SELECT * FROM crm_leadgen_cities
             WHERE enabled
             ORDER BY last_harvested_at ASC NULLS FIRST
        """)
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
        promoted = promote_leads(headroom)

        # 2. Top the bank back up if it's getting shallow, or if what's banked
        #    can't reach the cells that are actually short. A bank of 200
        #    Pacific candidates is a deep bank and an empty Eastern tab.
        depth = pool_depth()
        remaining_deficit = sum(bucket_deficits().values())
        if depth["qualified"] < POOL_FLOOR or remaining_deficit > depth["qualified"]:
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

        # 3. Enrich whatever is still unexamined.
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM crm_lead_candidates
                 WHERE status = 'new'
                 ORDER BY discovered_at ASC
                 LIMIT %s
            """, (max_enrich,))
            pending = [dict(r) for r in cursor.fetchall()]

        # Enrichment is pure network wait and each candidate is independent, so
        # it runs wide. Sequentially this was the whole runtime of a daily run.
        from concurrent.futures import ThreadPoolExecutor
        results: list[tuple[dict, dict]] = []
        if pending:
            with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
                for cand, result in zip(pending, pool.map(_enrich_safe, pending)):
                    results.append((cand, result))

        for cand, result in results:
            try:
                if result is None:
                    errors += 1
                    continue
                enriched += 1
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
                                   score=%s, enriched_at=%s
                             WHERE id=%s
                        """, (result["status"], result["reject_reason"], result["email"],
                              result["email_source"], result.get("email_kind"),
                              result.get("opener"), result.get("manager_name"),
                              result.get("manager_role"), result.get("manager_source"),
                              result.get("manager_seen_at"),
                              result["score"], now_iso(), cand["id"]))
                        conn.commit()
                    except Exception:
                        # Almost always the unique-email index: another venue
                        # already claimed this address. That's a duplicate, not
                        # an error worth failing the run over.
                        conn.rollback()
                        cursor.execute("""
                            UPDATE crm_lead_candidates
                               SET status='rejected', reject_reason='duplicate email',
                                   enriched_at=%s
                             WHERE id=%s
                        """, (now_iso(), cand["id"]))
                        conn.commit()
                        qualified = max(0, qualified - 1)
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
