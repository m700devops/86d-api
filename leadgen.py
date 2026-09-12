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

# ── Tunables ────────────────────────────────────────────────────────────────

DAILY_TARGET = int(os.getenv("LEADGEN_DAILY_TARGET", "25"))

# Hard ceiling on unworked leads sitting in the call list. The generator tops
# the list up towards this and never past it, so working leads off is what
# creates room for new ones.
#
# This is a usability limit before it's a resource one. A list that only ever
# grows is a list you stop opening, and at 25/day a hundred is already four
# days of calling visible at any moment.
MAX_ACTIVE = int(os.getenv("LEADGEN_MAX_ACTIVE", "100"))

# Qualified candidates kept banked behind the call list. Much smaller than it
# used to be: with a capped list holding four days of work, the list is itself
# the buffer, and a deep second bank would just be crawling nobody asked for.
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
EMAIL_BLOCKLIST = re.compile(
    r"(sentry|wixpress|squarespace|godaddy|shopify|cloudflare|example\.(com|org)|"
    r"your(email|name)|email@|name@|user@|test@|no-?reply|donotreply|postmaster|"
    r"abuse@|webmaster@|@2x|\.png|\.jpe?g|\.gif|\.svg|\.webp|\.css|\.js"
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
                discovered_at TEXT NOT NULL,
                enriched_at TEXT,
                promoted_at TEXT,
                promoted_lead_id TEXT
            )
        """)
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

        print(f"[leadgen] LEADGEN_TABLES_READY cities={city_count} "
              f"daily_target={DAILY_TARGET} max_active={MAX_ACTIVE} "
              f"pool_floor={POOL_FLOOR}", flush=True)


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


def us_tz_offset(lon: Optional[float]) -> Optional[int]:
    """Rough UTC offset from longitude, for the call-window hint.

    Deliberately crude — this only decides whether to show "good time to call"
    next to a phone number, and being an hour out never costs more than a
    voicemail. Standard time; DST is not modelled.
    """
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


def score_candidate(tags: dict, email: Optional[str], site_html: str) -> int:
    """How well this fits a bar-inventory pitch. Higher is better."""
    score = 0
    amenity = tags.get("amenity", "")
    if amenity in ("bar", "pub"):
        score += 3          # pours liquor by definition
    elif amenity == "nightclub":
        score += 2
    elif amenity == "restaurant":
        score += 1          # may or may not have a real bar program

    if email:
        score += 3
        local = email.split("@")[0]
        # A named human or an owner-ish mailbox answers more often than info@.
        if local not in ("info", "contact", "hello"):
            score += 1

    if site_html and LIQUOR_HINTS.search(site_html):
        score += 2
    if tags.get("brand") or tags.get("operator"):
        score -= 2          # branded/operated usually means a group
    if tags.get("opening_hours"):
        score += 1          # a maintained listing is a real, live business
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
            phone = (tags.get("phone") or tags.get("contact:phone") or "").strip()
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
                     amenity, raw_tags, tz_offset_hours, status, discovered_at)
                VALUES (%s, 'osm', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', %s)
                ON CONFLICT (source_ref) DO NOTHING
            """, (
                generate_id(), source_ref, name, city["name"], city.get("state"),
                lat, lon, phone, website, tags.get("amenity"),
                json.dumps(tags)[:8000], us_tz_offset(lon), now,
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
    email = None
    email_source = None
    fetched = 0

    home, status = _http(website, timeout=PAGE_TIMEOUT, verify_public=True)
    fetched += 1
    if status != 200 or not home:
        return {"status": "rejected", "reject_reason": f"site unreachable (HTTP {status})",
                "email": None, "email_source": None, "score": 0}

    html_seen += home[:200000]
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
            found = extract_emails(body)
            if found:
                email, email_source = found[0], url
                break

    chain_reason = looks_like_chain(cand["name"], website, html_seen)
    tags = {}
    try:
        tags = json.loads(cand.get("raw_tags") or "{}")
    except json.JSONDecodeError:
        pass

    if chain_reason:
        return {"status": "rejected", "reject_reason": chain_reason,
                "email": None, "email_source": None, "score": 0}
    if not email:
        return {"status": "rejected", "reject_reason": "no email found on site",
                "email": None, "email_source": None, "score": 0}

    return {
        "status": "qualified",
        "reject_reason": None,
        "email": email,
        "email_source": email_source,
        "score": score_candidate(tags, email, html_seen),
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

def promote_leads(limit: int = DAILY_TARGET) -> int:
    """Move the best qualified candidates into crm_leads. Returns how many."""
    promoted = 0
    now = now_iso()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM crm_lead_candidates
             WHERE status = 'qualified'
             ORDER BY score DESC, discovered_at ASC
             LIMIT %s
        """, (limit * 3,))   # over-fetch: some will be filtered below
        rows = cursor.fetchall()

        for cand in rows:
            if promoted >= limit:
                break

            reason = _is_suppressed(cursor, cand["name"], cand["email"],
                                    cand["phone"], cand["website"])
            if reason:
                cursor.execute(
                    "UPDATE crm_lead_candidates SET status='rejected', reject_reason=%s WHERE id=%s",
                    (reason, cand["id"]),
                )
                continue

            # Already in the pipeline under this email or name? Never pitch the
            # same bar twice.
            cursor.execute(
                "SELECT id FROM crm_leads WHERE LOWER(email) = LOWER(%s) "
                "OR (LOWER(name) = LOWER(%s) AND LOWER(COALESCE(loc,'')) = LOWER(%s))",
                (cand["email"], cand["name"], cand["city"] or ""),
            )
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE crm_lead_candidates SET status='rejected', "
                    "reject_reason='already in pipeline' WHERE id=%s", (cand["id"],)
                )
                continue

            # Already a customer? Pitching an existing user is the worst call
            # you can make.
            cursor.execute(
                "SELECT id FROM users WHERE LOWER(email) = LOWER(%s) AND deleted_at IS NULL",
                (cand["email"],),
            )
            if cursor.fetchone():
                cursor.execute(
                    "UPDATE crm_lead_candidates SET status='rejected', "
                    "reject_reason='already a customer' WHERE id=%s", (cand["id"],)
                )
                continue

            lead_id = generate_id()
            loc = ", ".join(x for x in [cand["city"], cand["state"]] if x)
            notes = (
                f"Auto-sourced {now[:10]} · {cand['amenity'] or 'bar'} · score {cand['score']}\n"
                f"{cand['website']}\n"
                f"Email found on: {cand['email_source'] or 'site'}"
            )
            cursor.execute("""
                INSERT INTO crm_leads (id, name, loc, status, phone, email, notes,
                                       source, tz_offset_hours, created_at, updated_at)
                VALUES (%s, %s, %s, 'new', %s, %s, %s, 'leadgen', %s, %s, %s)
            """, (lead_id, cand["name"], loc, cand["phone"], cand["email"], notes,
                  cand.get("tz_offset_hours"), now, now))
            cursor.execute("""
                UPDATE crm_lead_candidates
                   SET status='promoted', promoted_at=%s, promoted_lead_id=%s
                 WHERE id=%s
            """, (now, lead_id, cand["id"]))
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
    # Runway counts what's actually callable, not just what's banked — the
    # capped list in front of you is the first few days of work.
    return {
        "by_status": by_status,
        "qualified": qualified,
        "active_leads": active,
        "max_active": MAX_ACTIVE,
        "headroom": max(0, MAX_ACTIVE - active),
        "at_capacity": active >= MAX_ACTIVE,
        "days_of_runway": round((active + qualified) / DAILY_TARGET, 1) if DAILY_TARGET else 0,
        "unharvested_cities": fresh_cities,
    }


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
        # 0. How much room is there? Everything below is sized by this.
        headroom = max(0, MAX_ACTIVE - active_lead_count())
        detail["headroom_at_start"] = headroom

        # Nothing to add and nothing worth banking: stop before touching the
        # network at all. This is the whole point of the cap — when the list is
        # full, the generator does no work rather than quietly piling up leads
        # that will never be called.
        if headroom == 0 and pool_depth()["qualified"] >= POOL_FLOOR:
            detail["skipped"] = (
                f"call list is full ({MAX_ACTIVE}/{MAX_ACTIVE}) and the bank is stocked — "
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
            print(f"[leadgen] LEADGEN_RUN ok=True skipped=list_full "
                  f"active={MAX_ACTIVE}/{MAX_ACTIVE}", flush=True)
            return {"run_id": run_id, "ok": True, "promoted": 0, "enriched": 0,
                    "qualified": 0, "candidates_found": 0, "cities_harvested": 0,
                    "errors": 0, "detail": detail}

        # 1. Promote first, from what's already banked — never past the cap.
        promoted = promote_leads(min(target, headroom))

        # 2. Top the bank back up if it's getting shallow.
        depth = pool_depth()
        if depth["qualified"] < POOL_FLOOR:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM crm_leadgen_cities
                     WHERE enabled
                     ORDER BY last_harvested_at ASC NULLS FIRST
                     LIMIT %s
                """, (max_cities,))
                cities = [dict(r) for r in cursor.fetchall()]

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
                                   email_source=%s, score=%s, enriched_at=%s
                             WHERE id=%s
                        """, (result["status"], result["reject_reason"], result["email"],
                              result["email_source"], result["score"], now_iso(), cand["id"]))
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
        if promoted < target:
            remaining_room = max(0, MAX_ACTIVE - active_lead_count())
            if remaining_room:
                promoted += promote_leads(min(target - promoted, remaining_room))

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
          f"runway_days={detail['pool']['days_of_runway']}", flush=True)

    return {
        "run_id": run_id, "ok": ok, "promoted": promoted, "enriched": enriched,
        "qualified": qualified, "candidates_found": candidates,
        "cities_harvested": cities_done, "errors": errors, "detail": detail,
    }
