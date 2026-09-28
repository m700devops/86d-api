"""What a bar uses for inventory today, as one name — pure.

"They use our competitor, Margins Edge, they are satisfied" used to live only
inside a note line, so nobody could ask "which bars are on MarginEdge?" or
pitch a switch to all of them. `system_of()` reads any text (call notes, a
call's "How they do it now", an objection, a transcript) and returns the one
name it recognises, spelled the way the company spells it; the lead keeps it
in `crm_leads.current_system`. Unknown apps are left to the note — a name we
can't recognise is never guessed. See test_competitors.py.
"""

import re
from typing import Optional

# Name -> the ways people say or mistype it. Matched as whole words on
# lowercased text with punctuation flattened, so "Margins Edge", "margin-edge"
# and "MarginEdge" are one system.
SYSTEMS = {
    "MarginEdge": ("marginedge", "margin edge", "margins edge", "margin edges", "margins edges"),
    "BevSpot": ("bevspot", "bev spot"),
    "Partender": ("partender", "par tender"),
    "WISK": ("wisk", "wisk ai", "wisk.ai"),
    "Backbar": ("backbar", "back bar app", "backbar app"),
    "BinWise": ("binwise", "bin wise"),
    "Craftable": ("craftable", "bevager"),
    "Restaurant365": ("restaurant365", "restaurant 365", "r365"),
    "Sculpture Hospitality": ("sculpture hospitality",),
    "Accubar": ("accubar",),
    "Barventory": ("barventory",),
    "Toast Inventory": ("toast inventory", "xtrachef", "xtra chef"),
    "Square for Restaurants": ("square inventory",),
    "Lightspeed": ("lightspeed",),
    "meez": ("meez",),
    "Apicbase": ("apicbase",),
    "Crunchtime": ("crunchtime", "crunch time"),
    "Compeat": ("compeat",),
    "Optimum Control": ("optimum control",),
}
# By hand — not a product, but just as much an answer to "how do they do it".
MANUAL = {
    "Spreadsheet": ("spreadsheet", "excel", "google sheet", "google sheets"),
    "Pen and paper": ("clipboard", "pen and paper", "paper and pen", "by hand", "on paper",
                      "notebook"),
}


def _flat(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "


def _found(flat: str, table: dict) -> Optional[tuple]:
    best = None
    for name, spellings in table.items():
        for s in spellings:
            at = flat.find(" " + _flat(s).strip() + " ")
            if at >= 0 and (best is None or at < best[0]):
                best = (at, name)
    return best


def system_of(text: Optional[str]) -> Optional[str]:
    """The system a piece of text says the bar uses, or None.

    A product beats a manual method — "they count on paper and the owner
    orders through MarginEdge" is a MarginEdge bar — and between products
    the first one mentioned wins."""
    flat = _flat(text or "")[:200000]
    product = _found(flat, SYSTEMS)
    if product:
        return product[1]
    manual = _found(flat, MANUAL)
    return manual[1] if manual else None


def is_competitor(system: Optional[str]) -> bool:
    return system in SYSTEMS
