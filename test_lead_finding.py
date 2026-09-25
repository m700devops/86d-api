"""Finding bars: every place the pipeline used to lose a good one.

Measured on 90 real Austin venues (2026-09-24) before these fixes: 22 callable
leads. After: 35, with 18 more queued to retry instead of lost, plus venues
with a website and no phone tag (~160 in Austin) now harvested, a quarter of
which the venue's own site supplies a number for. A fake web (`_http`
patched) stands in for the network; the cases are the real ones found.
"""
import json
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import leadgen  # noqa: E402

PAGE = "<html><body><a href='/contact'>Contact</a> Call us (512) 476-0182 · hi@bar.example</body></html>"


def _web(monkeypatch, pages: dict):
    """pages: url -> (body, status), or a list of them for successive calls."""
    calls = []

    def fake(url, timeout=20, data=None, verify_public=False, insecure=False, tls_status=False):
        calls.append((url, data is not None, insecure))
        v = pages.get((url, insecure)) or pages.get(url) or ("", 404)
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        body, status = v
        if status == -1 and not tls_status:
            status = 0
        return body, status

    monkeypatch.setattr(leadgen, "_http", fake)
    return calls


def _cand(**kw):
    c = {"name": "Barton Tavern", "website": "https://bar.example/", "phone": "5124760182",
         "amenity": "bar", "raw_tags": "{}", "city": "Austin", "opening_hours": None,
         "local_codes": {"512"}}
    c.update(kw)
    return c


# ── harvest: what the map says ──────────────────────────────────────────────

def test_a_field_with_two_numbers_yields_the_first_good_one():
    assert leadgen.first_phone("+1 512-476-0182; +1 512-476-0199") == "5124760182"
    assert leadgen.first_phone("1-800-476-0100 or (512) 476-0182") == "5124760182"  # toll-free skipped
    assert leadgen.first_phone("call us!") is None and leadgen.first_phone(None) is None


def test_a_field_with_two_websites_yields_the_first():
    assert leadgen.first_website("www.bar.example;https://other.example") == "http://www.bar.example"
    assert leadgen.first_website("https://bar.example/austin") == "https://bar.example/austin"
    assert leadgen.first_website("") is None and leadgen.first_website("n/a") is None


def test_the_site_supplies_the_number_when_the_map_has_none():
    v = leadgen.judge_phone(None, ["5124760182"], {"512"})
    assert v["status"] == "from_site" and v["phone"] == "5124760182"
    assert "None" not in v["note"]
    assert leadgen.judge_phone(None, [], {"512"})["status"] == "unconfirmed"
    assert "None" not in leadgen.judge_phone(None, ["5124760182", "5124760199"], {"512"})["note"]


def test_overpass_falls_back_to_get_then_the_next_mirror(monkeypatch):
    ok = json.dumps({"elements": [{"type": "node", "id": 1}]})
    m1, m2 = leadgen.OVERPASS_MIRRORS[:2]
    calls = _web(monkeypatch, {m1: ("", 504), m2: ("<html>busy</html>", 200)})
    monkeypatch.setattr(leadgen.time, "sleep", lambda s: None)
    # First mirror: POST 504, GET (url with ?data=) also fails; second: POST
    # junk, GET works.
    get2 = f"{m2}?data={leadgen.urllib.parse.quote('Q')}"
    calls.clear()

    def fake(url, timeout=20, data=None, **kw):
        calls.append((url, data is not None))
        if url == get2:
            return ok, 200
        return ("", 504) if url.startswith(m1) else ("<html>busy</html>", 200)

    monkeypatch.setattr(leadgen, "_http", fake)
    assert leadgen._overpass("Q")["elements"]
    assert [c[1] for c in calls] == [True, False, True, False]   # POST, GET, POST, GET


# ── fetching a venue's site ─────────────────────────────────────────────────

def test_a_202_with_the_page_in_it_is_a_page(monkeypatch):
    _web(monkeypatch, {"https://bar.example/": (PAGE, 202)})
    home, url, status = leadgen._fetch_site("https://bar.example/")
    assert home == PAGE and status == 202


def test_a_stale_deep_link_falls_back_to_the_home_page(monkeypatch):
    _web(monkeypatch, {"https://ironcactus.example/austin-downtown": ("", 404),
                       "https://ironcactus.example/": (PAGE, 200)})
    home, url, _ = leadgen._fetch_site("https://ironcactus.example/austin-downtown")
    assert home == PAGE and url == "https://ironcactus.example/"


def test_a_certificate_problem_tries_http_then_reads_without_the_check(monkeypatch):
    _web(monkeypatch, {"https://thai.example/": ("", -1), "http://thai.example/": ("", 0),
                       ("https://thai.example/", True): (PAGE, 200)})
    home, url, _ = leadgen._fetch_site("https://thai.example/")
    assert home == PAGE and url == "https://thai.example/"


def test_a_site_down_for_now_is_retried_not_rejected(monkeypatch):
    for status in (0, 403, 429, 503):
        _web(monkeypatch, {"https://bar.example/": ("", status)})
        out = leadgen.enrich_candidate(_cand())
        assert out["status"] == "retry", status
    _web(monkeypatch, {"https://bar.example/": ("", 404)})
    assert leadgen.enrich_candidate(_cand())["status"] == "rejected"   # the root itself is gone


def test_retries_are_spaced_and_end_in_a_rejection(monkeypatch):
    writes = []

    class Cur:
        def execute(self, sql, params=()):
            writes.append((" ".join(sql.split()), params))

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(leadgen, "get_db", db)
    leadgen._record_retry({"id": "C1", "enrich_attempts": 0}, "site unreachable (HTTP 503)")
    sql, p = writes[-1]
    assert "status = 'retry'" in sql and p[1] == 1 and p[2] > leadgen.now_iso()
    leadgen._record_retry({"id": "C1", "enrich_attempts": leadgen.ENRICH_TRIES - 1}, "site unreachable (HTTP 503)")
    sql, p = writes[-1]
    assert "status = 'rejected'" in sql and p[0].endswith(f"{leadgen.ENRICH_TRIES} tries")


# ── qualifying ──────────────────────────────────────────────────────────────

def test_no_email_is_a_call_only_lead_not_a_rejection(monkeypatch):
    _web(monkeypatch, {"https://bar.example/": ("<html>Barton Tavern — whiskey bar · Call (512) 476-0182</html>", 200)})
    out = leadgen.enrich_candidate(_cand())
    assert out["status"] == "qualified" and out["email"] is None
    assert out["phone_status"] == "confirmed"
    with_email = leadgen.score_candidate({}, "dave@bar.example", "", None, "Austin")
    assert leadgen.score_candidate({}, None, "", None, "Austin") < with_email   # email still sorts first


def test_a_restaurant_whose_drinks_are_on_its_menu_page_qualifies(monkeypatch):
    home = ("<html><a href='/drink-menu'>Drinks</a><a href='/menu.pdf'>Menu</a> "
            "Tandoori classics · (512) 476-0182</html>")
    _web(monkeypatch, {"https://tandoori.example/": (home, 200),
                       "https://tandoori.example/drink-menu": ("<p>Whisky flights, mango margarita</p>", 200)})
    out = leadgen.enrich_candidate(_cand(name="Tandoori Lounge", website="https://tandoori.example/",
                                         amenity="restaurant"))
    assert out["status"] == "qualified"


def test_a_restaurant_with_no_drinks_anywhere_is_still_rejected(monkeypatch):
    _web(monkeypatch, {"https://burger.example/": ("<a href='/menu'>Menu</a> burgers (512) 476-0182", 200),
                       "https://burger.example/menu": ("<p>burgers, fries, shakes</p>", 200)})
    out = leadgen.enrich_candidate(_cand(name="Burger Barn", website="https://burger.example/",
                                         amenity="restaurant"))
    assert out["status"] == "rejected" and out["reject_reason"].startswith("no sign on their own site")


def test_drink_tags_on_the_map_count_only_for_spirits():
    # Beer and wine tags say nothing about a back bar; a spirits or cocktails
    # tag is one piece of evidence — enough for a bar, not for a restaurant.
    for tag in ({"drink:beer": "yes"}, {"drink:wine": "served"}, {"alcohol": "yes"}):
        assert not leadgen._restaurant_pours("", tag)[0], tag
    assert leadgen.liquor_verdict("", {"cocktails": "yes"}, "", "bar")["status"] == "spirits"
    assert leadgen.liquor_verdict("", {"cocktails": "yes"}, "", "restaurant")["status"] == "unknown"


def test_chain_names_match_whole_words_and_brand_possessives():
    assert leadgen.looks_like_chain("Casino El Camino") is None
    assert leadgen.looks_like_chain("Chili Pepper Grill") is None
    assert leadgen.looks_like_chain("Craft Pride Tap House") is None
    assert leadgen.looks_like_chain("Hops & Grain Brewhouse") is None
    assert leadgen.looks_like_chain("Chili's Grill & Bar")
    assert leadgen.looks_like_chain("Applebee's")
    assert leadgen.looks_like_chain("Buffalo Wild Wings")


def test_only_store_locator_language_makes_a_chain():
    for independent in ("all locations open at 10:30AM for the game",
                        "See all of our locations", "enjoyed by brewers nationwide"):
        assert leadgen.looks_like_chain("X", "", independent) is None, independent
    for chain in ("Find a Location", "Franchise opportunities", "Corporate Office",
                  "Store locator"):
        assert leadgen.looks_like_chain("X", "", chain), chain


# ── promoting ───────────────────────────────────────────────────────────────

def test_a_call_only_lead_is_promoted_and_an_unsourced_email_dropped():
    inserted = {}

    class Cur:
        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            if s.startswith("INSERT INTO crm_leads"):
                inserted["email"], inserted["notes"] = params[4], params[5]
            if s.startswith("SELECT id FROM users"):
                raise AssertionError("no email means no customer lookup by email")
            self._rows = []

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    cand = {"id": "C1", "name": "Barton Tavern", "city": "Austin", "state": "TX",
            "phone": "5124760182", "website": "https://bar.example/", "amenity": "bar",
            "score": 9, "email": "bank123@test.com", "email_source": None,
            "phone_status": "confirmed", "raw_tags": "{}"}
    assert leadgen._promote_one(Cur(), cand, "2026-09-25T00:00:00+00:00")
    assert inserted["email"] is None
    assert "call-only lead" in inserted["notes"]


def test_rejections_the_fixes_overturn_are_reopened_once():
    import re
    reopen = re.compile(leadgen.REQUALIFY_REASONS)
    for reason in ("site unreachable (http 0)", "no email found on site",
                   "restaurant with no sign of a bar programme", "franchise language on site",
                   "duplicate email", "chain name (casino)", "chain name (tap house)"):
        assert reopen.search(reason), reason
    for reason in ("lead deleted by hand", "already in pipeline", "already a customer",
                   "chain name (applebee's)".replace("applebee's", "olive garden"),
                   "suppressed email"):
        assert not reopen.search(reason), reason
