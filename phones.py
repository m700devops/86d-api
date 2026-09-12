"""Strict US phone validation, because a wrong number dials a stranger.

OpenStreetMap phone tags are free text written by volunteers. Across real data
they carry extensions, two numbers in one field, international numbers, vanity
spellings and plain typos. Passing any of that to a dialer means ringing
somebody's house at 2pm and burning both a call slot and a bit of goodwill.

So this is deliberately strict and fails closed: anything it cannot prove is a
dialable North American number returns None, and a candidate whose phone
returns None is never promoted. Losing a real lead to over-caution costs one
lead. Dialling a stranger costs more than that.

What it can and cannot promise:
  CAN   — the digits form a structurally valid NANP number, with a real area
          code and exchange, not a reserved or fictional range, not an
          extension or a second number glued on by accident.
  CANNOT — that the number still belongs to that restaurant. Nothing short of
          dialling it proves that. Numbers change hands and OSM data ages, so
          `phone_checked` records where it came from and the caller sees the
          venue name before the number.
"""

import re
from typing import Optional

# Area codes that exist but should never be dialed from a lead list.
INVALID_NPA = {
    # N11 service codes, and the toll/premium ranges that aren't a venue line.
    "211", "311", "411", "511", "611", "711", "811", "911",
    "900",                      # premium rate
    "976",                      # premium rate
}
# Toll-free is legitimate for a business, but a toll-free number on a single
# independent bar is nearly always a booking platform or a franchise line —
# exactly the thing we filter elsewhere. Kept separate so the caller can decide.
TOLL_FREE_NPA = {"800", "833", "844", "855", "866", "877", "888"}

# Splits a field carrying more than one number, or a number plus an extension.
# `\bx\b` would not match the "x" in "x204" — x and 2 are both word
# characters, so there is no boundary between them — which left the letter in
# place and got the whole number rejected as a vanity spelling. Matching "x"
# followed by digits is what actually occurs in the wild.
_SPLIT_RE = re.compile(
    r"\s*(?:;|/|\bor\b|\band\b|,|\||\bext\b\.?|\bextension\b|\bx(?=\s*\d)|#)\s*", re.I
)
# Separator words removed before the vanity-letter test, so "ext" and "x204"
# don't read as letters in the number itself.
_WORDS_RE = re.compile(r"\b(?:ext|extension|or|and)\b|\bx(?=\s*\d)", re.I)


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def normalize_us_phone(raw: Optional[str]) -> Optional[str]:
    """Ten dialable digits, or None.

    None means "do not call this" and is never a soft failure — callers must
    treat it as disqualifying rather than falling back to the raw string.
    """
    if not raw or not isinstance(raw, str):
        return None

    # A letter that isn't part of a separator word means a vanity number
    # (1-800-FLOWERS). Translating those is guesswork; refuse instead.
    stripped = _WORDS_RE.sub(" ", raw)
    if re.search(r"[A-Za-z]", stripped):
        return None

    # Take only the first number in the field. "615-1111 / 615-2222" is two
    # venues' worth of digits and concatenating them dials neither.
    first = _SPLIT_RE.split(raw, maxsplit=1)[0]
    digits = _digits(first)

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    elif len(digits) == 11:
        return None                     # 11 digits not starting with 1 — not NANP
    if len(digits) != 10:
        return None                     # too short, too long, or international

    npa, nxx, line = digits[:3], digits[3:6], digits[6:]

    # NANP structure: area code and exchange both start 2-9, and neither may
    # be an N11 service code.
    if npa[0] in "01" or nxx[0] in "01":
        return None
    if npa in INVALID_NPA or nxx[1:] == "11":
        return None
    if npa[1] == "9" and npa[2] == "9" and npa[0] == "9":
        return None                     # 999 is unassigned

    # 555-0100..555-0199 is the reserved fictional block.
    if nxx == "555" and 100 <= int(line) <= 199:
        return None

    # Obvious placeholder data: all one digit, or a straight run.
    if len(set(digits)) == 1:
        return None
    if digits in ("1234567890", "0123456789", "9876543210"):
        return None

    return digits


def is_toll_free(digits: Optional[str]) -> bool:
    return bool(digits) and digits[:3] in TOLL_FREE_NPA


def format_us_phone(digits: Optional[str]) -> str:
    """(615) 742-9095 — for reading aloud, never for the dialer."""
    if not digits or len(digits) != 10:
        return ""
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
