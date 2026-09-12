"""Call windows and service bands, from real OpenStreetMap opening_hours.

No database, no network, no dependence on what time it is when the suite runs
— every case passes an explicit `local_now`.
"""

from datetime import datetime

import pytest

from callwindow import (all_buckets, bucket_of, call_window, is_open_today,
                        opens_at, parse_opening_hours, service_of)

MON = datetime(2026, 9, 14, 15, 0)     # a Monday, 3pm local
MON_10AM = datetime(2026, 9, 14, 10, 0)
TUE = datetime(2026, 9, 15, 15, 0)


def test_parses_the_common_shapes():
    assert parse_opening_hours("Mo-Fr 16:00-02:00")[0] == [(16 * 60, 26 * 60)]
    assert parse_opening_hours("11:00-23:00")[3] == [(11 * 60, 23 * 60)]
    assert parse_opening_hours("24/7")[5] == [(0, 24 * 60)]
    # A range that wraps the week.
    assert set(parse_opening_hours("Sa-Mo 12:00-20:00")) == {5, 6, 0}


def test_a_comma_can_separate_rules_as_well_as_time_spans():
    """From a real harvested venue. The OSM spec separates rules with ";" and
    uses "," to join time spans inside one rule — but contributors use commas
    for both, and splitting on ";" alone read this whole string as a single
    Monday-to-Thursday rule carrying four spans. The venue then had NO hours
    for Friday, Saturday or Sunday, which for a bar means it was treated as
    shut on its three best nights and dropped off the call list all weekend."""
    schedule = parse_opening_hours(
        "Mo-Th 11:00-24:00, Fr 11:00-26:00, Sa 10:00-26:00, Su 10:00-24:00")
    assert schedule[0] == [(11 * 60, 24 * 60)]          # Monday
    assert schedule[4] == [(11 * 60, 26 * 60)]          # Friday, closing at 2am
    assert schedule[5] == [(10 * 60, 26 * 60)]          # Saturday
    assert schedule[6] == [(10 * 60, 24 * 60)]          # Sunday
    assert is_open_today(schedule, 5) is True


def test_a_comma_between_two_shifts_still_joins_them():
    # The other meaning of a comma, which must keep working: one rule, two
    # spans, a lunch service and a dinner service on the same days.
    schedule = parse_opening_hours("Mo-Fr 09:00-12:00,13:00-17:00")
    assert schedule[0] == [(9 * 60, 12 * 60), (13 * 60, 17 * 60)]
    assert schedule.get(5) is None


def test_unparseable_and_closed_are_different_answers():
    # None means "we don't know, use the generic window"; {} means the venue
    # said it is shut, which is a reason to drop the lead. Collapsing the two
    # would either drop half the list or call permanently closed buildings.
    assert parse_opening_hours("sunrise to sunset, ish") is None
    assert parse_opening_hours(None) is None
    assert parse_opening_hours("closed") == {}


def test_a_span_starting_at_midnight_is_last_nights_tail():
    # "Mo 00:00-02:00" is Sunday night spilling over, not a bar that opens at
    # midnight — treating it as the opening time would file it under lunch.
    schedule = parse_opening_hours("Mo 00:00-02:00,17:00-23:00")
    assert opens_at(schedule, 0) == 17 * 60


def test_shut_today_names_the_next_open_day():
    # We-Su means Monday and Tuesday are shut.
    w = call_window("We-Su 11:00-23:00", MON)
    assert w["state"] == "shut_today"
    assert not w["good_now"]
    assert "We" in w["headline"]
    assert is_open_today(parse_opening_hours("We-Su 11:00-23:00"), 0) is False


def test_lunch_venue_is_called_in_the_afternoon_lull():
    w = call_window("Mo-Su 11:00-23:00", MON)
    assert w["good_now"] and w["state"] == "good"
    assert w["window"] == "2:00pm-4:00pm"
    assert "lull" in w["headline"]


def test_lunch_venue_is_also_callable_right_after_it_unlocks():
    # The reason the lunch tab exists is to be worked during lunch hours. With
    # only the 2-4:30 window, every row in it read "too early" from 11am to
    # 2pm — three hours where the doors are open, the manager is on the floor
    # and nobody has ordered yet, reported as unreachable.
    w = call_window("Mo-Su 11:00-23:00", datetime(2026, 9, 14, 11, 10))
    assert w["good_now"] and "setting up" in w["headline"]
    # Starts half an hour BEFORE the doors open: staff are in, taking
    # deliveries, not yet serving anyone.
    assert w["windows"] == ["10:30am-11:45am", "2:00pm-4:00pm"]
    assert call_window("Mo-Su 11:00-23:00", datetime(2026, 9, 14, 10, 40))["good_now"]


def test_the_lunch_rush_itself_is_not_called():
    # Half past noon is the one part of the middle of the day that really is
    # a bad time — and it says so, rather than "too early", which at 12:30pm
    # reads like a bug.
    w = call_window("Mo-Su 11:00-23:00", datetime(2026, 9, 14, 12, 30))
    assert not w["good_now"]
    assert "In the rush" in w["headline"] and "2:00pm" in w["headline"]


def test_before_opening_is_still_too_early():
    early = call_window("Mo-Su 11:00-23:00", MON_10AM)
    assert not early["good_now"] and early["state"] == "early"
    assert "Too early" in early["headline"]


def test_after_the_last_window_lists_both_missed_ones():
    w = call_window("Mo-Su 11:00-23:00", datetime(2026, 9, 14, 17, 0))
    assert w["state"] == "late" and not w["good_now"]
    assert "10:30am-11:45am" in w["headline"] and "2:00pm-4:00pm" in w["headline"]


def test_a_dinner_venue_gets_one_window_not_two():
    # The pre-rush trick is a lunch-service thing. A nightclub opening at nine
    # has no earlier moment to catch.
    w = call_window("We-Sa 21:00-02:00", datetime(2026, 9, 16, 21, 30))
    assert w["windows"] == ["8:30pm-11:00pm"]


def test_late_opening_venue_is_called_just_after_it_opens():
    # A nightclub that unlocks at nine. Ringing it at 3pm reaches an empty
    # building — this is the case a fixed 2-5pm window got wrong every time.
    w = call_window("We-Sa 21:00-02:00", datetime(2026, 9, 16, 15, 0))
    assert not w["good_now"]
    assert w["window"] == "8:30pm-11:00pm"
    assert call_window("We-Sa 21:00-02:00", datetime(2026, 9, 16, 21, 30))["good_now"]
    # And half an hour before the doors, while they're setting up.
    assert call_window("We-Sa 21:00-02:00", datetime(2026, 9, 16, 20, 40))["good_now"]


def test_unknown_hours_fall_back_to_the_generic_window():
    w = call_window(None, MON)
    assert w["good_now"] and w["known"] is False
    assert w["window"] == "2:00pm-4:00pm"


def test_permanently_closed_is_never_callable():
    w = call_window("closed", MON)
    assert w["state"] == "permanently_closed" and not w["good_now"]


@pytest.mark.parametrize("hours,band", [
    ("Mo-Su 11:00-23:00", "lunch"),
    ("Mo-Fr 11:30-22:00", "lunch"),       # exactly on the cutoff
    ("Mo-Su 10:00-02:00", "lunch"),
    ("Mo-Fr 16:00-02:00", "dinner"),
    ("We-Sa 21:00-02:00", "dinner"),
    ("Mo-Su 12:00-23:00", "dinner"),      # noon is past the cutoff
    (None, "dinner"),                     # unknown is filed with dinner
    ("something unparseable", "dinner"),
])
def test_service_band_splits_the_two_tabs(hours, band):
    assert service_of(hours) == band


def test_a_venue_open_early_one_day_a_week_counts_as_lunch():
    # Brunch on Sunday only. Calling it before noon on a Sunday works, and the
    # generic window still covers the rest of the week.
    assert service_of("Mo-Sa 17:00-02:00; Su 10:00-22:00") == "lunch"


def test_buckets_cover_every_tab_the_page_can_show():
    assert len(all_buckets()) == 8
    assert bucket_of(-5, "Mo-Su 11:00-23:00") == ("lunch", -5)
    assert bucket_of(-8, "Mo-Fr 16:00-02:00") == ("dinner", -8)
    # No longitude means no zone sub-tab to file it under.
    assert bucket_of(None, "Mo-Su 11:00-23:00") == ("lunch", None)
    # A zone outside the lower 48 isn't one of the four tabs.
    assert bucket_of(-10, None) == ("dinner", None)
