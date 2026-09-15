"""What's true about a venue, for the thirty seconds before you dial.

Cold-calling a bar goes better when the first sentence proves you looked. Not
flattery — a real detail: they run seven days until two, they brew their own,
they've been there since 1978. It changes the call from a stranger reading a
list to somebody who did their homework.

Every fact here is EXTRACTED, never inferred and never generated. Two sources,
both attributable:

  * the OpenStreetMap tags harvested with the venue (address, cuisine, hours,
    brewery, min_age, levels, start_date)
  * phrases matched in the venue's OWN website text

Nothing is written that can't be pointed at. The reason is the phone call: a
brief that says "you've been open since 1987" to somebody who opened in 2019
is worse than no brief at all — it's the moment they decide you're reading a
script. So every fact carries where it came from, and anything the model later
writes on top of these is built only from them.
"""

import json
import re
from typing import Optional

# Phrases a venue writes about itself. Each pattern captures the number or
# name that makes the fact concrete, so nothing is reported vaguely.
_SITE_PATTERNS = [
    ("established", re.compile(
        r"\b(?:since|established(?:\s+in)?|est\.?|serving\s+\w+\s+since|opened(?:\s+in)?)\s+"
        r"((?:18|19|20)\d{2})\b", re.I)),
    ("taps", re.compile(r"\b(\d{2,3})\s*(?:craft\s+)?(?:beers?\s+on\s+)?(?:taps?|draft lines|draught)\b", re.I)),
    ("seats", re.compile(r"\b(?:seats?|seating(?:\s+for)?|capacity(?:\s+of)?)\s+(\d{2,4})\b", re.I)),
    ("locations", re.compile(r"\b(\d{1,2})\s+locations\b", re.I)),
    ("floors", re.compile(r"\b(two|three|second|third)[- ](?:floor|story|storey|level)\b", re.I)),
]

# Things worth knowing that are simply present or absent on the page.
_SITE_FLAGS = [
    ("private_events", re.compile(r"private (?:event|part(?:y|ies)|hire|dining)|book the (?:space|room)|buyout", re.I)),
    ("live_music", re.compile(r"live music|live band|open mic|dj\b", re.I)),
    ("brunch", re.compile(r"\bbrunch\b", re.I)),
    ("catering", re.compile(r"\bcatering\b", re.I)),
    ("gift_cards", re.compile(r"gift cards?\b", re.I)),
    ("multiple_locations", re.compile(r"our locations|all locations|other locations", re.I)),
]

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_ANY_TAG = re.compile(r"<[^>]+>")


def _visible(html: str) -> str:
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    text = _ANY_TAG.sub(" ", text)
    return re.sub(r"\s+", " ", text)


def weekly_hours(opening_hours: Optional[str]) -> Optional[dict]:
    """How hard this place runs, from its own posted hours.

    Days open and hours a week is the closest thing to a volume signal that
    costs nothing: a bar open seven days until 2am pours several times what a
    Thursday-to-Sunday room does, and pours are what there is to count.
    """
    from callwindow import parse_opening_hours
    schedule = parse_opening_hours(opening_hours)
    if not schedule:
        return None
    days = 0
    minutes = 0
    latest = 0
    for day in range(7):
        spans = schedule.get(day) or []
        if not spans:
            continue
        days += 1
        for start, end in spans:
            minutes += max(0, end - start)
            latest = max(latest, end)
    if not days:
        return None
    return {"days_open": days, "hours_per_week": round(minutes / 60),
            "latest_close_min": latest}


def extract_facts(tags: Optional[dict], site_html: str = "",
                  opening_hours: Optional[str] = None) -> dict:
    """Everything attributable about this venue, as {key: {value, source}}."""
    tags = tags or {}
    facts: dict = {}

    def add(key, value, source):
        if value not in (None, "", []):
            facts[key] = {"value": value, "source": source}

    OSM = "OpenStreetMap"
    SITE = "their website"

    kind = tags.get("amenity")
    if tags.get("brewery") == "yes" or tags.get("microbrewery") == "yes":
        kind = "brewpub"
    add("kind", kind, OSM)
    add("cuisine", (tags.get("cuisine") or "").replace("_", " ").replace(";", ", ") or None, OSM)

    street = " ".join(x for x in [tags.get("addr:housenumber"), tags.get("addr:street")] if x)
    add("address", street or None, OSM)
    add("neighbourhood", tags.get("addr:suburb") or tags.get("addr:neighbourhood"), OSM)

    hours = weekly_hours(opening_hours or tags.get("opening_hours"))
    if hours:
        add("days_open", hours["days_open"], OSM)
        add("hours_per_week", hours["hours_per_week"], OSM)
        if hours["latest_close_min"] >= 24 * 60 + 60:      # past 1am
            closing = hours["latest_close_min"] % (24 * 60)
            hour12 = (closing // 60) % 12 or 12
            add("late_close", f"{hour12}am", OSM)

    if tags.get("start_date"):
        year = re.search(r"(18|19|20)\d{2}", str(tags["start_date"]))
        if year:
            add("established", year.group(0), OSM)
    if tags.get("building:levels") and str(tags["building:levels"]).isdigit():
        levels = int(tags["building:levels"])
        if levels > 1:
            add("levels", levels, OSM)
    add("outdoor_seating", True if tags.get("outdoor_seating") == "yes" else None, OSM)
    add("over_21", True if str(tags.get("min_age")) == "21" else None, OSM)
    add("operator", tags.get("operator") or tags.get("brand"), OSM)

    text = _visible(site_html)
    if text:
        for key, pattern in _SITE_PATTERNS:
            match = pattern.search(text)
            if match:
                # A venue's own claim, so keep the phrase it made it in — the
                # caller can then repeat it back in the venue's own words.
                snippet = text[max(0, match.start() - 20):match.end() + 20].strip()
                add(key, {"value": match.group(1), "quote": snippet}, SITE)
        for key, pattern in _SITE_FLAGS:
            if pattern.search(text):
                add(key, True, SITE)

    return facts


def facts_to_lines(facts: dict) -> list:
    """Facts as short readable lines, each carrying where it actually came from.

    The source is read off the fact, never assumed. An early version hardcoded
    "their website" next to the year a venue opened, and then backfilled 307
    leads from map tags alone — every one of them credited to a website nobody
    had read. On a phone call that's the difference between "your site says
    you've been there since 1974" and asserting a date you got from a map.
    """
    if not facts:
        return []
    out = []

    def get(key):
        item = facts.get(key)
        if not item:
            return None, None
        value = item["value"]
        if isinstance(value, dict):
            return value.get("value"), item.get("source")
        return value, item.get("source")

    def line(text, source):
        if text:
            out.append({"text": text, "source": source or "OpenStreetMap"})

    kind, kind_src = get("kind")
    cuisine, cuisine_src = get("cuisine")
    if kind or cuisine:
        line(" ".join(x for x in [cuisine, kind] if x), cuisine_src or kind_src)

    established, est_src = get("established")
    if established:
        # The wording itself hedges by source. A venue saying "since 1974" on
        # its own site is a claim you can repeat back; a year off a map is
        # something to ask about, not assert.
        line(f"open since {established}" if est_src == "their website"
             else f"map lists it opening {established}", est_src)

    days, days_src = get("days_open")
    hours, _ = get("hours_per_week")
    late, _ = get("late_close")
    if days:
        text = f"{days} days a week"
        if hours:
            text += f", about {hours} hours"
        if late:
            text += f", last call around {late}"
        line(text, days_src)

    taps, taps_src = get("taps")
    if taps:
        line(f"{taps} taps", taps_src)
    seats, seats_src = get("seats")
    if seats:
        line(f"seats about {seats}", seats_src)
    locations, loc_src = get("locations")
    if locations:
        line(f"{locations} locations", loc_src)
    levels, levels_src = get("levels")
    if levels:
        line(f"{levels} floors", levels_src)

    for key, label in [("over_21", "21 and over"),
                       ("outdoor_seating", "outdoor seating"),
                       ("private_events", "does private events"),
                       ("live_music", "live music"),
                       ("brunch", "serves brunch"),
                       ("multiple_locations", "more than one location")]:
        value, source = get(key)
        if value:
            line(label, source)

    # The thing most worth knowing before you open your mouth.
    platform, plat_src = get("runs_platform")
    if platform:
        line(f"their site runs {platform} — ask what it does for inventory", plat_src)
    if get("upscale")[0]:
        line("white-tablecloth signals on the site", get("upscale")[1])
    if get("neighbourhood")[0]:
        line("neighbourhood room (pool, happy hour, taps)", get("neighbourhood")[1])

    operator, op_src = get("operator")
    if operator:
        line(f"operated by {operator}", op_src)
    address, addr_src = get("address")
    if address:
        line(address, addr_src)
    return out


def dumps(facts: dict) -> Optional[str]:
    return json.dumps(facts)[:6000] if facts else None


def loads(raw: Optional[str]) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
