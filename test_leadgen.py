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
import json
import sys
import types

# leadgen imports `database`, which raises at import time without DATABASE_URL.
if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

from leadgen import (  # noqa: E402
    _restaurant_pours, _on_tourist_strip, score_candidate, ASIAN_CUISINE_HINTS,
    _map_fit_penalty, _rescore_map_penalties_once,
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
    assert _rejected(html) == "no sign on their own site that they pour liquor"


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
    assert _rejected("") == "no sign on their own site that they pour liquor"


# ── Real bar programs still qualify ─────────────────────────────────────────

def test_full_bar_qualifies():
    _qualified("Join us for dinner and drinks — we have a full bar.")


def test_craft_cocktail_menu_qualifies():
    _qualified("Our craft cocktail menu changes seasonally.")


def test_named_liquor_qualifies():
    for word in ("tequila", "bourbon", "mezcal", "whiskey", "whisky", "vodka", "rum", "gin"):
        _qualified(f"We pour a wide selection of {word}.")


# ── Beer and wine are not liquor (the owner's rule, 2026-09-25) ─────────────
# A beer-and-wine room has no back bar to count. These all used to qualify.

def test_wine_list_draft_beer_and_tap_list_do_not_qualify():
    for html in ("Ask your server about our extensive wine list.",
                 "12 rotating craft beers, draft beer and bottles.",
                 "Check our tap list for this week's rotation.",
                 "Mimosas, sangria and a Bloody Mary bar at brunch."):
        _rejected(html)


def test_beer_and_wine_only_says_so():
    for html in ("We serve beer and wine only.", "Soju cocktails and Korean BBQ",
                 "Our wine-based cocktails", "Agave wine margaritas every day",
                 "Craft cocktails made with our beer & wine license"):
        assert _rejected(html).startswith("beer and wine only"), html


def test_osm_bar_or_beer_tags_are_not_liquor():
    _rejected("", {"bar": "yes"})
    _rejected("", {"drink:beer": "yes", "drink:wine": "yes"})


def test_one_spirits_tag_is_not_enough_for_a_restaurant():
    _rejected("", {"drink:cocktail": "yes"})
    _qualified("A classic Negroni", {"drink:cocktail": "yes"})


# ── The Sweedeedee bug: raw HTML ────────────────────────────────────────────

SQUARESPACE_SCRIPT = ('<script>window.countries=[{"name":"Martinique","code":"MQ"},'
                      '{"name":"Gin Islands"},"bourbon","full bar"]</script>')


def test_words_in_scripts_styles_and_markup_never_count():
    # Every Squarespace page carries a country picker in a script; its
    # "Martinique" matched "martini", so every Squarespace restaurant poured.
    _rejected(SQUARESPACE_SCRIPT + "<p>Pie, coffee and brunch. Open 8-3.</p>")
    _rejected('<style>.full-bar{}</style><!-- cocktail menu --><img alt="x">'
              '<p>Breakfast all day</p>')


def test_food_named_after_a_spirit_is_not_a_spirit():
    for html in ("Bourbon pecan pie and whiskey glaze", "Penne alla vodka, vodka sauce",
                 "Rum cake, rum raisin ice cream", "Margarita pizza", "Scotch eggs",
                 "Straight from Bourbon Street",
                 "Red Velvet Whiskey Pop Tart with whiskey cream cheese filling"):
        _rejected(html)


def test_real_bar_programmes_qualify():
    for html in ("Tequila, lime, triple sec: our house margarita",
                 "Beer, wine & spirits", "Espresso martini and a Negroni",
                 "Happy hour well drinks $5", "Our whiskey list runs to 200 bottles"):
        _qualified(html)


def test_a_bar_needs_one_spirit_a_restaurant_two():
    from leadgen import liquor_verdict
    assert liquor_verdict("whiskey and cold beer", {}, "Joe's", "bar")["status"] == "spirits"
    assert liquor_verdict("whiskey and cold beer", {}, "Joe's", "restaurant")["status"] == "unknown"
    # A taproom or wine bar is tagged a bar too, and is held to the restaurant bar.
    assert liquor_verdict("whiskey and cold beer", {}, "Hopworks Taproom", "bar")["status"] == "unknown"
    assert liquor_verdict("whiskey", {"microbrewery": "yes"}, "Kells", "pub")["status"] == "unknown"


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



# ── One-time rescore of rows scored before the map penalties existed ──────────

class _FakeCursor:
    """Just enough of a DB cursor for `_rescore_map_penalties_once()`: one
    marker table, candidate rows, lead rows, and the queries it runs."""

    def __init__(self, cands, leads):
        self.cands = {c["id"]: c for c in cands}
        self.leads = {l["id"]: l for l in leads}
        self.markers = set()
        self._result = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self._result = []
        if s.startswith("CREATE TABLE"):
            return
        if s.startswith("INSERT INTO crm_leadgen_oneshots"):
            if params[0] not in self.markers:
                self.markers.add(params[0])
                self._result = [{"name": params[0]}]
        elif s.startswith("SELECT c.id"):
            (cutoff,) = params
            for c in self.cands.values():
                if c["status"] not in ("qualified", "promoted"):
                    continue
                if not c["enriched_at"] or c["enriched_at"] >= cutoff:
                    continue
                lead = self.leads.get(c.get("promoted_lead_id"))
                unworked = bool(lead and lead["status"] == "new"
                                and lead["last_touch_at"] is None)
                self._result.append({**c, "lead_unworked": unworked})
        elif s.startswith("UPDATE crm_lead_candidates"):
            self.cands[params[1]]["score"] += params[0]
        elif s.startswith("UPDATE crm_leads"):
            lead = self.leads[params[1]]
            lead["lead_score"] = (lead["lead_score"] or 0) + params[0]
        elif s.startswith("UPDATE crm_leadgen_oneshots"):
            return
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


_OLD = "2026-09-20T10:00:00+00:00"
_NEW = "2026-09-25T10:00:00+00:00"
_STRIP = json.dumps({"addr:street": "Las Vegas Blvd S"})
_SUSHI = json.dumps({"cuisine": "sushi"})


def _cand(id, status, tags, enriched=_OLD, lead=None, score=10, city="Las Vegas"):
    return {"id": id, "status": status, "city": city, "raw_tags": tags,
            "enriched_at": enriched, "promoted_lead_id": lead, "score": score}


def _lead(id, status="new", touched=None, score=10):
    return {"id": id, "status": status, "last_touch_at": touched, "lead_score": score}


def test_rescore_applies_penalty_to_banked_and_unworked_rows():
    cur = _FakeCursor(
        [_cand("banked", "qualified", _STRIP),
         _cand("sushi", "promoted", _SUSHI, lead="L1", city="Denver")],
        [_lead("L1")])
    assert _rescore_map_penalties_once(cur) == (2, 1)
    assert cur.cands["banked"]["score"] == 6    # tourist strip, -4
    assert cur.cands["sushi"]["score"] == 8     # Asian cuisine, -2
    assert cur.leads["L1"]["lead_score"] == 8


def test_rescore_never_touches_a_worked_lead():
    cur = _FakeCursor(
        [_cand("called", "promoted", _STRIP, lead="L1"),
         _cand("dead", "promoted", _STRIP, lead="L2")],
        [_lead("L1", touched="2026-09-22T20:00:00+00:00"), _lead("L2", status="dead")])
    assert _rescore_map_penalties_once(cur) == (0, 0)
    assert cur.leads["L1"]["lead_score"] == 10
    assert cur.leads["L2"]["lead_score"] == 10


def test_rescore_skips_rows_already_scored_with_the_penalty():
    cur = _FakeCursor([_cand("fresh", "qualified", _STRIP, enriched=_NEW)], [])
    assert _rescore_map_penalties_once(cur) == (0, 0)
    assert cur.cands["fresh"]["score"] == 10


def test_rescore_runs_only_once():
    cur = _FakeCursor([_cand("banked", "qualified", _STRIP)], [])
    assert _rescore_map_penalties_once(cur) == (1, 0)
    assert _rescore_map_penalties_once(cur) is None   # the next boot
    assert cur.cands["banked"]["score"] == 6          # charged once, not twice


def test_rescore_uses_the_same_numbers_as_score_candidate():
    tags = {"amenity": "bar", "cuisine": "sushi", "addr:street": "Las Vegas Blvd S"}
    plain = {"amenity": "bar"}
    assert (score_candidate(tags, None, "", city="Las Vegas")
            - score_candidate(plain, None, "", city="Las Vegas")
            == _map_fit_penalty(tags, "Las Vegas") == -6)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
