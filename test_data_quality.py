"""Audit of 2026-09-25: every place a wrong email, phone, website or name
could reach a lead. The cases are real — found by running the pipeline over
180 real Portland and Nashville venues and reading what it chose.
"""
import json
import sys
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import contacts  # noqa: E402
import crm  # noqa: E402
import inbox  # noqa: E402
import leadgen  # noqa: E402


# ── 1. someone else's address on the venue's site ───────────────────────────

REAL_FOREIGN = [   # (venue, its site, the address the old code took)
    ("Martin's Bar-B-Que Joint", "https://www.martinsbbqjoint.com/", "hi@dianabartonpr.com"),
    ("Suzy Wong's House of Yum", "https://suzywongsnashville.com/", "templates@wavesdesign.io"),
    ("White Owl Social Club", "http://www.whiteowlsocialclub.com/", "events@toothandnailpdx.com"),
    ("Bottle + Kitchen", "https://www.bottlekitchen.com/", "hrpmgr@staypineapple.com"),
]


def test_another_business_address_never_fits():
    for name, site, email in REAL_FOREIGN:
        assert not contacts.email_fits_venue(email, site, name), email


def test_own_domain_free_mail_and_name_domains_fit():
    assert contacts.email_fits_venue("info@limelightloungepdx.com",
                                     "https://limelightloungepdx.com/", "Limelight")
    assert contacts.email_fits_venue("events@mail.ringosbar.com", "https://ringosbar.com/", "Ringo's")
    assert contacts.email_fits_venue("sandovalspdx@gmail.com", "https://sandovalspdx.com", "Sandoval's")
    assert contacts.email_fits_venue("dave@cox.net", "https://joes.example", "Joe's")
    assert contacts.email_fits_venue("molly.carter@five-srg.com", "https://www.fivespicerestaurant.com/",
                                     "Five Spice Seafood")      # its restaurant group


def test_pick_email_skips_a_foreign_address_for_the_venues_own():
    emails = ["hi@dianabartonpr.com", "paul@musichighway.net", "info@martinsbbqjoint.com"]
    assert leadgen.pick_email(emails, "https://www.martinsbbqjoint.com/",
                              "Martin's Bar-B-Que Joint") == "info@martinsbbqjoint.com"
    assert leadgen.pick_email(["hi@dianabartonpr.com"], "https://www.martinsbbqjoint.com/",
                              "Martin's Bar-B-Que Joint") is None   # call-only, never the PR firm


def _site(monkeypatch, pages):
    monkeypatch.setattr(leadgen, "_http", lambda url, **kw: (pages[url], 200)
                        if url in pages else ("", 404))


def test_enrichment_takes_no_email_rather_than_the_web_designers(monkeypatch):
    _site(monkeypatch, {"https://suzy.example": "<h1>Suzy Wong's House of Yum</h1> craft cocktails, "
                        "vodka · 615-555-0142 · <a href='mailto:templates@wavesdesign.io'>x</a>"})
    out = leadgen.enrich_candidate({"id": "C", "name": "Suzy Wong's House of Yum",
                                    "website": "https://suzy.example", "phone": "6155550142",
                                    "raw_tags": json.dumps({"amenity": "bar"}), "amenity": "bar",
                                    "city": "Nashville", "local_codes": {"615"}})
    assert out["status"] == "qualified" and out["email"] is None


# ── 2. a website that isn't theirs ─────────────────────────────────────────

def test_a_site_that_never_names_the_venue_is_not_theirs():
    # The Ranch (Nashville) is tagged on the map with Jackalope Brewing's site.
    assert not leadgen.site_mentions_venue("<h1>Jackalope Brewing Co.</h1>",
                                           "https://jackalopebrew.com/", "The Ranch")
    assert leadgen.site_mentions_venue("<h1>Welcome</h1>", "https://ringosbar.com/", "Ringo's Bar")
    assert leadgen.site_mentions_venue("<p>Ringo's — since 1998</p>", "https://x.example", "Ringo's")


def test_enrichment_rejects_someone_elses_site(monkeypatch):
    _site(monkeypatch, {"https://jackalopebrew.com/": "<h1>Jackalope Brewing</h1> beer and cocktails"})
    out = leadgen.enrich_candidate({"id": "C", "name": "The Ranch", "website": "https://jackalopebrew.com/",
                                    "phone": "6155550100", "raw_tags": "{}", "amenity": "bar",
                                    "city": "Nashville"})
    assert out["status"] == "rejected" and "doesn't mention The Ranch" in out["reject_reason"]


def test_the_prep_sheet_and_wrong_number_skip_a_site_marked_not_theirs():
    notes = ("Website: https://africangrilllakewood.com/\n[2026-09-26] Website "
             "https://africangrilllakewood.com/ is not theirs — the automatic lookup …\n"
             "later: Website: https://nemoose.example")
    assert crm._notes_website(notes) == "https://nemoose.example"
    assert crm._notes_website("Website: https://africangrilllakewood.com/ · picked a different "
                              "venue (https://africangrilllakewood.com/)") is None


# ── 3. the AI reading call notes ───────────────────────────────────────────

def _notes(extracted, text):
    from test_quick_add import _FakeCursor, _lead
    cur = _FakeCursor(_lead(email="owner@murphys.example", phone="615-555-0100"))
    lead = _lead(email="owner@murphys.example", phone="615-555-0100")
    updated, applied, _, _ = crm._apply_call_notes(cur, lead, extracted, text, "call",
                                                   "2026-09-25", "2026-09-25T12:00:00Z")
    return updated, applied


def test_an_email_or_phone_the_notes_dont_contain_is_not_saved():
    updated, applied = _notes({"email": "dave@murphyspub.com", "phone": "615-555-0199",
                               "outcome": "gatekeeper"},
                              "Murphy's — spoke to Sarah, owner Dave is in Thursday")
    assert updated["email"] == "owner@murphys.example"      # the good one stays
    assert updated["phone"] == "615-555-0100"
    assert len(applied["not_saved"]) == 2


def test_an_email_the_notes_do_contain_is_saved():
    updated, _ = _notes({"email": "dave@murphyspub.com", "outcome": "callback"},
                        "Dave said email him at dave@murphyspub.com, call Thursday")
    assert updated["email"] == "dave@murphyspub.com"


def test_verified_and_site_found_emails_still_land():
    updated, _ = _notes({"email": "a@x.example", "_verified": True, "outcome": "callback"}, "part")
    assert updated["email"] == "a@x.example"
    updated, _ = _notes({"email": "b@y.example", "_email_from_site": "b@y.example",
                         "outcome": "gatekeeper"}, "no email in these notes")
    assert updated["email"] == "b@y.example"


# ── 4. inbox: one person's reply is not every provider customer's ──────────

def test_an_internet_provider_domain_never_ties_a_reply_to_other_bars():
    for dom in ("cox.net", "charter.net", "bellsouth.net", "nc.rr.com",
                "privaterelay.appleid.com", "yahoo.co.uk"):
        assert contacts.free_mail(dom), dom
    leads = [{"id": "A", "email": "joe@cox.net"}, {"id": "B", "email": "sam@cox.net"},
             {"id": "C", "email": "gm@hospitalitygroup.com"}, {"id": "D", "email": "x@hospitalitygroup.com"}]
    assert inbox.match_leads({"from_addr": "joe@cox.net"}, leads, {}) == ["A"]
    # A real company domain still reaches all its venues.
    assert inbox.match_leads({"from_addr": "gm@hospitalitygroup.com"}, leads, {}) == ["C", "D"]


# ── 5. queued mail after things changed ─────────────────────────────────────

def test_queued_mail_is_held_when_the_lead_changed():
    to = "brent@bar.example"
    assert crm._queued_mail_hold({"status": "contacted", "email": to}, to, to) is None
    assert "said no" in crm._queued_mail_hold({"status": "dead", "email": to}, to, to)
    assert "signed up" in crm._queued_mail_hold({"status": "won", "email": to}, to, to)
    assert "changed to jed@bar.example" in crm._queued_mail_hold(
        {"status": "warm", "email": "jed@bar.example"}, to, to)
    assert "deleted" in crm._queued_mail_hold(None, to, to)


def test_mail_deliberately_sent_elsewhere_still_goes():
    # Queued to a cell the owner gave on the call, not the lead's own address.
    lead = {"status": "warm", "email": "info@bar.example"}
    assert crm._queued_mail_hold(lead, "dave.cell@gmail.com", "info@bar.example") is None
    # Queued before the snapshot existed: not judged on the address at all.
    assert crm._queued_mail_hold(lead, "brent@bar.example", None) is None


# ── 6. pages the crawler used to lose ───────────────────────────────────────

def test_a_page_that_is_not_utf8_is_still_read(monkeypatch):
    import subprocess
    body = "Caf\xe9 Olé — full bar ¥".encode("cp1252") + b"\n__STATUS__200"

    class Proc:
        stdout, stderr, returncode = body, b"", 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Proc())
    html, status = leadgen._http("https://cafe.example")
    assert status == 200 and "full bar" in html and "Café" in html


def test_the_drinks_list_after_a_long_script_is_read():
    page = "<script>" + "x" * 400_000 + "</script><p>Negroni · Old Tom gin · rye whiskey</p>"
    assert "Negroni" in leadgen._page_text(page)


def test_a_long_tag_never_leaks_into_the_text_or_becomes_a_manager():
    page = ('<div data-x="' + "a" * 5000 + ' gallery-manager.js"></div><p>Welcome in!</p>')
    assert "gallery" not in contacts.visible_text(page)
    assert contacts.find_manager(page) is None
    assert contacts.find_manager("<p>gallery-manager</p>") is None      # lowercase is no name


def test_a_pdf_link_with_a_trailing_space_is_not_fetched():
    home = '<a href="/menus/happy-hour.pdf%20">Drinks</a><a href="/drinks">Drinks</a>'
    assert leadgen._drink_links("https://bar.example/", home) == ["https://bar.example/drinks"]


# ── 7. the venue's own name is not a person ────────────────────────────────

def test_a_mailbox_named_after_the_venue_is_not_personal():
    assert contacts.email_kind("sweedeedee@gmail.com", "Sweedeedee") == "unknown"
    assert contacts.email_kind("tootsies@gmail.com", "Tootsie's Orchid Lounge") == "unknown"
    assert contacts.email_kind("dave@joesbar.com", "Joe's Bar") == "personal"


# ── 8. cleaning what is already on the list ─────────────────────────────────

class _Cur:
    def __init__(self, leads, cands):
        self.leads, self.cands, self.writes, self._rows, self.rowcount = leads, cands, [], [], 0

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self._rows, self.rowcount = [], 0
        if s.startswith("SELECT l.id, l.name, l.email, c.website"):
            self._rows = self.leads
        elif s.startswith("SELECT id, name, email, website FROM crm_lead_candidates"):
            self._rows = self.cands
        elif s.startswith("UPDATE"):
            self.writes.append((s, params))
            self.rowcount = 1
        else:
            raise AssertionError(s[:80])

    def fetchall(self):
        return list(self._rows)


def test_foreign_emails_come_off_unemailed_leads_and_the_bank():
    cur = _Cur([{"id": "L1", "name": "Martin's Bar-B-Que Joint", "email": "hi@dianabartonpr.com",
                 "website": "https://www.martinsbbqjoint.com/"},
                {"id": "L2", "name": "Limelight", "email": "info@limelightloungepdx.com",
                 "website": "https://limelightloungepdx.com/"}],
               [{"id": "C1", "name": "Suzy Wong's House of Yum", "email": "templates@wavesdesign.io",
                 "website": "https://suzywongsnashville.com/"}])
    assert leadgen._reconcile_foreign_emails(cur) == (1, 1)
    (lead_sql, lead_params), (cand_sql, cand_params) = cur.writes
    assert lead_params[-2:] == ("L1", "hi@dianabartonpr.com") and "another business" in lead_params[0]
    assert cand_params == ("C1",)


def test_after_a_clean_up_the_list_refills_from_checked_venues(monkeypatch):
    calls = []
    monkeypatch.setattr(leadgen, "verify_fit", lambda lead_limit=20, bank_limit=20, **k:
                        {"leads_checked": 0} if bank_limit == 0 else
                        {"bank_checked": 5, "bank_ok": 3})
    monkeypatch.setattr(leadgen, "bucket_deficits", lambda *a: {("dinner", -5): 4})
    monkeypatch.setattr(leadgen, "promote_leads", lambda n: calls.append(n) or n)
    assert leadgen.fit_check_step() == 5 and calls == [4]


def test_attribution_never_matches_on_a_provider_domain():
    assert crm._email_domain("joe@cox.net") == ""
    assert crm._email_domain("dave@bellsouth.net") == ""
    assert crm._email_domain("gm@hospitalitygroup.com") == "hospitalitygroup.com"


def test_a_stray_byte_keeps_a_utf8_page_utf8():
    raw = "Café Olé — full bar".encode("utf-8") + b"\xa5"
    assert "Café Olé" in leadgen._decode(raw)
    assert leadgen._decode("Tootsie\u2019s".encode("cp1252") * 5) .startswith("Tootsie\u2019s")
