"""Who to ask for, and which email address is worth having.

Two small things that decide the order of the call list, both read only from
the venue's OWN website. Pure functions of (html, url) so they can be tested
against real pages without a network.

ON MANAGER NAMES — read this before trusting one.

A name here is NOT verified and cannot be. Restaurant managers turn over
constantly; the best this can honestly say is "on <date>, this venue's own
site said <name> was the <title>". So:

  * a name is only taken when a ROLE WORD sits right next to it (General
    Manager, Owner, Bar Manager, Beverage Director). A bare capitalised name
    on a page is a chef, a band, a supplier or a street — never a contact.
  * the page it came from and the date it was read are stored with it, so the
    caller can see the source and how old it is before using it.
  * it is never presented as confirmed. Asking "is Dave still the GM?" costs
    nothing when he isn't; asking "can I speak to Dave?" about someone who
    left two years ago is the thing that sounds stupid.

The extraction is deliberately narrow. Missing a real manager costs nothing —
the call still works without a name. Inventing one costs the call.
"""

import re
from html import unescape as _unescape
from typing import Optional

# Titles worth asking for: the people who actually own ordering and inventory.
# "Chef" is excluded on purpose — kitchen, not bar.
ROLE_WORDS = [
    "general manager", "gm", "owner", "co-owner", "proprietor",
    "bar manager", "beverage director", "beverage manager", "bar director",
    "managing partner", "operations manager", "restaurant manager", "manager",
]
_ROLE_ALT = "|".join(re.escape(r) for r in sorted(ROLE_WORDS, key=len, reverse=True))

# A person's name: one to three capitalised words, allowing O'Brien, McRae and
# Jean-Luc. Deliberately not matching ALL CAPS — that's a heading, not a name.
_NAME = r"[A-Z][a-z'’]+(?:[-'’][A-Z][a-z'’]+)?(?:\s+[A-Z][a-z'’.]+){0,2}"

# "Dave Smith, General Manager" / "Dave Smith - Owner"
_NAME_THEN_ROLE = re.compile(
    rf"\b({_NAME})\s*(?:,|[-–—]|\|)\s*({_ROLE_ALT})\b", re.I | re.X)
# "General Manager: Dave Smith" / "Owner — Dave Smith"
_ROLE_THEN_NAME = re.compile(
    rf"\b({_ROLE_ALT})\s*(?::|[-–—]|\|)\s*({_NAME})\b", re.I | re.X)
# "owner Dave Smith" / "our general manager Dave Smith"
_ROLE_SPACE_NAME = re.compile(rf"\b({_ROLE_ALT})\s+({_NAME})\b")

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t ]+")

# Words that look like names to a regex but never are. Mostly the surrounding
# furniture of a restaurant website.
_NOT_A_NAME = {
    "the", "our", "we", "us", "this", "that", "here", "home", "about", "contact",
    "menu", "menus", "hours", "location", "locations", "events", "private",
    "catering", "reservations", "book", "order", "online", "gift", "cards",
    "new", "sign", "up", "email", "phone", "team", "staff", "meet", "your",
    "general", "bar", "kitchen", "restaurant", "management", "please", "call",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


def _visible_text(html: str) -> str:
    """Tags out, whitespace normalised, line structure kept.

    Line structure matters: "Dave Smith" on one line and "General Manager" on
    the next is the single commonest way a real site prints this, and it only
    survives if the tag between them becomes a newline rather than nothing.
    """
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    text = re.sub(r"<\s*(br|/p|/div|/li|/h[1-6]|/td|/tr)\s*[^>]*>", "\n", text, flags=re.I)
    text = _ANY_TAG.sub(" ", text)
    # Full entity decoding, not a handful of replacements: "Sarah Chen &mdash;
    # Beverage Director" is a real shape and a hand-rolled list missed it.
    text = _unescape(text).replace("\u00a0", " ")
    text = _WS.sub(" ", text)
    return re.sub(r"\n\s*\n+", "\n", text)


def _plausible_name(candidate: str) -> bool:
    """Reject the things a name-shaped regex picks up that aren't people."""
    candidate = candidate.strip(" .,-–—")
    if not candidate or len(candidate) > 40:
        return False
    words = candidate.split()
    if not (1 <= len(words) <= 3):
        return False
    if any(w.lower().strip(".") in _NOT_A_NAME for w in words):
        return False
    # A single word is only a name when it's clearly one: "Dave", not "Bar".
    # Two words is the normal case and the one worth trusting most.
    if len(words) == 1 and len(candidate) < 3:
        return False
    return True


def _normalise_role(role: str) -> str:
    role = role.strip().lower()
    return "General Manager" if role == "gm" else role.title()


# Who to ask for when a page lists several. The owner signs off on a new
# system; a general manager runs the ordering; a shift manager can only take a
# message. Lower number wins.
ROLE_RANK = {
    "owner": 0, "co-owner": 0, "proprietor": 0, "managing partner": 0,
    "general manager": 1, "gm": 1,
    "beverage director": 2, "bar director": 2, "bar manager": 2,
    "beverage manager": 2,
    "operations manager": 3, "restaurant manager": 3, "manager": 4,
}
# A title line can carry more than the title: "Owner, Operator", "Owner & Chef".
_ROLE_LINE = re.compile(rf"^({_ROLE_ALT})(?:\s*[,/&|].*)?$", re.I)


def find_managers(html: str, url: Optional[str] = None) -> list[dict]:
    """Everyone on this page with a role worth asking for, best first.

    Best means the role that can actually say yes, not the one printed first.
    A real harvested page listed an owner, two general managers and a chef, in
    that order; taking the first match got a GM when the owner was right there.
    """
    text = _visible_text(html)
    if not text:
        return []

    found: list[dict] = []
    seen: set = set()

    def add(name: str, role: str):
        name = name.strip(" .,-\u2013\u2014")
        if not _plausible_name(name):
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        found.append({"name": name, "role": _normalise_role(role), "source": url,
                      "_rank": ROLE_RANK.get(role.strip().lower(), 9)})

    for match in _NAME_THEN_ROLE.finditer(text):
        add(match.group(1), match.group(2))
    for pattern in (_ROLE_THEN_NAME, _ROLE_SPACE_NAME):
        for match in pattern.finditer(text):
            add(match.group(2), match.group(1))

    # Name on one line, title on the next — the commonest layout on a real
    # "Our Team" page, and the only one that needs the line structure kept.
    lines = [ln.strip(" .,-\u2013\u2014|") for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    for i, line in enumerate(lines[:-1]):
        match = _ROLE_LINE.match(lines[i + 1])
        if match and _plausible_name(line):
            add(line, match.group(1))

    found.sort(key=lambda p: p["_rank"])
    for person in found:
        person.pop("_rank", None)
    return found


def find_manager(html: str, url: Optional[str] = None) -> Optional[dict]:
    """The single best person to ask for, or None.

    None is the expected answer most of the time and is completely fine — a
    lead without a name is still a lead. Measured over 22 reachable bar sites,
    one had a usable name. That's the shape of the web, not a bug: most
    independent bars don't publish who runs the floor.
    """
    people = find_managers(html, url)
    if not people:
        return None
    best = dict(people[0])
    # Two people with the same claim is worth knowing about: the page may be
    # out of date, or they may job-share. Say so rather than picking blind.
    same = [p["name"] for p in people if p["role"] == best["role"]]
    if len(same) > 1:
        best["also"] = same[1:]
    return best


# `also` is returned but deliberately not stored on the lead. The call script
# on the page is already "is <name> still the GM?", which handles a stale name
# without needing a second one, and another column for an edge case that
# appeared once in 22 sites isn't worth the schema.


# ── Email quality ───────────────────────────────────────────────────────────
#
# dave@divebar.com is worth several info@divebar.com. It reaches one person who
# can answer, it means somebody there has their own mailbox, and a reply to it
# is a conversation rather than a ticket.

ROLE_LOCALS = {
    "info", "contact", "hello", "hi", "mail", "email", "enquiries", "inquiries",
    "admin", "office", "reservations", "reservation", "bookings", "booking",
    "events", "catering", "orders", "order", "support", "help", "team",
    "general", "frontdesk", "host", "hostess", "marketing", "press", "media",
    "jobs", "careers", "hr", "accounts", "accounting", "billing", "webmaster",
    "chef", "kitchen", "takeout", "delivery", "gift", "giftcards", "shop",
    "noreply", "no-reply", "donotreply", "sales", "service", "customerservice",
}
# Role addresses that still reach a decision-maker directly.
OWNER_LOCALS = {
    "owner", "owners", "gm", "generalmanager", "manager", "management",
    "bar", "barmanager", "beverage", "gm1", "proprietor", "gerente",
}


def email_kind(email: Optional[str]) -> str:
    """'personal' | 'owner' | 'role' | 'unknown'.

    'personal' means the local part looks like one human's mailbox —
    dave@, dave.smith@, d.smith@, daveb@. That's the one worth calling first.
    """
    if not email or "@" not in email:
        return "unknown"
    local = email.split("@", 1)[0].strip().lower()
    bare = re.sub(r"[^a-z]", "", local)
    if not bare:
        return "unknown"
    if local in ROLE_LOCALS or bare in ROLE_LOCALS:
        return "role"
    if local in OWNER_LOCALS or bare in OWNER_LOCALS:
        return "owner"
    # A role word buried in a longer local part is still a role box:
    # "eventsandcatering@", "info.london@".
    for word in ROLE_LOCALS:
        if len(word) >= 5 and word in bare:
            return "role"
    # What's left that reads like a person: one name, two names joined by a dot
    # or underscore, or an initial plus a surname.
    if re.fullmatch(r"[a-z]{2,}[._-][a-z]{2,}", local):
        return "personal"
    # An initial and a surname: j.smith, jsmith.
    if re.fullmatch(r"[a-z]\.[a-z]{2,}", local):
        return "personal"
    # A single short token: dave, mike2, jsmith. The length cap matters —
    # without it this matched any run of letters, so `edelweisstavern@` and
    # `oshaughnessyspub@` were being called personal mailboxes and sorted to
    # the top of the list. Those are the venue's own name, which is a fine
    # address to have and not a named human.
    if re.fullmatch(r"[a-z]{3,12}\d{0,2}", local):
        return "personal"
    return "unknown"


# ── Addresses that are not worth having ─────────────────────────────────────

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
