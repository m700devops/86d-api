"""Every case here is a number that would have dialled a stranger.

Kept as a file rather than a one-off check because the cost is asymmetric: a
rejected good lead is one lost call, a bad number is a real person's phone
ringing during their afternoon. The strings come from real OpenStreetMap
`phone` and `contact:phone` tags unless marked otherwise.
"""

import pytest

from phones import format_us_phone, is_toll_free, normalize_us_phone


@pytest.mark.parametrize("raw,expected", [
    # Shapes OSM actually contains, all the same number.
    ("+1-615-742-9095", "6157429095"),
    ("(615) 742-9095", "6157429095"),
    ("615.742.9095", "6157429095"),
    ("1 615 742 9095", "6157429095"),
    ("6157429095", "6157429095"),
    ("+1 (615) 742-9095", "6157429095"),
    # Stray whitespace inside the number — common when a volunteer pastes it.
    ("+1-773- 394-9898", "7733949898"),
    ("  212-555-1234  ", "2125551234"),
])
def test_accepts_real_numbers(raw, expected):
    assert normalize_us_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    None, "", "   ",
    # Not enough digits, or too many.
    "742-9095", "615-742", "615-742-90951",
    # International. +44 and +33 numbers turn up on US venue pages whose owner
    # also runs somewhere abroad; dialling them is both wrong and expensive.
    "+44 20 7946 0958", "+33 1 42 68 53 00", "+52 55 1234 5678",
    # 11 digits not starting with 1 is not a NANP number.
    "20123456789",
    # Vanity. Translating letters to keys is guesswork.
    "1-800-FLOWERS", "615-EAT-HERE", "call us at 615-742-9095",
    # N11 service codes as area code or exchange — 911 is the one that matters.
    "911-742-9095", "411-742-9095", "615-411-9095", "615-911-9095",
    # Premium rate.
    "900-742-9095", "976-742-9095",
    # Reserved fictional block.
    "615-555-0142",
    # Area code or exchange starting 0 or 1.
    "015-742-9095", "115-742-9095", "615-042-9095", "615-142-9095",
    # Placeholder junk.
    "1111111111", "0000000000", "123-456-7890",
])
def test_rejects_undialable(raw):
    assert normalize_us_phone(raw) is None


def test_extension_is_not_part_of_the_number():
    # The extension digits glued on would make a 13-digit number, and stripping
    # blindly to 10 would dial a different line entirely.
    assert normalize_us_phone("615-742-9095 ext. 204") == "6157429095"
    assert normalize_us_phone("615-742-9095 x204") == "6157429095"
    assert normalize_us_phone("615-742-9095 #204") == "6157429095"


def test_two_numbers_in_one_field_takes_the_first():
    # Concatenating these gives 20 digits; truncating gives a number that is
    # neither venue's.
    assert normalize_us_phone("615-742-9095; 615-742-9096") == "6157429095"
    assert normalize_us_phone("615-742-9095 / 615-742-9096") == "6157429095"
    assert normalize_us_phone("615-742-9095 or 615-742-9096") == "6157429095"


def test_toll_free_is_valid_but_flagged():
    # Dialable, so not None — but on a single independent bar it's almost
    # always a booking platform or a franchise line, which is the opposite of
    # what this list is for. The caller decides.
    digits = normalize_us_phone("1-800-742-9095")
    assert digits == "8007429095"
    assert is_toll_free(digits)
    assert not is_toll_free(normalize_us_phone("615-742-9095"))


def test_formatting_is_for_reading_not_for_dialling():
    assert format_us_phone("6157429095") == "(615) 742-9095"
    assert format_us_phone("615742909") == ""
    assert format_us_phone(None) == ""


def test_non_strings_do_not_raise():
    for junk in (12345, [], {}, object()):
        assert normalize_us_phone(junk) is None
