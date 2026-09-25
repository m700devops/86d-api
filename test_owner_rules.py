"""The owner's rules for the call list (2026-09-25), in order:
1. no chains or corporate venues, 2. very confident it pours liquor,
3. no main-strip tourist bars, 4. an email if they have one.

1-3 are exclusions. Honky Tonk Central (329 Broadway, Nashville — one of four
Broadway bars under one owner) and Sweedeedee (a beer-and-wine brunch café in
Portland) topped the list because each of these used to be a few points of
score, and a personal-looking email outranked all of them. The liquor rule
itself is test_leadgen.py; the call list's order and filter test_callnow.py.
Here: tourist strips, chain signals, and applying all of it to leads and
candidates that were qualified under the old rules — with a fake cursor, and
the crawl stubbed, like test_phone_check.py.
"""
import json
import sys
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import leadgen  # noqa: E402
from leadgen import corporate_reason, tourist_strip  # noqa: E402


# ── 3. tourist strips ─────────────────────────────────────────────────────

def test_honky_tonk_central_is_on_lower_broadway():
    tags = {"addr:street": "Broadway", "addr:housenumber": "329"}      # from OSM, 2026-09-25
    assert tourist_strip(tags, "Nashville") == "Lower Broadway"


def test_a_venue_with_no_address_is_placed_by_the_map():
    # Barstool (Lower Broadway) and a casino-floor bar on the Strip carry no
    # street at all; where they sit decides.
    assert tourist_strip({}, "Nashville", 36.16094, -86.77485) == "Lower Broadway"
    assert tourist_strip({}, "Las Vegas", 36.10996, -115.17578) == "the Las Vegas Strip"   # Marquee
    assert tourist_strip({}, "Las Vegas", 36.16893, -115.14067) == "Fremont Street"
    assert tourist_strip({}, "Nashville", 36.1745, -86.7550) is None      # East Nashville


def test_only_the_strip_part_of_a_street_counts():
    # Austin's Dirty Sixth, not East Austin; the Strip, not downtown or North LV.
    assert tourist_strip({"addr:street": "East 6th Street", "addr:housenumber": "501"},
                         "Austin") == "Dirty Sixth"
    assert tourist_strip({"addr:street": "East 6th Street", "addr:housenumber": "1816"},
                         "Austin") is None
    assert tourist_strip({"addr:street": "West 6th Street", "addr:housenumber": "501"},
                         "Austin") is None
    assert tourist_strip({"addr:street": "South Las Vegas Boulevard",
                          "addr:housenumber": "3655"}, "Las Vegas") == "the Las Vegas Strip"
    assert tourist_strip({"addr:street": "South Las Vegas Boulevard",
                          "addr:housenumber": "1616"}, "Las Vegas") is None      # Arts District
    assert tourist_strip({"addr:street": "North Las Vegas Boulevard",
                          "addr:housenumber": "2500"}, "Las Vegas") is None
    assert tourist_strip({"addr:street": "5th Avenue", "addr:housenumber": "3845"},
                         "San Diego") is None                                    # Hillcrest
    assert tourist_strip({"addr:street": "5th Avenue", "addr:housenumber": "600"},
                         "San Diego") == "the Gaslamp Quarter"


def test_a_strip_street_elsewhere_is_just_a_street():
    assert tourist_strip({"addr:street": "Broadway", "addr:housenumber": "329"}, "Denver") is None
    assert tourist_strip({"addr:street": "Beale Street"}, "Memphis") == "Beale Street"
    assert tourist_strip({"addr:street": "Bourbon Street"}, "New Orleans") == "Bourbon Street"


# ── 1. chains and corporate venues ──────────────────────────────────────────

INDEX = {"brands": {"mcmenamins"}, "domains": {"flyingsaucer.com"}}


def test_a_brand_on_several_venues_is_a_chain_one_on_a_single_bar_is_not():
    assert corporate_reason({"brand": "McMenamins"}, "https://mcmenamins.com/x", INDEX)
    # Greater Trumps: operator=McMenamins, no brand tag of its own.
    assert corporate_reason({"operator": "McMenamins"}, None, INDEX)
    # Aalto Lounge: a single independent a mapper tagged with its own name.
    assert corporate_reason({"brand": "Aalto Lounge"}, None, INDEX) is None
    # An owner named as operator is an independent.
    assert corporate_reason({"operator": "Lucy De Leon"}, None, INDEX) is None


def test_a_known_chain_name_in_the_operator_is_corporate():
    assert corporate_reason({"operator": "Hilton"}, None, {})             # Hop City Tavern


def test_one_website_across_cities_is_a_chain():
    assert corporate_reason({}, "https://www.flyingsaucer.com/nashville", INDEX)
    assert corporate_reason({}, "https://devonspub.example", INDEX) is None


def test_shared_hosts_are_no_ones_domain():
    for host in ("facebook.com", "order.toasttab.com", "linktr.ee", "joes.square.site"):
        assert leadgen.SHARED_HOSTS.search(host), host
    assert not leadgen.SHARED_HOSTS.search("flyingsaucer.com")


class _IndexCursor:
    def __init__(self, brands, domains):
        self.answers = [[{"v": b} for b in brands], [{"d": d} for d in domains]]

    def execute(self, sql, params=()):
        self._rows = self.answers.pop(0)

    def fetchall(self):
        return self._rows


def test_the_index_drops_shared_hosts():
    idx = leadgen.corporate_index(_IndexCursor(["mcmenamins"],
                                               ["flyingsaucer.com", "facebook.com"]))
    assert idx == {"brands": {"mcmenamins"}, "domains": {"flyingsaucer.com"}}


# ── enrichment rejects before crawling ──────────────────────────────────────

def test_enrichment_rejects_a_strip_bar_without_a_single_request(monkeypatch):
    monkeypatch.setattr(leadgen, "_http", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("crawled a tourist-strip bar")))
    tags = {"amenity": "bar", "addr:street": "Broadway", "addr:housenumber": "329"}
    out = leadgen.enrich_candidate({"id": "C", "name": "Honky Tonk Central",
                                    "website": "https://www.honkytonkcentral.com/",
                                    "raw_tags": json.dumps(tags), "city": "Nashville",
                                    "phone": "6157429095", "amenity": "bar"})
    assert out["status"] == "rejected" and out["reject_reason"] == "tourist strip (Lower Broadway)"


def test_enrichment_rejects_sweedeedee(monkeypatch):
    # Squarespace's country picker in a script, a brunch menu, no liquor.
    home = ('<script>{"name":"Martinique"}</script><p>Sweedeedee — pie, coffee, brunch. '
            '(503) 946-8087 <a href="mailto:sweedeedee@gmail.com">email</a></p>')
    monkeypatch.setattr(leadgen, "_http", lambda url, **k: (home, 200) if url.rstrip("/")
                        == "https://www.sweedeedee.com" else ("", 404))
    out = leadgen.enrich_candidate({"id": "C", "name": "Sweedeedee",
                                    "website": "https://www.sweedeedee.com/",
                                    "raw_tags": json.dumps({"amenity": "restaurant"}),
                                    "city": "Portland", "phone": "5039468087",
                                    "amenity": "restaurant", "local_codes": {"503"}})
    assert out["status"] == "rejected"
    assert out["reject_reason"] == "no sign on their own site that they pour liquor"


def test_a_qualified_lead_carries_its_evidence(monkeypatch):
    home = '<p>Devon\'s Pub — full bar, whiskey list. 303-756-5507</p>'
    monkeypatch.setattr(leadgen, "_http", lambda url, **k: (home, 200) if url.rstrip("/")
                        == "https://devonspub.example" else ("", 404))
    out = leadgen.enrich_candidate({"id": "C", "name": "Devon's Pub",
                                    "website": "https://devonspub.example",
                                    "raw_tags": json.dumps({"amenity": "pub"}),
                                    "city": "Denver", "phone": "3037565507", "amenity": "pub",
                                    "local_codes": {"303"}})
    assert out["status"] == "qualified" and out["fit_status"] == "ok"
    assert out["fit_note"].startswith("Pours liquor — their site: “full bar”")


# ── applying the rules to what's already listed and banked ──────────────────

class _Cursor:
    """crm_leads + crm_lead_candidates, just the statements the rules run."""

    def __init__(self, cands, leads):
        self.cands = {c["id"]: c for c in cands}
        self.leads = {l["id"]: l for l in leads}
        self._rows, self.rowcount = [], 0

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self._rows, self.rowcount = [], 0
        if "substring(raw_tags" in s or "substring(website" in s:
            return                                          # corporate_index: empty
        if s.startswith("SELECT c.id, c.city"):             # _reconcile_owner_rules
            for c in self.cands.values():
                lead = self.leads.get(c.get("promoted_lead_id"))
                listed = (c["status"] == "promoted" and lead and lead["source"] == "leadgen"
                          and lead["status"] == "new" and not lead["last_touch_at"])
                if c["status"] == "qualified" or listed:
                    self._rows.append({**c, "lead_id": lead and lead["id"],
                                       "queued_email_at": lead and lead.get("queued_email_at")})
        elif s.startswith("DELETE FROM crm_leads"):
            lead = self.leads.get(params[0])
            if (lead and lead["status"] == "new" and not lead["last_touch_at"]
                    and not lead.get("queued_email_at")
                    and ("fit_status IS NULL" not in s or lead.get("fit_status") is None)):
                del self.leads[params[0]]
                self.rowcount = 1
        elif s.startswith("UPDATE crm_leads SET fit_status"):
            lead = self.leads.get(params[-1])
            if lead and ("fit_status IS NULL" not in s or lead.get("fit_status") is None) \
                    and ("last_touch_at IS NULL" not in s or not lead["last_touch_at"]):
                lead["fit_status"] = "ok" if "'ok'" in s else "blocked"
                lead["fit_note"] = params[0]
        elif s.startswith("UPDATE crm_lead_candidates"):
            c = self.cands[params[-1]]
            if "status = 'rejected'" in s:
                c.update(status="rejected", reject_reason=params[0], fit_status="blocked",
                         promoted_lead_id=None)
            elif "status = 'retry'" in s:
                c.update(status="retry", reject_reason=params[0], promoted_lead_id=None)
            else:
                c.update(fit_status="ok", fit_note=params[0])
        else:
            raise AssertionError(f"unexpected SQL: {s[:90]}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _c(id, tags, status="promoted", lead=None, city="Nashville", lat=None, lon=None):
    return {"id": id, "city": city, "lat": lat, "lon": lon, "website": f"https://{id}.example",
            "raw_tags": json.dumps(tags), "status": status, "promoted_lead_id": lead,
            "name": id, "amenity": "bar"}


def _l(id, touched=None, queued=None, source="leadgen"):
    return {"id": id, "status": "new", "last_touch_at": touched, "queued_email_at": queued,
            "source": source, "fit_status": None}


HTC = {"addr:street": "Broadway", "addr:housenumber": "329"}


def test_boot_takes_strip_bars_off_the_list_and_out_of_the_bank():
    cur = _Cursor([_c("htc", HTC, lead="L1"), _c("tootsies", HTC, status="qualified"),
                   _c("dive", {"addr:street": "Gallatin Ave"}, lead="L2")],
                  [_l("L1"), _l("L2")])
    assert leadgen._reconcile_owner_rules(cur) == (1, 1)
    assert "L1" not in cur.leads and "L2" in cur.leads
    assert cur.cands["htc"]["reject_reason"] == "tourist strip (Lower Broadway)"
    assert cur.cands["tootsies"]["status"] == "rejected"


def test_boot_never_touches_a_worked_lead_and_hides_one_with_mail_queued():
    cur = _Cursor([_c("called", HTC, lead="L1"), _c("queued", HTC, lead="L2")],
                  [_l("L1", touched="2026-09-24T20:00:00Z"), _l("L2", queued="2026-09-26")])
    assert leadgen._reconcile_owner_rules(cur) == (0, 0)
    assert cur.leads["L1"]["fit_status"] is None           # someone rang it: theirs now
    assert cur.leads["L2"]["fit_status"] == "blocked"      # kept, never offered


def test_the_background_check_applies_each_outcome():
    cur = _Cursor([_c("ok", {}, lead="L1"), _c("beer", {}, lead="L2"),
                   _c("down", {}, lead="L3"), _c("rung", {}, lead="L4")],
                  [_l("L1"), _l("L2"), _l("L3"), _l("L4", touched="2026-09-25T01:00:00Z")])
    at = "2026-09-26T00:00:00+00:00"
    leadgen._apply_fit_to_lead(cur, {"lead_id": "L1", "id": "ok"},
                               {"status": "ok", "note": "Pours liquor — their site: “full bar”"}, at)
    leadgen._apply_fit_to_lead(cur, {"lead_id": "L2", "id": "beer"},
                               {"status": "blocked", "note": "beer and wine only"}, at)
    leadgen._apply_fit_to_lead(cur, {"lead_id": "L3", "id": "down"},
                               {"status": "unreachable", "note": "site unreachable (HTTP 0)"}, at)
    leadgen._apply_fit_to_lead(cur, {"lead_id": "L4", "id": "rung"},
                               {"status": "blocked", "note": "beer and wine only"}, at)
    assert cur.leads["L1"]["fit_status"] == "ok" and cur.cands["ok"]["fit_status"] == "ok"
    assert "L2" not in cur.leads and cur.cands["beer"]["status"] == "rejected"
    assert "L3" not in cur.leads and cur.cands["down"]["status"] == "retry"   # re-crawled later
    assert cur.leads["L4"]["fit_status"] is None and cur.cands["rung"]["status"] == "promoted"


def test_check_fit_reads_the_drinks_page_when_the_homepage_says_nothing(monkeypatch):
    pages = {"https://cantina.example": '<a href="/drinks">Drinks</a> Tacos and more',
             "https://cantina.example/drinks": "<p>Mezcal flights · Paloma · Tequila</p>"}
    monkeypatch.setattr(leadgen, "_http", lambda url, **k: (pages[url.rstrip("/")], 200)
                        if url.rstrip("/") in pages else ("", 404))
    out = leadgen.check_fit({"name": "Cantina", "website": "https://cantina.example",
                             "raw_tags": json.dumps({"amenity": "restaurant"}),
                             "amenity": "restaurant", "city": "Austin"})
    assert out["status"] == "ok" and "mezcal flights" in out["note"]
