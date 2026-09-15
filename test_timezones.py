"""Which timezone tab a city lands in.

This stopped being cosmetic when the zones became tabs. The whole workflow is
"work west as the rush rolls", so a bar filed an hour wrong is called during
its dinner service — the exact mistake the tabs exist to prevent.
"""

import pytest

from leadgen import us_tz_offset

EASTERN, CENTRAL, MOUNTAIN, PACIFIC = -5, -6, -7, -8


@pytest.mark.parametrize("city,lat,lon,state,zone", [
    # The three a longitude-only rule got wrong. All sit just west of -97.5
    # and all are solidly Central.
    ("Austin", 30.27, -97.74, "TX", CENTRAL),
    ("San Antonio", 29.42, -98.49, "TX", CENTRAL),
    ("Oklahoma City", 35.47, -97.51, "OK", CENTRAL),
    # ...and the sliver of Texas that really is Mountain.
    ("El Paso", 31.76, -106.49, "TX", MOUNTAIN),

    # States the line genuinely crosses.
    ("Nashville", 36.16, -86.78, "TN", CENTRAL),
    ("Knoxville", 35.96, -83.92, "TN", EASTERN),
    ("Miami", 25.76, -80.19, "FL", EASTERN),
    ("Pensacola", 30.42, -87.22, "FL", CENTRAL),
    ("Detroit", 42.33, -83.05, "MI", EASTERN),
    ("Louisville", 38.25, -85.76, "KY", EASTERN),
    ("Bowling Green", 36.99, -86.44, "KY", CENTRAL),
    ("Chattanooga", 35.05, -85.31, "TN", EASTERN),
    ("Indianapolis", 39.77, -86.16, "IN", EASTERN),
    ("Evansville", 37.97, -87.55, "IN", CENTRAL),
    ("Marquette", 46.55, -87.40, "MI", EASTERN),
    ("Iron Mountain", 45.82, -88.06, "MI", CENTRAL),
    ("Tallahassee", 30.44, -84.28, "FL", EASTERN),
    ("Bismarck", 46.81, -100.78, "ND", CENTRAL),
    ("Dickinson", 46.88, -102.79, "ND", MOUNTAIN),
    ("Dodge City", 37.75, -100.02, "KS", CENTRAL),
    ("Goodland", 39.35, -101.71, "KS", MOUNTAIN),
    ("Scottsbluff", 41.87, -103.67, "NE", MOUNTAIN),
    ("Rapid City", 44.08, -103.23, "SD", MOUNTAIN),
    ("Ontario", 44.03, -116.96, "OR", MOUNTAIN),
    ("West Wendover", 40.74, -114.07, "NV", MOUNTAIN),
    ("Las Vegas", 36.17, -115.14, "NV", PACIFIC),
    ("Portland", 45.52, -122.68, "OR", PACIFIC),

    # Idaho splits north-south, not east-west: Boise is Mountain despite
    # sitting further west than Las Vegas is from its own state's line.
    ("Boise", 43.62, -116.20, "ID", MOUNTAIN),
    ("Coeur d'Alene", 47.68, -116.78, "ID", PACIFIC),

    # Unambiguous ones, as a floor.
    ("New York", 40.71, -74.01, "NY", EASTERN),
    ("Chicago", 41.88, -87.63, "IL", CENTRAL),
    ("Denver", 39.74, -104.99, "CO", MOUNTAIN),
    ("Phoenix", 33.45, -112.07, "AZ", MOUNTAIN),
    ("Seattle", 47.61, -122.33, "WA", PACIFIC),
    ("New Orleans", 29.95, -90.07, "LA", CENTRAL),
])
def test_city_lands_in_the_right_zone(city, lat, lon, state, zone):
    assert us_tz_offset(lon, state, lat) == zone


def test_falls_back_to_longitude_without_a_state():
    # Every seeded city carries a state; this is the path for anything added
    # later without one. Crude, but never None when a longitude exists.
    assert us_tz_offset(-74.0) == EASTERN
    assert us_tz_offset(-87.6) == CENTRAL
    assert us_tz_offset(-104.9) == MOUNTAIN
    assert us_tz_offset(-122.3) == PACIFIC


def test_no_location_at_all_is_none_not_a_guess():
    assert us_tz_offset(None) is None
    assert us_tz_offset(None, "XX") is None
