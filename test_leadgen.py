"""The restaurant liquor gate: a restaurant-tagged OSM venue must show real
evidence it pours alcohol before it can be promoted to a lead.

`_restaurant_pours()` is what enrich_candidate() calls for any harvested
`amenity=restaurant` venue (bars/pubs/nightclubs skip this gate entirely —
see score_candidate, which scores them "pours liquor by definition"). A real
harvested pizzeria with zero alcohol on the premises reached the call list
because the old LIQUOR_HINTS regex matched words a kitchen-only menu also
uses: bare "cocktail" matched "shrimp cocktail"/"fruit cocktail", "bar menu"
matched "salad bar menu", "spirits" (no word boundary) matched "spirited",
"shots" matched "screenshot", and bare "draft"/"happy hour" matched an
NFL-watch-party page or a lunch special with no alcohol involved at all.

Pure function of (crawled text, OSM tags) — no DB, network or clock — so it's
tested directly here rather than through enrich_candidate(), which crawls a
real site.
"""
import sys
import types

# leadgen imports `database`, which raises at import time without DATABASE_URL.
if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

from leadgen import (  # noqa: E402
    _restaurant_pours, _on_tourist_strip, score_candidate, ASIAN_CUISINE_HINTS,
)


def _rejected(html="", tags=None):
    ok, reason = _restaurant_pours(html, tags or {})
    assert ok is False, f"expected rejection, qualified instead (reason={reason!r})"
    return reason


def _qualified(html="", tags=None):
    ok, reason = _restaurant_pours(html, tags or {})
    assert ok is True, f"expected qualified, rejected instead (reason={reason!r})"


# ── The actual bug: kitchen-only menus that used to slip through ───────────

def test_shrimp_cocktail_does_not_qualify():
    html = "Appetizers: Shrimp Cocktail, Garlic Bread, Mozzarella Sticks"
    assert _rejected(html) == "restaurant with no sign of a bar programme"


def test_fruit_cocktail_does_not_qualify():
    html = "Kids menu includes a side of fruit cocktail or applesauce"
    _rejected(html)


def test_salad_bar_menu_does_not_qualify():
    html = "Check out our fresh salad bar menu, included with every pizza"
    _rejected(html)


def test_espresso_shot_and_screenshot_do_not_qualify():
    html = "Add an espresso shot to any coffee. Screenshot this coupon for 10% off."
    _rejected(html)


def test_team_spirit_does_not_qualify():
    html = "Our staff bring true team spirit to every game-day watch party."
    _rejected(html)


def test_nfl_draft_party_alone_does_not_qualify():
    html = "Join us for our annual NFL Draft watch party, pizza specials all night."
    _rejected(html)


def test_bare_happy_hour_alone_does_not_qualify():
    html = "Happy Hour 3-5pm: $2 off any large pizza slice."
    _rejected(html)


def test_bare_drink_menu_alone_does_not_qualify():
    html = "See our full drink menu: soda, lemonade, iced tea, milkshakes."
    _rejected(html)


def test_no_html_and_no_tag_does_not_qualify():
    assert _rejected("") == "restaurant with no sign of a bar programme"


# ── Real bar programs still qualify ─────────────────────────────────────────

def test_full_bar_qualifies():
    _qualified("Join us for dinner and drinks — we have a full bar.")


def test_craft_cocktail_menu_qualifies():
    _qualified("Our craft cocktail menu changes seasonally.")


def test_wine_list_qualifies():
    _qualified("Ask your server about our extensive wine list.")


def test_named_liquor_qualifies():
    for word in ("tequila", "bourbon", "mezcal", "whiskey", "whisky", "vodka", "rum", "gin"):
        _qualified(f"We pour a wide selection of {word}.")


def test_draft_beer_qualifies():
    _qualified("12 rotating craft beers, draft beer and bottles.")


def test_tap_list_qualifies():
    _qualified("Check our tap list for this week's rotation.")


def test_osm_bar_tag_qualifies_with_no_site_text():
    _qualified("", {"bar": "yes"})


def test_osm_cocktail_tag_qualifies_with_no_site_text():
    _qualified("", {"drink:cocktail": "yes"})


# ── The explicit "we don't serve alcohol" override ──────────────────────────

def test_byob_overrides_a_cocktail_mention():
    html = "BYOB — we don't have a liquor license, but feel free to bring your own wine."
    reason = _rejected(html)
    assert reason == "site says no alcohol served"


def test_byob_overrides_the_osm_bar_tag():
    # A stale/incorrect OSM edit says bar=yes; the venue's own site is the
    # more authoritative source on whether IT sells alcohol.
    html = "We are BYOB with no liquor license on site."
    reason = _rejected(html, {"bar": "yes"})
    assert reason == "site says no alcohol served"


def test_explicit_no_alcohol_statement_overrides():
    html = "Family friendly dining. We do not serve alcohol at this location."
    reason = _rejected(html)
    assert reason == "site says no alcohol served"


def test_case_insensitive():
    _qualified("WE HAVE A FULL BAR AND CRAFT COCKTAILS.")
    _rejected("WE DO NOT SERVE ALCOHOL.")


# ── Tourist-strip address penalty (criterion 6: not mainstream tourist bars) ─
# These are already-crowded pitches — a resort bar on the Strip or a honky-tonk
# on Lower Broadway almost certainly already runs some system, whatever it is.
# It's a scoring penalty like POS_STACK_HINTS, never a reject: a real
# independent bar on one of these blocks still makes the list, just lower.

def test_flags_a_venue_on_the_vegas_strip():
    assert _on_tourist_strip({"addr:street": "Las Vegas Blvd S"}, "Las Vegas") is True


def test_flags_a_venue_on_lower_broadway_nashville():
    assert _on_tourist_strip({"addr:street": "Broadway"}, "Nashville") is True


def test_does_not_flag_broadway_in_a_city_with_no_strip_defined():
    # "Broadway" is an ordinary street name in plenty of towns — only the
    # metros with a curated strip in TOURIST_STRIP_STREETS get checked.
    assert _on_tourist_strip({"addr:street": "Broadway"}, "Columbus") is False


def test_does_not_flag_a_side_street_off_the_strip():
    assert _on_tourist_strip({"addr:street": "Sahara Ave"}, "Las Vegas") is False


def test_no_address_tag_never_flags():
    assert _on_tourist_strip({}, "Las Vegas") is False


def test_no_city_never_flags():
    assert _on_tourist_strip({"addr:street": "Las Vegas Blvd"}, None) is False


def test_tourist_strip_lowers_score_but_does_not_zero_it():
    tags_off_strip = {"amenity": "bar", "addr:street": "Sahara Ave"}
    tags_on_strip = {"amenity": "bar", "addr:street": "Las Vegas Blvd"}
    off = score_candidate(tags_off_strip, None, "", None, "Las Vegas")
    on = score_candidate(tags_on_strip, None, "", None, "Las Vegas")
    assert on == off - 4   # a penalty, not a rejection — no exception, no zeroing


# ── Asian-cuisine scoring penalty (read off the OSM `cuisine` tag directly) ──
# Same "probably already has a system" idea as UPSCALE_HINTS/POS_STACK_HINTS,
# just from a third signal (what kind of restaurant, not words or location).

def test_flags_sushi_cuisine_tag():
    assert ASIAN_CUISINE_HINTS.search("sushi") is not None


def test_flags_semicolon_joined_cuisine_list():
    # OSM cuisine values are often ';'-joined, e.g. "asian;noodle;ramen"
    assert ASIAN_CUISINE_HINTS.search("noodle;ramen;asian") is not None


def test_does_not_flag_unrelated_cuisine():
    assert ASIAN_CUISINE_HINTS.search("italian;pizza") is None


def test_asian_cuisine_lowers_score_but_does_not_zero_it():
    tags_western = {"amenity": "restaurant", "cuisine": "american"}
    tags_asian = {"amenity": "restaurant", "cuisine": "japanese;sushi"}
    western = score_candidate(tags_western, None, "")
    asian = score_candidate(tags_asian, None, "")
    assert asian == western - 2   # a penalty, not a rejection


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
