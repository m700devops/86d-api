"""No page and no email may freeze the server.

Python's re holds the GIL while it matches, so one pattern that backtracks on
one odd input stops every request the process is serving — the product API
included, not just the CRM. PR #35 took production down that way. Everything
here reads text strangers control: venue websites (anyone can edit the map
that points at them) and the inbox (anyone can send mail). Each hostile shape
is built at 800KB, curl's per-page cap, and every reader must get through it
in well under a second. A regex that fails this is quadratic somewhere.
"""
import sys
import time
import types

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import contacts  # noqa: E402
import inbox  # noqa: E402
import leadgen  # noqa: E402
import venue  # noqa: E402

N = 800_000
SHAPES = {
    "letters": "a" * N, "dotted": "a." * (N // 2), "ats": "a@" * (N // 2),
    "open angles": "<" * N, "open hrefs": '<a href="' * (N // 9),
    "tag never closes": '<a href="x"' + " y" * (N // 2), "words": "a " * (N // 2),
    "digits": "1" * N, "spaces": " " * N, "newlines": "\n" * N,
    "open comments": "<!--" * (N // 4), "open scripts": "<script>" * (N // 8),
    "telephone tag never closes": '<span itemprop="telephone" ' + "x" * N,
    "names": "Manager John Smith, " * (N // 20), "tel hrefs": 'href="tel:' * (N // 10),
    "json phones": '"telephone":"' * (N // 13), "caps": "Ab " * (N // 3),
    "at words": "info at " * (N // 8), "dot words": "x dot " * (N // 6),
    "quotes": "\n> " * (N // 3), "dashes": "-" * N, "on": "On " * (N // 3),
}


def _mail(kind, body):
    return ("From: a@b.com\r\nMessage-ID: <x@y>\r\nContent-Type: text/" + kind
            + "\r\n\r\n" + body).encode()


READERS = {
    "extract_emails": leadgen.extract_emails,
    "site_phones": leadgen.site_phones,
    "contact links": lambda h: leadgen._contact_urls("https://x.com", h),
    "find_manager": lambda h: contacts.find_manager(h, "https://x.com/about"),
    "restaurant gate": lambda h: leadgen._restaurant_pours(h, {}),
    "opener": lambda h: leadgen.opener_line(h, "bar"),
    "stack signals": leadgen._stack_signals,
    "venue facts": lambda h: venue.extract_facts({}, h),
    "chain check": lambda h: leadgen.looks_like_chain("Olde Town", "https://x.com", h),
    "reply text": inbox.new_text,
    "opt-out check": inbox.looks_like_opt_out,
    "html email": lambda h: inbox.parse(_mail("html", h)),
    "plain email": lambda h: inbox.parse(_mail("plain", h)),
}


@pytest.mark.parametrize("reader", sorted(READERS))
def test_no_reader_grinds_on_hostile_input(reader):
    slow = []
    for shape, text in SHAPES.items():
        start = time.perf_counter()
        READERS[reader](text)
        took = time.perf_counter() - start
        if took > 1.0:
            slow.append(f"{shape}: {took:.1f}s")
    assert not slow, slow


def test_stripping_scripts_and_comments_still_works():
    html = ('a<script type="x">var e="a@b.com"</script >b<!-- c@d.com -->'
            'c<STYLE>x</style>d<script>never closed')
    assert contacts.strip_non_content(html) == "a b c d"
    assert contacts.strip_non_content("a<scripts>b") == "a<scripts>b"


def test_the_bounded_email_patterns_still_find_real_addresses():
    html = ('<a href="mailto:dave@divebar.com">x</a> write info [at] olde-town [dot] com '
            'or bob.smith@gmail.com.')
    assert sorted(leadgen.extract_emails(html)) == [
        "bob.smith@gmail.com", "dave@divebar.com", "info@olde-town.com"]
