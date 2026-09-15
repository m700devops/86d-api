"""Facts about a venue, for the thirty seconds before the phone rings.

The rule these tests exist to hold: every line must be attributable. A brief
that asserts something wrong is the moment the person on the other end decides
they're being read a script, and that costs more than having no brief at all.
"""

import pytest

from venue import extract_facts, facts_to_lines, loads, weekly_hours

SITE = """<p>Serving Nashville since 1974. 40 beers on tap, seats 220.
          Private events welcome. Live music nightly.</p>"""
TAGS = {
    "amenity": "bar", "cuisine": "american;burger",
    "addr:housenumber": "402", "addr:street": "12th Avenue South",
    "opening_hours": "Mo-Su 11:00-26:00", "min_age": "21",
    "outdoor_seating": "yes", "building:levels": "2", "brewery": "yes",
}


def text_of(lines):
    return [l["text"] for l in lines]


def source_for(lines, needle):
    return next((l["source"] for l in lines if needle in l["text"]), None)


def test_reads_the_map_and_the_venues_own_words():
    lines = facts_to_lines(extract_facts(TAGS, SITE))
    joined = " | ".join(text_of(lines))
    assert "american, burger brewpub" in joined     # brewery tag beats plain "bar"
    assert "open since 1974" in joined
    assert "40 taps" in joined
    assert "seats about 220" in joined
    assert "21 and over" in joined
    assert "402 12th Avenue South" in joined


def test_every_line_says_where_it_came_from():
    lines = facts_to_lines(extract_facts(TAGS, SITE))
    assert lines and all(l["source"] for l in lines)
    # The specific failure this guards: an early version hardcoded "their
    # website" next to the year, then backfilled 307 leads from map tags
    # alone — every one crediting a website nobody had read.
    assert source_for(lines, "1974") == "their website"
    assert source_for(lines, "21 and over") == "OpenStreetMap"


def test_a_year_from_the_map_is_phrased_as_a_question_not_a_claim():
    # OSM's start_date is often when the entry was surveyed, not when the bar
    # opened. Said out loud as fact it's the kind of wrong that ends a call.
    lines = facts_to_lines(extract_facts({"amenity": "bar", "start_date": "2025"}, ""))
    assert "map lists it opening 2025" in text_of(lines)
    assert "open since 2025" not in text_of(lines)


def test_hours_are_a_volume_signal():
    # Seven days and a 2am licence is several times the pour volume of a
    # Thursday-to-Sunday room, and pours are what there is to count.
    busy = weekly_hours("Mo-Su 11:00-26:00")
    assert busy["days_open"] == 7 and busy["hours_per_week"] == 105
    quiet = weekly_hours("Th-Su 17:00-23:00")
    assert quiet["days_open"] == 4 and quiet["hours_per_week"] == 24
    assert weekly_hours(None) is None
    assert weekly_hours("whenever we feel like it") is None


def test_a_late_licence_is_reported():
    line = " | ".join(text_of(facts_to_lines(extract_facts(
        {"amenity": "bar", "opening_hours": "Mo-Su 11:00-26:00"}, ""))))
    assert "last call around 2am" in line


def test_nothing_is_invented_from_nothing():
    assert facts_to_lines(extract_facts({}, "")) == []
    assert facts_to_lines(extract_facts(None, None or "")) == []
    assert facts_to_lines({}) == []


def test_a_bare_map_entry_still_gives_something_usable():
    # The common case: no website text at all. Address and hours alone are
    # worth having in front of you.
    lines = facts_to_lines(extract_facts(
        {"amenity": "pub", "addr:housenumber": "12", "addr:street": "Main St",
         "opening_hours": "We-Su 16:00-24:00"}, ""))
    joined = " | ".join(text_of(lines))
    assert "pub" in joined and "5 days a week" in joined and "12 Main St" in joined
    assert all(l["source"] == "OpenStreetMap" for l in lines)


@pytest.mark.parametrize("html,expected", [
    ("<p>Est. 1987 in the heart of downtown.</p>", "open since 1987"),
    ("<p>We opened in 2011.</p>", "open since 2011"),
    ("<p>Serving Portland since 1998</p>", "open since 1998"),
])
def test_ways_a_venue_says_how_old_it_is(html, expected):
    assert expected in text_of(facts_to_lines(extract_facts({"amenity": "bar"}, html)))


def test_a_script_tag_is_not_the_venue_talking():
    # Same class of bug as the email crawler: anything inside <script> was
    # written by a developer, not by the bar.
    html = '<script>var founded = "since 1901";</script><p>A nice pub.</p>'
    assert not any("1901" in t for t in text_of(facts_to_lines(
        extract_facts({"amenity": "pub"}, html))))


def test_bad_json_never_crashes_a_page():
    assert loads(None) == {} and loads("") == {} and loads("{not json") == {}
