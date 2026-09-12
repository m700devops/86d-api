"""Manager names and email quality — the two things that reorder the call list.

The manager tests err hard towards silence. Every false positive here is a
caller asking a bar for somebody who was never there, which is worse than
having no name at all; a false negative costs nothing, because the call works
without a name.
"""

import pytest

from contacts import email_kind, find_manager, find_managers

TEAM_PAGE = """
<h2>Our Team</h2>
<div><h3>Justin Lynch</h3><p>Owner, Operator</p></div>
<div><h3>Brad Smithberger</h3><p>General Manager</p></div>
<div><h3>Tylson Boyd</h3><p>Chef</p></div>
<div><h3>Andrew Burdette</h3><p>General Manager</p></div>
"""


def test_reads_a_real_team_page():
    # Taken from a live harvested site. The owner outranks both GMs even
    # though a GM is not printed last, and the chef is not a contact for a
    # bar-inventory pitch.
    people = find_managers(TEAM_PAGE, "https://example.com/about-us")
    assert [p["name"] for p in people] == [
        "Justin Lynch", "Brad Smithberger", "Andrew Burdette"]
    assert find_manager(TEAM_PAGE)["name"] == "Justin Lynch"
    assert find_manager(TEAM_PAGE)["role"] == "Owner"


def test_two_people_with_the_same_title_are_both_reported():
    # One of the two GMs above has probably moved on. Saying so beats picking
    # one at random and sounding certain about it.
    page = "<p>Brad Smithberger</p><p>General Manager</p><p>Andrew Burdette</p><p>General Manager</p>"
    assert find_manager(page)["also"] == ["Andrew Burdette"]


@pytest.mark.parametrize("html,name,role", [
    ("<h3>Dave Smith</h3><p>General Manager</p>", "Dave Smith", "General Manager"),
    ("<p>General Manager: Dave Smith</p>", "Dave Smith", "General Manager"),
    ("<li>Maria O'Brien, Owner</li>", "Maria O'Brien", "Owner"),
    ("<p>Sarah Chen &mdash; Beverage Director</p>", "Sarah Chen", "Beverage Director"),
    ("<p>Tim O&#39;Neil, GM</p>", "Tim O'Neil", "General Manager"),
    ("<div>Our bar manager Jean-Luc Fontaine keeps the list tight.</div>",
     "Jean-Luc Fontaine", "Bar Manager"),
    ("<td>Tom Ford</td><td>Managing Partner</td>", "Tom Ford", "Managing Partner"),
])
def test_shapes_that_really_appear(html, name, role):
    got = find_manager(html, "https://example.com/about")
    assert got and got["name"] == name and got["role"] == role
    assert got["source"] == "https://example.com/about"


@pytest.mark.parametrize("html", [
    "",
    "<h2>MEET THE TEAM</h2><p>Contact Us</p><p>Our Menu</p>",
    "<p>Chef Antonio Ruiz</p>",                  # kitchen, not bar
    "<p>Please contact our management team</p>",  # no name at all
    "<p>General Manager</p><p>Hiring now</p>",    # the role, vacant
    "<p>Monday Manager</p>",                      # a weekday is not a person
    "<p>Private Events Manager</p>",              # a department, not a person
    "<script>var manager = 'Dave Smith';</script>",  # not visible text
])
def test_says_nothing_rather_than_guessing(html):
    assert find_manager(html) is None


def test_a_role_word_must_sit_next_to_the_name():
    # A capitalised name elsewhere on the page is a band, a supplier, a street
    # or a cocktail. Without an adjacent title it is never a contact.
    page = "<p>Live music from Johnny Delaware this Friday.</p><p>Our manager is in.</p>"
    assert find_manager(page) is None


# ── Email quality ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("email,kind", [
    # The ones worth calling first: one human's mailbox.
    ("dave@divebar.com", "personal"),
    ("dave.smith@divebar.com", "personal"),
    ("d.smith@divebar.com", "personal"),
    ("jsmith@divebar.com", "personal"),
    ("mike2@divebar.com", "personal"),
    ("maria_lopez@divebar.com", "personal"),
    # Reaches a decision-maker, just not by name.
    ("owner@divebar.com", "owner"),
    ("gm@divebar.com", "owner"),
    ("barmanager@divebar.com", "owner"),
    # A shared inbox somebody may or may not read.
    ("info@divebar.com", "role"),
    ("contact@divebar.com", "role"),
    ("hello@divebar.com", "role"),
    ("bookings@divebar.com", "role"),
    ("eventsandcatering@divebar.com", "role"),
    ("noreply@divebar.com", "role"),
    # Nothing to go on.
    ("a1b2c3@divebar.com", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
    ("not-an-email", "unknown"),
])
def test_email_kind(email, kind):
    assert email_kind(email) == kind


# ── Junk addresses ──────────────────────────────────────────────────────────
#
# Every one of these came out of a real harvest and reached the call list.
# They matter more than they look: a template placeholder reads as a personal
# mailbox, so it sorted to the TOP of the list and reached nobody at all.

from contacts import EMAIL_BLOCKLIST  # noqa: E402


@pytest.mark.parametrize("email", [
    "your@email.com",                                        # stock template text
    "mymail@mailservice.com",                                # ditto
    "bank1160e5fa-cbd0-483a-b2d7-3d9b8d5a8839@test.com",     # a script's throwaway
    "a3f9c1d2e4b5a6c7d8e9f0a1b2c3d4e5@barname.com",          # a token, not a mailbox
    "yourname@yourdomain.com",
    "noreply@barname.com",
    "wixofday@wix.com",                                      # reaches Wix, not the bar
])
def test_junk_addresses_are_blocked(email):
    assert EMAIL_BLOCKLIST.search(email)


@pytest.mark.parametrize("email", [
    "info@realbar.com",
    "dave@divebar.com",
    "monique@popsforchampagne.com",
    "oshaughnessyspub@gmail.com",        # free mailboxes are normal for a small bar
    "bukowskitavern@yahoo.com",
    "jjjohnsonllc2014@gmail.com",        # digits in a name are not a machine id
])
def test_real_addresses_survive(email):
    assert not EMAIL_BLOCKLIST.search(email)



# ── What a crawler may read an address off ──────────────────────────────────

def test_addresses_in_script_and_style_are_not_contacts():
    """A live bug this caught: a jQuery validation message on a real bar's
    homepage reads "Please use the format email@example.com", and the crawler
    was pulling addresses straight out of raw HTML — script bodies included.
    Anything in there was written by a developer or a library, never by the
    venue, and is exactly where machine-shaped addresses come from."""
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql://localhost/unused")
    from leadgen import extract_emails

    html = """
      <a href="mailto:real@bar.com">Email us</a>
      <script>
        var msg = "use the format email@example.com";
        track("a1b2c3d4-0000-1111-2222-333344445555@segment.io");
      </script>
      <style>/* theme@builder.com */</style>
      <!-- old contact: designer@agency.com -->
      <p>Bookings: events@bar.com</p>
    """
    got = extract_emails(html)
    assert set(got) == {"real@bar.com", "events@bar.com"}


def test_a_mailto_link_still_counts_even_though_it_is_markup():
    import os
    os.environ.setdefault("DATABASE_URL", "postgresql://localhost/unused")
    from leadgen import extract_emails
    # An address a human deliberately published, and often the only one on the
    # page — losing it to the script filter would cost real leads.
    assert extract_emails('<a href="mailto:gm@thebar.com">contact</a>') == ["gm@thebar.com"]
