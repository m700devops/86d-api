import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Optional


# ── Product match keys ───────────────────────────────────────────────────────
# The AI transcribes a label freshly on every scan, so the same bottle can come
# back as "Jack Daniel's" one time and "Jack Daniels" the next. Comparing raw
# strings makes those two different products; comparing on this key doesn't.
#
# Stripping every non-alphanumeric (rather than just lowercasing) is what folds
# apostrophes, hyphens and spacing together. NORM_SQL below must stay in lockstep
# with this function — the matcher compares values produced by one against values
# produced by the other, and a drift between them silently stops matching.

def normalize_match_text(value: Optional[str]) -> str:
    """Collapse a product name/brand into a comparison key."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


# Postgres equivalent of normalize_match_text, for matching inside a query.
# `{col}` is substituted with the column expression to normalize.
NORM_SQL = "regexp_replace(lower(coalesce({col}, '')), '[^a-z0-9]+', '', 'g')"


# ── The product match key (products.match_key) ─────────────────────────────
# normalize_match_text above has three blind spots, each of which turned a
# correct read into a second product:
#
# 1. It DELETES accented letters instead of folding them: "Patrón" -> "patrn",
#    "Patron" -> "patron". The prompt's spelling list names 25 accented products
#    (Patrón, every Añejo, Kahlúa, Jägermeister, Rémy Martin), the model writes
#    the accent or doesn't, and the seed catalog is plain ASCII.
# 2. A name that repeats the brand ("Jack Daniel's Old No. 7" / "Jack Daniel's")
#    never meets the same bottle read the way the prompt asks ("Old No. 7").
# 3. Sizes: 442 of the 457 seeded products are stored "Grey Goose Original 750ml"
#    / "Grey Goose", while the model is told to answer "Original" / "Grey Goose" —
#    so the seed catalog was unreachable from a scan, and the first scan of each
#    of those bottles created a new unverified product instead.
#
# This key folds accents, drops the brand from the front of the name, and drops
# sizes and pack counts. It deliberately does NOT drop class words ("Bourbon",
# "Rye", "Gin"): Bulleit Bourbon and Bulleit Rye differ only by one, and so do
# plenty of real products. The variant words are never loosened — "Citron" can
# never meet "Mandrin". Computed in Python only and STORED (products.match_key,
# kept current by database.reconcile_product_match_keys on every boot), so there
# is no SQL twin to drift from. test_match_key.py checks that no two seeded
# products share a key.

_SIZE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ml|cl|l|lt|ltr|liters?|litres?|oz|fl\.?\s*oz)\b"
    r"|\b\d+\s*-?\s*(?:pack|pk|ct|count)\b"
)


def fold_accents(value: Optional[str]) -> str:
    """ "Patrón Añejo" -> "Patron Anejo". Letters that don't decompose (ø, ß)
    are left as they are."""
    return "".join(c for c in unicodedata.normalize("NFKD", value or "")
                   if not unicodedata.combining(c))


def _match_words(value: Optional[str]) -> list:
    text = fold_accents(value).lower().replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", _SIZE_RE.sub(" ", text))


def product_match_key(name: Optional[str], brand: Optional[str]) -> str:
    """ "Grey Goose Original 750ml" / "Grey Goose" and "Original" / "Grey Goose"
    both -> "greygoose|original". A name that is only the brand is the brand's
    base product, which the prompt and the seed catalog both call "Original"."""
    brand_flat = "".join(_match_words(brand))
    words = _strip_brand(_match_words(name), brand_flat)
    return f"{brand_flat}|{''.join(words) or 'original'}"


def _strip_brand(words: list, brand_flat: str) -> list:
    """Drop the brand from the front of the name's words — repeatedly, since
    some seeded beers carry it twice ("Coors Coors Light 12oz" / "Coors") — on
    word boundaries but compared as run-together letters, so "J&B" meets "JB"
    and "Tito's" meets "Titos"."""
    while brand_flat and words:
        seen, cut = "", 0
        for i, word in enumerate(words):
            seen += word
            if seen == brand_flat:
                cut = i + 1
                break
            if not brand_flat.startswith(seen):
                break
        if not cut:
            break
        words = words[cut:]
    return words


def _one_edit_apart(a: str, b: str) -> bool:
    """True when a and b differ by at most one inserted, deleted or changed letter."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            edits += 1
            if edits > 1:
                return False
            if len(a) == len(b):
                i += 1
            j += 1
            continue
        i += 1
        j += 1
    return edits + (len(b) - j) + (len(a) - i) <= 1


def label_supports(name: Optional[str], brand: Optional[str], label_text: Optional[str]) -> Optional[bool]:
    """Does the label text the model wrote down contain the product name it
    returned? None when there is no label text to judge by (an older reply, or a
    provider that skipped the field) — no evidence is not counter-evidence.

    The prompt asks the model to transcribe the label BEFORE naming the product
    and to take the name only from those words. A name word missing from its own
    transcription is a name from memory — the Gatorade that was read as "Glacier
    Freeze" when the label said "Blue Bolt". Tolerant of what isn't recall:
    accents, case, punctuation, sizes, the brand repeated in the name, a word
    split differently ("Old No.7"), one wrong letter in a longer word
    ("Citroen"/"Citron"), and "Original", which is the convention for a base
    product and never printed as a variant."""
    label = _match_words(label_text)
    if not label:
        return None
    tokens = set(label)
    flat = "".join(label)
    words = _strip_brand(_match_words(name), "".join(_match_words(brand)))
    for word in words:
        if word == "original" or word in tokens:
            continue
        if len(word) >= 3 and word in flat:
            continue
        if len(word) >= 5 and any(_one_edit_apart(word, t) for t in tokens):
            continue
        return False
    return True


_SIZE_VALUE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(ml|cl|l|lt|ltr|liters?|litres?|fl\.?\s*oz|oz)\b")


def size_ml(text: Optional[str]) -> Optional[float]:
    """The first bottle size written in `text`, in millilitres: "750ml" -> 750,
    "1.75L" -> 1750, "12oz" -> 354.9. None when there isn't one."""
    match = _SIZE_VALUE_RE.search(fold_accents(text).lower())
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2)
    if unit == "ml":
        return value
    if unit == "cl":
        return value * 10
    if unit.startswith("l"):
        return value * 1000
    return value * 29.5735  # oz / fl oz


def sizes_compatible(a: Optional[float], b: Optional[float]) -> bool:
    """False only when BOTH sizes are known and differ — the key ignores sizes,
    so this is what stops a scan that read "1L" landing on the 750ml product.
    5% slack, so 12oz meets 355ml."""
    if a is None or b is None:
        return True
    return abs(a - b) <= 0.05 * max(a, b)


# Words that can differ between two readings of the SAME bottle without making it
# a different product: label furniture, age wording, and the class / region words
# a model may or may not fold into the name ("Old No. 7" vs "Old No. 7 Tennessee
# Whiskey", "Red" vs "Red Label", "12" vs "12 Year Old"). Deliberately small:
# nothing that ever names a variant — "rye", "light", "dry", "reserve", "ale" and
# above all "original" are NOT here, because "Bulleit" vs "Bulleit Rye" and
# "Bud Light" vs "Bud Light Lime" are different bottles.
_DESCRIPTOR_WORDS = frozenset({
    "label", "year", "years", "yr", "yrs", "old", "aged", "brand", "the", "and", "of",
    "vodka", "gin", "rum", "tequila", "mezcal", "whiskey", "whisky", "bourbon", "scotch",
    "cognac", "brandy", "liqueur", "beer", "lager", "wine",
    "tennessee", "kentucky", "straight", "sour", "mash", "blended", "single", "malt",
    "canadian", "irish", "american", "japanese", "mexican", "french",
})


def _reading_words(name: Optional[str], brand: Optional[str]) -> list:
    """Brand + name as comparable words. A name that is only the brand (or empty)
    is the base product, "original" — so a bare "Grey Goose" never agrees with
    "Grey Goose Le Citron"."""
    brand_words = _match_words(brand)
    name_words = _strip_brand(_match_words(name), "".join(brand_words)) or ["original"]
    return brand_words + name_words


def _covered(word: str, pool) -> bool:
    return word in pool or (len(word) >= 5 and any(_one_edit_apart(word, p) for p in pool))


def answers_agree(name_a: Optional[str], brand_a: Optional[str],
                  name_b: Optional[str], brand_b: Optional[str]) -> bool:
    """Do two providers' answers describe the same bottle?

    Agree when one reading's words are all in the other (one wrong letter allowed
    in a longer word) and whatever the longer one adds is only descriptor words
    (_DESCRIPTOR_WORDS) — "Jack Daniel's / Old No. 7" and "Jack Daniel's /
    Old No. 7 Tennessee Whiskey". Anything else is a disagreement: "Red Label" vs
    "Black Label", "Bud Light" vs "Bud Light Lime", 12 vs 15, a 750ml vs a 1L.

    Strict on purpose. A false disagreement costs a bartender one glance at a
    flagged row; a false agreement is exactly today's behaviour, an unflagged
    guess. Two answers that land on the same catalog product agree regardless —
    the caller checks that first."""
    if not sizes_compatible(size_ml(name_a) or size_ml(brand_a), size_ml(name_b) or size_ml(brand_b)):
        return False
    a, b = _reading_words(name_a, brand_a), _reading_words(name_b, brand_b)
    for small, big in ((a, b), (b, a)):
        if all(_covered(w, big) for w in small):
            extra = [w for w in big if not _covered(w, small)]
            if all(w in _DESCRIPTOR_WORDS for w in extra):
                return True
    return False


def _upce_to_upca(code: str) -> Optional[str]:
    """An 8-digit UPC-E (number system 0 or 1) written out as its 12-digit UPC-A."""
    if len(code) != 8 or code[0] not in "01":
        return None
    ns, x, check = code[0], code[1:7], code[7]
    last = x[5]
    if last in "012":
        body = x[0:2] + last + "0000" + x[2:5]
    elif last == "3":
        body = x[0:3] + "00000" + x[3:5]
    elif last == "4":
        body = x[0:4] + "00000" + x[4]
    else:
        body = x[0:5] + "0000" + last
    return ns + body + check


def _upca_to_upces(upca: str) -> set:
    """The 8-digit UPC-E codes that write out as this 12-digit UPC-A — so a can
    registered by its UPC-E is found when its UPC-A is read, and not only the
    other way round. Each candidate is kept only if it expands back exactly."""
    if len(upca) != 12 or upca[0] not in "01":
        return set()
    ns, m, p, check = upca[0], upca[1:6], upca[6:11], upca[11]
    candidates = [
        ns + m[0:2] + p[2:5] + m[2] + check,     # manufacturer ends x00, x in 0-2
        ns + m[0:3] + p[3:5] + "3" + check,       # manufacturer ends 00
        ns + m[0:4] + p[4] + "4" + check,         # manufacturer ends 0
        ns + m[0:5] + p[4] + check,               # product 5-9
    ]
    return {c for c in candidates if _upce_to_upca(c) == upca}


def clean_barcode(code: Optional[str]) -> Optional[str]:
    """How a barcode is stored: digits only for a numeric code typed with spaces
    or dashes ("0 12345 67890 5"), anything else as given, None when empty. The
    lookup matches every form of the digits, not stray punctuation."""
    raw = (code or "").strip()
    if not raw:
        return None
    compact = raw.replace(" ", "").replace("-", "")
    return compact if compact.isdigit() else raw


def barcode_variants(code: Optional[str]) -> list:
    """Every way the same product barcode can be written, so a lookup meets it
    however it was stored. A phone reads one printed code differently by format
    and platform: iOS reports a 12-digit UPC-A as a 13-digit EAN-13 with a
    leading 0, a GTIN can be padded to 14, and small cans carry an 8-digit UPC-E
    that stands for a 12-digit UPC-A. Anything that isn't 6-14 digits (a Code
    128 shelf tag) is only ever itself."""
    raw = (code or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not raw or digits != raw.replace(" ", "").replace("-", "") or not 6 <= len(digits) <= 14:
        return [raw] if raw else []
    cores = {digits.lstrip("0") or "0"}
    expanded = _upce_to_upca(digits)
    if expanded:
        cores.add(expanded.lstrip("0") or "0")
    out = {raw, digits}
    for core in cores:
        for width in (8, 12, 13, 14):
            if len(core) <= width:
                out.add(core.zfill(width))
    for form in list(out):
        out |= _upca_to_upces(form)
    return sorted(out)


def _row_ml(row: dict) -> Optional[float]:
    return size_ml(row.get("size")) or size_ml(row.get("name"))


def _edit_variants(word: str) -> set:
    """The word and, from 4 letters, each way of deleting one letter: two words
    one insert, delete or change apart always share one of these."""
    out = {word}
    if len(word) >= 4:
        out |= {word[:i] + word[i + 1:] for i in range(len(word))}
    return out


def _keeper_rank(row: dict) -> tuple:
    """Which of two copies of one bottle to keep: the catalog's own (a verified
    product can't be merged away), then the one this bar has set more on —
    price, par, distributor — then the most scanned, then the oldest."""
    set_here = sum((float(row.get("price") or 0) > 0,
                    float(row.get("par_quantity") or 0) > 0,
                    bool(row.get("has_distributor"))))
    return (-int(bool(row.get("verified"))), -set_here, -int(row.get("scan_count") or 0),
            str(row.get("created_at") or ""), str(row["id"]))


def same_bottle(a: dict, b: dict) -> bool:
    """Two catalog products that are one bottle: the same match key, or two
    readings answers_agree() would call the same ("Red" / "Red Label") — never
    two known sizes that differ (a bar may keep the 750ml and the 1L apart)."""
    if not sizes_compatible(_row_ml(a), _row_ml(b)):
        return False
    if a.get("match_key") and a.get("match_key") == b.get("match_key"):
        return True
    return answers_agree(a.get("name"), a.get("brand"), b.get("name"), b.get("brand"))


def duplicate_groups(rows: list) -> list:
    """One bar's products that are the same bottle twice, as
    [{"keep": row, "fold": [rows]}] — what to merge into what.

    The copies usually come from the scanner reading one label two ways before
    the matcher learned both. They cost the bar: a count split over two rows,
    and a scan that fits both of them can't use the bar's own book at all.

    A row joins a group only when it is the same bottle as the keeper. A
    suggestion has to be right to be worth a tap — a merge moves pars and
    prices — so anything that would be a guess is left out: a row that could
    belong to two groups (a sizeless "Original" beside the 750ml and the 1L),
    and a whole group whose copies disagree on size (a sizeless keeper with a
    750ml and a 1L copy: which one is it?). A verified product is never folded —
    the merge route refuses to retire one."""
    groups: list = []
    # Candidates, so each row is compared with the few keepers it could possibly
    # be (same_bottle stays the judge): comparing every row with every group
    # took seconds on a big bar's book, on a request thread. Two readings can
    # only agree when the smaller one's FIRST word is in the other — exactly, or
    # one letter off (_covered) — so keepers are indexed by each word and each
    # first word, with one-letter-deleted variants (two words one edit apart
    # always share one), and by match key.
    by_word: dict = {}
    by_first: dict = {}
    by_key: dict = {}

    def candidates(row) -> list:
        words = _reading_words(row.get("name"), row.get("brand"))
        found = set(by_key.get(row.get("match_key"), ()))
        for v in _edit_variants(words[0]):
            found |= by_word.get(v, set())
        for w in words:
            for v in _edit_variants(w):
                found |= by_first.get(v, set())
        return [groups[i] for i in sorted(found)]

    def index(i, row) -> None:
        words = _reading_words(row.get("name"), row.get("brand"))
        for w in words:
            for v in _edit_variants(w):
                by_word.setdefault(v, set()).add(i)
        for v in _edit_variants(words[0]):
            by_first.setdefault(v, set()).add(i)
        if row.get("match_key"):
            by_key.setdefault(row["match_key"], set()).add(i)

    for row in sorted(rows, key=_keeper_rank):
        homes = [] if row.get("verified") else [g for g in candidates(row) if same_bottle(g["keep"], row)]
        if len(homes) == 1:
            home = homes[0]
            if all(sizes_compatible(_row_ml(m), _row_ml(row)) for m in home["fold"]):
                home["fold"].append(row)
            else:
                home["contested"] = True
        elif not homes:
            groups.append({"keep": row, "fold": [], "contested": False})
            index(len(groups) - 1, row)
    return [{"keep": g["keep"], "fold": g["fold"]} for g in groups
            if g["fold"] and not g["contested"]]


def seed_display_name(name: str, brand: Optional[str]) -> str:
    """A seeded product's name the way the model is asked to write it — no
    brand in front, no size at the end: "Johnnie Walker Red Label 750ml" /
    "Johnnie Walker" -> "Red Label". Used to build the prompt's product list."""
    text = re.sub(r"\s*\b\d+(?:\.\d+)?\s*(?:ml|cl|l|oz)\s*$", "", name.strip(), flags=re.IGNORECASE)
    while brand and text.lower().startswith(brand.lower() + " "):
        text = text[len(brand) + 1:].strip()
    if brand and text.lower() == brand.lower():
        text = "Original"
    return text.strip() or "Original"

# ── Level classification ─────────────────────────────────────────────────────
# Boundaries (threshold, bucket_above, bucket_below) ordered highest-first.
_LEVEL_BOUNDARIES = [
    (0.875, "almost_full", "3/4"),
    (0.625, "3/4",         "half"),
    (0.375, "half",        "1/4"),
    (0.125, "1/4",         "empty"),
]

def generate_id() -> str:
    """Generate UUID v4 string"""
    return str(uuid.uuid4())

def now_iso() -> str:
    """Current time in ISO 8601 format (UTC)"""
    return datetime.now(timezone.utc).isoformat()

def level_to_decimal(level: str) -> float:
    """Convert level string to decimal value"""
    mapping = {
        "full": 1.0,
        "almost_full": 1.0,  # Backwards compatibility
        "3/4": 0.75,
        "half": 0.5,
        "1/4": 0.25,
        "empty": 0.0,
    }
    return mapping.get(level.lower(), 0.5)

def decimal_to_level(decimal: float) -> str:
    """Convert decimal to level string using hard thresholds (no hysteresis)."""
    if decimal >= 0.875:
        return "almost_full"
    elif decimal >= 0.625:
        return "3/4"
    elif decimal >= 0.375:
        return "half"
    elif decimal >= 0.125:
        return "1/4"
    else:
        return "empty"


def classify_level(
    level_decimal: float,
    previous_level: Optional[str] = None,
    hysteresis: bool = True,
    deadband: float = 0.03,
    confidence: Optional[float] = None,
) -> str:
    """Classify a liquid level decimal with optional hysteresis deadband.

    When hysteresis=True and previous_level is provided, a reading that falls
    within ±deadband of a boundary will stick with the previous classification
    rather than flip-flopping.  This prevents a stable half-full bottle from
    oscillating between 'half' and '3/4' due to small model noise.

    Confidence-aware stickiness: when confidence is provided and below 0.5,
    the effective deadband widens proportionally so low-confidence reads near
    a boundary stick harder to the previous label.  At confidence=0.0 the
    deadband doubles; at confidence=0.5 it is unchanged.

    Args:
        level_decimal:  Raw AI level float, clamped to 0.0–1.0.
        previous_level: Previously stored bucket label, if known.
        hysteresis:     Enable deadband near boundaries (default True).
        deadband:       Half-width of the deadband zone (default ±0.03,
                        configurable via LEVEL_DEADBAND env var in main.py).
        confidence:     AI confidence [0, 1]. When < 0.5 the deadband widens.

    Returns:
        Bucket label: 'almost_full' | '3/4' | 'half' | '1/4' | 'empty'
    """
    level_decimal = max(0.0, min(1.0, level_decimal))
    hard = decimal_to_level(level_decimal)

    if not hysteresis or previous_level is None:
        return hard

    # Already agree — nothing to do.
    if previous_level == hard:
        return hard

    # Widen the deadband when confidence is low (confidence < 0.5 → scale up).
    # Formula: effective = deadband * (2 - confidence * 2), clamped to [deadband, deadband*2].
    # confidence=0.5 → 1.0x, confidence=0.0 → 2.0x.
    effective_deadband = deadband
    if confidence is not None and confidence < 0.5:
        scale = 2.0 - confidence * 2.0  # linear: 1.0 at conf=0.5, 2.0 at conf=0.0
        effective_deadband = deadband * scale

    # Check if the reading is within effective deadband of any boundary.
    for threshold, above, below in _LEVEL_BOUNDARIES:
        if abs(level_decimal - threshold) < effective_deadband:
            # In the ambiguous zone: require stronger evidence to cross.
            if previous_level in (above, below):
                return previous_level
            break  # Only one boundary can be nearest; stop checking.

    return hard


def smooth_level(readings: list, window: int = 3) -> float:
    """Return the median of the last *window* readings, clamped to [0, 1].

    Used for temporal smoothing: pass the last N raw liquidLevel floats from
    the AI (including the current one) and get back a stable value for
    bucketing.  A single outlier read (glare, angle) won't flip the bucket.

    Args:
        readings: List of raw liquidLevel floats in chronological order.
        window:   How many recent readings to include (default 3).

    Returns:
        Median float in [0, 1].  Returns 0.0 for an empty list.
    """
    if not readings:
        return 0.0
    recent = readings[-window:]
    sorted_vals = sorted(recent)
    mid = len(sorted_vals) // 2
    if len(sorted_vals) % 2 == 0:
        return max(0.0, min(1.0, (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0))
    return max(0.0, min(1.0, sorted_vals[mid]))

def calculate_variance(
    current_usage: float,
    history: list[float]
) -> Optional[dict]:
    """
    Calculate variance alert if usage is unusual.
    Returns alert dict or None.
    
    Rules:
    - Alert if usage > avg * 2 (high usage)
    - Alert if usage < avg * 0.5 AND avg > 1 (low usage)
    """
    if not history:
        return None
    
    avg_usage = sum(history) / len(history)
    
    # Avoid division by zero
    if avg_usage == 0:
        if current_usage > 2:
            return {
                "type": "high",
                "current": current_usage,
                "average": avg_usage,
                "variance_percent": 100,
                "message": f"Used {current_usage} bottles (usually 0)"
            }
        return None
    
    variance_percent = ((current_usage - avg_usage) / avg_usage) * 100
    
    if current_usage > avg_usage * 2:
        return {
            "type": "high",
            "current": current_usage,
            "average": round(avg_usage, 1),
            "variance_percent": round(variance_percent, 0),
            "message": f"Used {current_usage} bottles vs avg {avg_usage:.1f} — possible theft or waste"
        }
    
    if current_usage < avg_usage * 0.5 and avg_usage > 1:
        return {
            "type": "low",
            "current": current_usage,
            "average": round(avg_usage, 1),
            "variance_percent": round(variance_percent, 0),
            "message": f"Used {current_usage} bottles vs avg {avg_usage:.1f} — slow week or counting error?"
        }
    
    return None

def generate_order_items(
    scans: list[dict],
    par_levels: dict[str, float]  # product_id -> par quantity
) -> list[dict]:
    """
    Generate order items by comparing current inventory to par levels.
    
    Returns list of items to order.
    """
    # Aggregate scans by product
    inventory = {}  # product_id -> total amount
    
    for scan in scans:
        product_id = scan["product_id"]
        # Level decimal + quantity (for backup bottles)
        amount = scan["level_decimal"] + (scan.get("quantity", 1) - 1)
        
        if product_id in inventory:
            inventory[product_id] += amount
        else:
            inventory[product_id] = amount
    
    # Calculate order quantities
    order_items = []
    
    for product_id, par in par_levels.items():
        current = inventory.get(product_id, 0)
        order_qty = max(0, par - current)
        
        if order_qty > 0:
            # Determine urgency
            if current == 0:
                urgency = "critical"
            elif order_qty >= par * 0.5:
                urgency = "moderate"
            else:
                urgency = "normal"
            
            order_items.append({
                "product_id": product_id,
                "current_amount": round(current, 2),
                "par_level": par,
                "order_quantity": round(order_qty, 0),
                "urgency": urgency
            })
    
    # Sort by urgency (critical first)
    urgency_order = {"critical": 0, "moderate": 1, "normal": 2}
    order_items.sort(key=lambda x: urgency_order[x["urgency"]])
    
    return order_items

FIRST_ORDER_NUMBER = 1001


def format_order_number(n: Optional[int]) -> Optional[str]:
    """`#1042`, or None for an order sent before numbers existed."""
    return f"#{int(n)}" if n else None


def _qty(n: float) -> str:
    return str(int(n)) if n == int(n) else f"{n:g}"


def _plural(n: float, word: str) -> str:
    return word if n == 1 else word + "s"


def _btl(n: float) -> str:
    return f"{_qty(n)} btl"


def case_size_of(item: dict) -> Optional[int]:
    """The pack size of a line ordered by the case, else None (a bottle line)."""
    if item.get("unit") != "case":
        return None
    size = item.get("case_size")
    return int(size) if size and int(size) >= 2 else None


def line_quantity(item: dict) -> str:
    """How one order line's amount reads: "2 cases (24 btl)", "1 case + 3 btl
    (15 btl)", "1 btl". `quantity` is always bottles; a case line spells out
    both, because "2 cases" alone is where a rep guesses the case size wrong."""
    qty = item["quantity"]
    pack = case_size_of(item)
    if not pack:
        return _btl(qty)
    cases, rest = divmod(qty, pack)
    cases = int(cases)
    if not cases:
        return _btl(qty)
    head = f"{cases} {_plural(cases, 'case')}" + (f" + {_btl(rest)}" if rest else "")
    return f"{head} ({_btl(qty)})"


def order_total(items: list[dict]) -> str:
    """"2 cases + 1 btl (25 btl)", or "3 btl" when nothing is by the case."""
    total = sum(i["quantity"] for i in items)
    cases = 0
    loose = 0.0
    for i in items:
        pack = case_size_of(i)
        if pack:
            c, r = divmod(i["quantity"], pack)
            cases += int(c)
            loose += r
        else:
            loose += i["quantity"]
    if not cases:
        return _btl(total)
    head = f"{cases} {_plural(cases, 'case')}" + (f" + {_btl(loose)}" if loose else "")
    return f"{head} ({_btl(total)})"


def delivery_label(day) -> Optional[str]:
    """A requested delivery date as the email shows it: "Fri, Oct 10"."""
    if not day:
        return None
    return f"{day.strftime('%a, %b')} {day.day}"


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def clean_delivery_days(raw) -> Optional[str]:
    """"Mon, thu" / "monday,Thursday" -> "mon,thu" (week order, no repeats).
    Blank -> None. Anything that isn't a weekday raises ValueError."""
    if raw is None or not str(raw).strip():
        return None
    days = set()
    for part in re.split(r"[\s,;/]+", str(raw).strip().lower()):
        if not part:
            continue
        day = part[:3]
        if day not in WEEKDAYS or not "monday tuesday wednesday thursday friday saturday sunday".split()[WEEKDAYS.index(day)].startswith(part):
            raise ValueError(f"not a weekday: {part!r}")
        days.add(day)
    return ",".join(d for d in WEEKDAYS if d in days) or None


def clean_account_number(raw) -> Optional[str]:
    """A bar's account number with a distributor, as typed: trimmed, a leading
    "#" or "Acct" dropped, one line, at most 40 characters. Blank -> None."""
    if raw is None:
        return None
    s = " ".join(str(raw).split())
    s = re.sub(r"^(?:acct\.?|account)\s*(?:no\.?|number)?\s*[:#]?\s*", "", s, flags=re.IGNORECASE)
    s = s.lstrip("#").strip()
    return s[:40] or None


DEFAULT_SENDER_TITLE = "Bar Manager"


def order_email(
    *,
    order_number: Optional[int],
    business_name: str,
    location_name: Optional[str],
    items: list[dict],
    sender_name: Optional[str],
    sender_title: Optional[str] = None,
    account_number: Optional[str] = None,
    deliver_by=None,
) -> dict:
    """One distributor's order email: {"subject", "text", "html"}.

    Laid out like the order card on the 86'd landing page: the order number in
    the subject, the bar's account number with this distributor and the date
    it wants delivery right under it, one line per item with its unit, and a
    closing that says who sent it and asks for the number on the invoice —
    what lets a bar match a delivery or a bill back to the order. Plain text
    and an HTML card carrying the same words; a mail app that won't show the
    HTML shows the text. `items` are {"name", "size", "quantity"} plus
    {"unit": "case", "case_size"} on a case line; `quantity` is bottles.
    The account number and delivery date are left out when there aren't any,
    and every value a bar typed is escaped before it goes into the HTML.
    """
    ref = format_order_number(order_number)
    where = business_name + (f" ({location_name})"
                             if location_name and location_name != business_name else "")
    subject = f"Order {ref} from {business_name}" if ref else f"Order from {business_name}"

    info = []
    if clean_account_number(account_number):
        info.append(f"Acct #{clean_account_number(account_number)}")
    deliver = delivery_label(deliver_by)
    if deliver:
        info.append(f"Deliver by {deliver}")

    rows = [(f"{i['name']}{' ' + i['size'] if i.get('size') else ''}", line_quantity(i)) for i in items]
    total = order_total(items)

    person = (sender_name or "").strip()
    title = (sender_title or "").strip() or DEFAULT_SENDER_TITLE
    if person and person != business_name:
        sent_by = f"Order sent by {person}, {title} at {where}."
    else:
        sent_by = f"Order sent by {where}."
    invoice = (f"Please put order {ref} on the invoice." if ref
               else "Please reference this order on the invoice.")
    closing = f"{sent_by} {invoice}"

    text_lines = [subject]
    text_lines += info
    text_lines.append("")
    text_lines += [f"- {name} — {qty}" for name, qty in rows]
    text_lines += ["", f"Total: {total}", "", closing, "", "Sent with 86'd bar inventory"]
    text = "\n".join(text_lines)

    html = _order_email_html(subject, info, rows, total, closing)
    return {"subject": subject, "text": text, "html": html}


def _order_email_html(subject: str, info: list[str], rows: list[tuple[str, str]],
                      total: str, closing: str) -> str:
    """The light order card. Tables and inline styles only: that's what every
    mail app renders the same way. Every value is escaped."""
    from html import escape as e
    chips = "".join(
        f'<span style="display:inline-block;margin:0 8px 8px 0;padding:4px 10px;border-radius:999px;'
        f'background:#f2ece4;color:#17181b;font-size:13px;font-weight:600;">{e(c)}</span>'
        for c in info)
    body_rows = "".join(
        f'<tr><td style="padding:10px 0;border-top:1px solid #ece7e0;color:#17181b;font-size:15px;">{e(n)}</td>'
        f'<td style="padding:10px 0;border-top:1px solid #ece7e0;color:#17181b;font-size:15px;'
        f'font-weight:700;text-align:right;white-space:nowrap;">{e(q)}</td></tr>'
        for n, q in rows)
    return (
        '<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f6f4f1;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;'
        'margin:0 auto;background:#ffffff;border:1px solid #e6e1da;border-radius:12px;">'
        '<tr><td style="padding:24px;">'
        f'<div style="font-size:18px;font-weight:700;color:#17181b;margin-bottom:12px;">{e(subject)}</div>'
        + (f'<div style="margin-bottom:8px;">{chips}</div>' if chips else '')
        + '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + body_rows
        + '<tr><td style="padding:12px 0 0;border-top:2px solid #17181b;color:#17181b;font-size:15px;'
          'font-weight:700;">Total</td><td style="padding:12px 0 0;border-top:2px solid #17181b;'
          f'color:#17181b;font-size:15px;font-weight:700;text-align:right;white-space:nowrap;">{e(total)}</td></tr>'
        + '</table>'
        f'<p style="margin:20px 0 0;color:#3a3b40;font-size:14px;line-height:1.5;">{e(closing)}</p>'
        '</td></tr></table>'
        '<p style="max-width:560px;margin:12px auto 0;color:#8a8580;font-size:12px;text-align:center;">'
        "Sent with 86&#x27;d bar inventory</p>"
        '</body></html>'
    )


# ── How fast a bar goes through each bottle, from its own sent orders ────────
#
# The app rounds a shortfall up to a full case only when the extra bottles
# would be used soon, so it needs a usage rate per bottle. A bar that orders
# back up to par is, in each order, replacing what it used since the last one:
# so the bottles in every order AFTER the first, divided by the time from the
# first order to the last, is its rate. (The first order's own quantity covers
# a stretch before the window and is left out.) Fewer than two orders, or less
# than USAGE_MIN_SPAN_DAYS between them, is no rate at all — the app then falls
# back to the bottle's par, never a guess dressed up as data.

USAGE_WINDOW_DAYS = 56
USAGE_MIN_SPAN_DAYS = 6


def parse_iso(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _usage_name_key(name) -> str:
    return " ".join(str(name or "").lower().split())


def order_usage(orders: list) -> dict:
    """orders: [(sent_at datetime, order_data dict)], any order.

    Returns {"span_days": float, "products": {product_id: bottles},
    "names": {lowercased line name: bottles}}, counting only orders after the
    first. `names` covers lines from builds that sent no product_id. A line's
    quantity is bottles, case lines included. span_days 0 = no usable rate.
    """
    dated = sorted((o for o in orders if o[0] is not None), key=lambda o: o[0])
    empty = {"span_days": 0, "products": {}, "names": {}}
    if len(dated) < 2:
        return empty
    span = (dated[-1][0] - dated[0][0]).total_seconds() / 86400
    if span < USAGE_MIN_SPAN_DAYS:
        return empty
    products: dict = {}
    names: dict = {}
    for _, data in dated[1:]:
        for dist in (data or {}).get("distributors") or []:
            if dist.get("status") not in (None, "sent"):
                continue          # a line that never reached the distributor wasn't ordered
            for item in dist.get("items") or []:
                try:
                    qty = float(item.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                if qty <= 0:
                    continue
                pid = item.get("product_id")
                if pid:
                    products[pid] = products.get(pid, 0) + qty
                key = _usage_name_key(item.get("name"))
                if key:
                    names[key] = names.get(key, 0) + qty
    return {"span_days": round(span, 2), "products": products, "names": names}
