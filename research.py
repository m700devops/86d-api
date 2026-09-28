"""Finding venues the operator asked for but didn't name — pure parts.

"Libbey's Coastal Kitchen … they have sister restaurants that use the same
technology. Add this to the CRM, and find the sister restaurants and add them"
came back as "I have no way to look them up — tell me their names". The AI bar
only ever saw the book. Now it can ask for research (assist.BAR_SCHEMA's
`research`), and crm._run_research does it in two steps:

1. ONE model call with Anthropic's server-side web search (`research_prompt`,
   parsed by `parse_found`). It runs WITHOUT structured outputs: web search
   answers carry citations, and citations and `output_config.format` can't be
   combined, so the JSON is asked for in the prompt and cut out of the text.
2. Every venue the model names is CHECKED in code before anything is saved —
   its own website must name it, the phone must be on that website, and the
   link to the first restaurant must show on a real page (theirs, the first
   restaurant's, or the source the model cited): `related_on_page`. A search
   result is a lead to check, never a fact.

Pure: no network, no database. See test_research.py.
"""

import json
import re
from typing import Optional

MAX_FOUND = 8            # venues checked per request; a group rarely runs more
MAX_SEARCHES = 6         # web searches the model may run for one request

RESEARCH_SYSTEM = """You research US bars and restaurants for a salesperson. You have web search.
Find exactly what you're asked for and nothing else. Only list a venue a real web page you
found says is connected the way you were asked (same owner, same restaurant group, a sister
restaurant, another location). Never guess, never pad the list, never list the venue you were
asked about. A venue that is permanently closed is left out.

Answer with ONE JSON object and nothing after it:
{"found": [{"name": "...", "city": "...", "state": "two-letter code", "website": "https://...",
"phone": "the venue's own number if a page shows it, else empty", "relation": "how it's
connected, in a few words", "source_url": "the page that says so"}],
 "note": "one sentence: what you searched and what you found (or didn't)"}
An empty "found" list is a correct answer when nothing checks out."""


MAX_PROSPECTS = 20       # venues one prospecting request may add

PROSPECT_SYSTEM = """You find US bars and restaurants for a salesperson who sells an iPhone app
for bar inventory to INDEPENDENT venues with a FULL BAR (spirits, not beer-and-wine only). You
have web search. Find venues that match what you're asked for, in the place you're asked about.
Leave out chains and franchises, hotel and casino bars, and bars on a main tourist strip. Only
list a venue a real web page you found shows exists and is open; never guess, never pad the
list, never repeat one.

Answer with ONE JSON object and nothing after it:
{"found": [{"name": "...", "city": "...", "state": "two-letter code", "website": "https://...
(the venue's OWN site, not a listing)", "phone": "the venue's own number if a page shows it,
else empty", "relation": "why it matches what was asked, in a few words", "source_url": "the
page that says so"}],
 "note": "one sentence: what you searched and what you found (or didn't)"}
An empty "found" list is a correct answer when nothing checks out."""


def prospect_prompt(find: str, loc: str, count: int) -> str:
    return (f"Where: {loc or 'anywhere in the US'}\n"
            f"Find: {find or 'independent cocktail bars and restaurants with a full bar'}\n"
            f"How many: up to {max(1, min(count or 10, MAX_PROSPECTS))}")


def research_prompt(about: str, loc: str, ask: str, website: Optional[str] = None) -> str:
    """The one research request: who, where, and what to find."""
    lines = [f"Venue: {about}" + (f", {loc}" if loc else "")]
    if website:
        lines.append(f"Their website: {website}")
    lines.append(f"Find: {ask or 'its sister restaurants (same owners or restaurant group)'}")
    return "\n".join(lines)


def _text(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse_found(text: str, limit: int = MAX_FOUND) -> tuple[list, str]:
    """(venues, note) from the model's answer: the JSON object in its text.

    Anything malformed is dropped, never repaired. A venue needs a name and a
    town; the rest may be empty and is checked later anyway."""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end < start:
        return [], ""
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return [], ""
    if not isinstance(data, dict):
        return [], ""
    found: list = []
    for item in data.get("found") or []:
        if not isinstance(item, dict):
            continue
        venue = {
            "name": _text(item.get("name"), 200),
            "city": _text(item.get("city"), 100),
            # Only a two-letter code: "Maryland" cut to two letters is "MA".
            "state": _text(item.get("state"), 20).upper(),
            "website": _text(item.get("website"), 300),
            "phone": _text(item.get("phone"), 40),
            "relation": _text(item.get("relation"), 200),
            "source_url": _text(item.get("source_url"), 500),
        }
        if not venue["name"] or not venue["city"]:
            continue
        if not re.fullmatch(r"[A-Z]{2}", venue["state"]):
            venue["state"] = ""
        for key in ("website", "source_url"):
            if venue[key] and not re.match(r"https?://", venue[key], re.I):
                venue[key] = ""
        if venue["name"].lower() not in {v["name"].lower() for v in found}:
            found.append(venue)
    return found[:limit], _text(data.get("note"), 500)


def related_on_page(page_text: str, name: str, words_of) -> bool:
    """Whether a page names this venue: every distinctive word of its name
    appears on it (`words_of` is leadgen's distinctive-words reader, passed in
    to keep this pure). Used both ways — the sister's site naming the first
    restaurant, the first restaurant's site naming the sister — and on the
    source page the model cited, which must name both."""
    wanted = set(words_of(name))
    if not wanted:
        return False
    seen = set(words_of(page_text or ""))
    return wanted <= seen


def lead_note(today: str, about: str, relation: str, source: str, their_words: str) -> str:
    """The note a found venue is saved with: how it was found and where it
    says so, then what the salesperson said — labelled as said about the
    FIRST restaurant, because nobody at this one has been spoken to."""
    first = f"[{today}] Found by the AI: {relation or 'connected to'} {about}"
    first += f" (source: {source})" if source else ""
    said = _text(their_words, 1500)
    if said:
        first += f"\n[{today}] From the call to {about}: {said}"
    return first
