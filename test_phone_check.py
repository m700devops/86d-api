"""Is the number on the call list really the bar's?

The phone came off OpenStreetMap and nothing checked it against the bar. On
102 real Denver bars (2026-09-24), where the bar's own website listed a
number, the map's disagreed about one time in five — one map entry carried a
Chicago area code. leadgen now reads the numbers a venue's own site publishes
and only promotes a number the site vouches for. These tests use page snippets
shaped like the real sites that were checked. `database` is stubbed the way
test_leadgen.py stubs it.
"""
import json
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402
import leadgen  # noqa: E402
from leadgen import judge_phone, local_area_codes, site_phones  # noqa: E402

DENVER = {"303", "720"}


# ── reading a site ──────────────────────────────────────────────────────────

def test_every_way_a_site_publishes_its_number_is_read_best_first():
    html = ('<a href="tel:+13038930552">Call</a>'
            '<script type="application/ld+json">{"@type":"BarOrPub","telephone":"(720) 805-2626"}</script>'
            '<span itemprop="telephone">303.645.3753</span>'
            '<footer>Private events: 720-547-2533</footer>')
    assert site_phones(html) == ["3038930552", "7208052626", "3036453753", "7205472533"]


def test_toll_free_reserved_and_script_noise_are_not_the_bar():
    html = ("<p>Gift cards 1-800-555-0100 · Reservations 1 (888) 555-0199</p>"
            "<p>303-353-2918 or 997-427-9989</p>"
            "<script>var tracking = '3035550123';</script>")
    assert site_phones(html) == ["3033532918"]


def test_the_same_number_written_three_ways_counts_once():
    html = '<a href="tel:3032782337">303-278-2337</a> (303) 278 2337'
    assert site_phones(html) == ["3032782337"]


# ── judging it ──────────────────────────────────────────────────────────────

def test_the_maps_number_on_their_site_is_confirmed():
    v = judge_phone("3035983035", ["3035983035", "7204845341"], DENVER)
    assert v["status"] == "confirmed" and v["phone"] == "3035983035"


def test_their_site_corrects_a_stale_map_number():
    # Devon's Pub: map 303-756-5507, their site 303-893-0552.
    v = judge_phone("3037565507", ["3038930552"], DENVER)
    assert v == {"phone": "3038930552", "status": "from_site",
                 "note": "Map listed 303-756-5507; their website lists 303-893-0552"}


def test_an_out_of_town_map_number_is_replaced_by_the_local_one():
    # Glow Lounge: the map carried a Chicago 773 number for a Denver bar.
    v = judge_phone("7736124424", ["3038322687"], DENVER)
    assert v["status"] == "from_site" and v["phone"] == "3038322687"


def test_another_locations_line_is_not_taken():
    # A multi-location site listing only its Nashville number.
    v = judge_phone("3037796444", ["6157151093"], DENVER)
    assert v["status"] == "conflict" and v["phone"] == "3037796444"


def test_several_local_numbers_and_none_of_them_the_maps_is_not_a_guess():
    # A brewery group's site listing four taprooms' lines.
    v = judge_phone("7204770813", ["7208052626", "3039555788", "3036453753"], DENVER)
    assert v["status"] == "conflict"


def test_a_site_with_no_number_leaves_it_unconfirmed():
    assert judge_phone("3035551234", [], DENVER)["status"] == "unconfirmed"


def test_local_area_codes_come_from_the_metros_own_bars():
    phones = ["303" + "5551234"] * 60 + ["720" + "5551234"] * 35 + ["773" + "5551234"]
    assert local_area_codes(phones) == {"303", "720"}
    assert local_area_codes(["3035551234"] * 5) == set()     # too few to tell


# ── enrichment ──────────────────────────────────────────────────────────────

def _site(monkeypatch, pages):
    monkeypatch.setattr(leadgen, "_http",
                        lambda url, **kw: (pages[url], 200) if url in pages else ("", 404))


def _cand(**kw):
    c = {"id": "C1", "name": "Devon's Pub", "website": "https://devonspub.example",
         "phone": "3037565507", "amenity": "bar", "raw_tags": json.dumps({"amenity": "bar"}),
         "opening_hours": None, "city": "Denver", "local_codes": DENVER}
    c.update(kw)
    return c


def test_enrichment_takes_the_number_from_their_contact_page(monkeypatch):
    _site(monkeypatch, {
        "https://devonspub.example": '<a href="mailto:owner@devonspub.example">Email</a>'
                                     '<a href="/contact">Contact</a><p>Whiskey and cold beer</p>',
        "https://devonspub.example/contact": "<p>Call us: 303-893-0552</p>",
    })
    out = leadgen.enrich_candidate(_cand())
    assert out["status"] == "qualified"
    assert (out["phone"], out["phone_status"]) == ("3038930552", "from_site")


def test_a_site_with_no_number_still_banks_the_lead_but_marks_it(monkeypatch):
    _site(monkeypatch, {"https://devonspub.example":
                        '<a href="mailto:owner@devonspub.example">Email</a> Full bar'})
    out = leadgen.enrich_candidate(_cand())
    assert out["status"] == "qualified" and out["phone_status"] == "unconfirmed"


# ── re-checking the call list ───────────────────────────────────────────────

class _Cursor:
    def __init__(self, delete_hits=1):
        self.seen, self.rowcount, self.delete_hits = [], 0, delete_hits

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.seen.append((s, list(params)))
        self.rowcount = self.delete_hits if s.startswith("DELETE") else 1


def test_a_corrected_number_replaces_the_maps_and_says_so():
    cur, tally = _Cursor(), leadgen.Counter()
    leadgen._apply_lead_verdict(cur, {"lead_id": "L1", "id": "C1"},
                                judge_phone("3037565507", ["3038930552"], DENVER),
                                "2026-09-24T20:00:00+00:00", tally)
    sql, params = cur.seen[0]
    assert sql.startswith("UPDATE crm_leads SET phone = %s")
    assert "last_touch_at IS NULL" in sql           # never a lead someone just rang
    assert params[0] == "3038930552"
    assert "Phone corrected from their website" in params[4]


def test_a_number_their_site_cant_vouch_for_leaves_the_call_list():
    cur, tally = _Cursor(), leadgen.Counter()
    leadgen._apply_lead_verdict(cur, {"lead_id": "L1", "id": "C1"},
                                judge_phone("3035551234", [], DENVER),
                                "2026-09-24T20:00:00+00:00", tally)
    assert cur.seen[0][0].startswith("DELETE FROM crm_leads")
    assert "queued_email_at IS NULL" in cur.seen[0][0]
    assert cur.seen[1][0].startswith("UPDATE crm_lead_candidates SET status = 'qualified'")
    assert tally["off_call_list"] == 1


def test_one_with_an_email_queued_stays_but_is_marked():
    cur, tally = _Cursor(delete_hits=0), leadgen.Counter()
    leadgen._apply_lead_verdict(cur, {"lead_id": "L1", "id": "C1"},
                                judge_phone("3035551234", [], DENVER),
                                "2026-09-24T20:00:00+00:00", tally)
    assert cur.seen[1][0].startswith("UPDATE crm_leads SET phone_status = %s")
    assert not tally["off_call_list"]


# ── the Wrong number button ─────────────────────────────────────────────────

class _WrongCursor:
    def __init__(self, lead):
        self.lead, self.seen, self._last = lead, [], None

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.seen.append((s, list(params)))
        if s.startswith("SELECT l.*"):
            self._last = {**self.lead, "cand_website": "https://bar.example", "cand_city": "Denver"}
        elif s.startswith("SELECT * FROM crm_leads"):
            self._last = dict(self.lead)
        elif s.startswith("SELECT city, phone"):
            self._rows = []
        elif s.startswith("UPDATE crm_leads"):
            self._last = {**self.lead, "phone": params[0], "phone_status": params[1],
                          "followup_date": params[3]}
        else:
            self._last = None

    def fetchone(self):
        return self._last

    def fetchall(self):
        return getattr(self, "_rows", [])


def _wrong(monkeypatch, numbers):
    lead = {k: None for k in crm.LEAD_COLUMNS}
    lead.update(id="L1", name="Mystic Lounge", phone="3035700206", status="new",
                attempts=0, source="leadgen")
    cur = _WrongCursor(lead)

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cur, commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_today", lambda: "2026-09-24")
    monkeypatch.setattr(crm, "_load_counters_locked", lambda cursor: {})
    monkeypatch.setattr(leadgen, "find_site_phones", lambda site, phone=None, budget=3: (numbers, True))
    return cur, crm.wrong_number("L1", True)


def test_wrong_number_takes_the_one_their_website_lists(monkeypatch):
    cur, out = _wrong(monkeypatch, ["3034680598"])
    assert out["new_phone"] == "303-468-0598" and out["bad_phone"] == "303-570-0206"
    assert out["lead"]["phone_status"] == "from_site"
    assert out["lead"]["followup_date"] == "2026-09-24"      # back in Follow-ups today
    touch = next(p for s, p in cur.seen if s.startswith("INSERT INTO crm_touches"))
    assert "wrong_number" in touch                          # the dial is logged
    suppress = next(p for s, p in cur.seen if s.startswith("INSERT INTO crm_suppressions"))
    assert suppress[1] == "3035700206"                       # and never dialled again


def test_wrong_number_with_nothing_on_their_site_clears_the_number(monkeypatch):
    _cur, out = _wrong(monkeypatch, ["3035700206"])          # only the bad one
    assert out["new_phone"] is None
    assert out["lead"]["phone"] is None and out["lead"]["phone_status"] == "wrong"


def test_undoing_a_wrong_number_puts_the_number_back_in_play(monkeypatch):
    seen = []

    class Cur:
        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            seen.append((s, list(params)))
            self._last = ({"id": "U1", "lead_id": "L1", "action": "wrong-number call",
                           "snapshot": json.dumps({"phone": "3035700206"}),
                           "counters_spent": 1, "restored_at": None, "touch_id": "T1"}
                          if s.startswith("SELECT * FROM crm_lead_undo") else {"id": "L1"})

        def fetchone(self):
            return self._last

    cur = Cur()

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: cur, commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_lead_row", lambda row: row)
    monkeypatch.setattr(crm, "_load_counters_locked", lambda cursor: {})
    crm.undo_touch("U1", True)
    assert ("DELETE FROM crm_suppressions WHERE kind = 'phone' AND value = %s",
            ["3035700206"]) in seen


# ── never freezing the server ───────────────────────────────────────────────
# PR #35 took production down: Python's re holds the GIL, and a pattern that
# backtracks on one odd page stalls every request the process is serving. A
# page reaches site_phones at up to 800KB (curl's cap), so each hostile shape
# below is built that big and must still parse in well under a second.

import time  # noqa: E402

_BIG = 800_000


def _fast(html, limit=1.0):
    start = time.perf_counter()
    site_phones(html)
    return time.perf_counter() - start < limit


def test_a_telephone_tag_that_never_closes_is_fast():
    assert _fast('<span itemprop="telephone" ' + "x" * _BIG)
    assert _fast('<span itemprop="telephone" content="' + "x" * _BIG)
    assert _fast(('<span itemprop="telephone" ' + "a=b " * 50) * (_BIG // 220))


# Every other reader and shape: test_hostile_pages.py.


def test_a_huge_page_is_read_top_and_bottom_only():
    filler = "x" * (leadgen.SITE_HTML_HEAD + leadgen.SITE_HTML_TAIL)
    html = ("<header>303-893-0552</header>" + filler + "<p>720-547-2533</p>"
            + filler + "<footer>720-805-2626</footer>")
    assert site_phones(html) == ["3038930552", "7208052626"]


def test_itemprop_content_attribute_is_still_read():
    html = '<meta itemprop="telephone" content="+1 303 645 3753"><span>x</span>'
    assert site_phones(html) == ["3036453753"]


# ── the background re-check stays small ─────────────────────────────────────

def test_a_batch_stops_starting_sites_at_its_deadline(monkeypatch):
    rows = [{"lead_id": f"L{i}", "id": f"C{i}", "city": "Denver",
             "website": f"https://bar{i}.example", "phone": "303-893-0552"}
            for i in range(10)]
    crawled, writes = [], []

    class Cur:
        rowcount = 1

        def execute(self, sql, params=None):
            self.sql = sql
            writes.append(sql) if sql.lstrip().startswith(("UPDATE", "DELETE")) else None

        def fetchall(self):
            return rows if "FROM crm_leads l" in self.sql else []

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

    @contextmanager
    def db():
        yield Conn()

    def slow_site(website, map_phone=None, budget=3):
        crawled.append(website)
        time.sleep(0.2)
        return ["3038930552"], True

    monkeypatch.setattr(leadgen, "get_db", db)
    monkeypatch.setattr(leadgen, "find_site_phones", slow_site)
    monkeypatch.setattr(leadgen, "_local_codes_by_city", lambda c, cities: {})
    monkeypatch.setattr(leadgen, "VERIFY_WORKERS", 2)
    out = leadgen.verify_phones(lead_limit=10, bank_limit=0, budget_s=0.3)
    # Two at a time: the first pair, maybe a second pair started before the
    # deadline, never all ten.
    assert 2 <= len(crawled) <= 4
    assert out["leads_checked"] == len(crawled)


def test_nothing_crawls_at_boot():
    import inspect
    assert "Thread(" not in inspect.getsource(leadgen.init_leadgen_tables)
