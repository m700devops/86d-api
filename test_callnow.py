"""Calling mode shows every dialable lead, whatever the clock says.

`GET /v1/crm/now` sorts by whether a venue is in its calling window. It used to
FILTER by it: the bucket loop was an `if/elif` with no `else`, so every venue
past its window or shut today fell off the end and never reached the page, and
the page then rendered a one-line "nothing in a window" instead of the table.
Outside US afternoons that is most of the list, so pressing "Ready to start
calling" emptied a screen with hundreds of banked leads behind it — while the
tab view, which ranks these same rows rather than dropping them, still showed
every one.

The window decides ORDER and LABEL. It must never decide existence. These tests
stub the window itself (callwindow's own logic is covered by test_callwindow.py)
so the bucketing, ordering and headlines are checked against a fixed clock.
"""
import sys
import types

# crm imports `database`, which raises at import time without DATABASE_URL.
if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402


class _Cursor:
    """Answers call_now's two queries: the lead SELECT, then the done-today COUNT."""

    def __init__(self, rows):
        self._rows, self._n = rows, 0

    def execute(self, sql, params=None):
        self._n += 1
        self._last = sql

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return {"n": 0}


class _Conn:
    def __init__(self, rows):
        self._c = _Cursor(rows)

    def cursor(self):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _lead(name, phone="+1-615-742-9095", **kw):
    row = {k: None for k in crm.LEAD_COLUMNS}
    row.update(id=f"L-{name}", name=name, phone=phone, status="new",
               loc="Nashville, TN", attempts=0, lead_score=0)
    row.update(kw)
    return row


WINDOWS = {
    "good": {"good_now": True, "state": "good", "known": True,
             "hint": "CALL NOW — open", "local_time": "2:30pm", "window": "2-4pm"},
    "early": {"good_now": False, "state": "early", "known": True, "starts_in": 45,
              "hint": "Opens in 45m", "local_time": "10:15am", "window": "11-11:45am",
              "starts_at_yours": "1:15am"},
    "late": {"good_now": False, "state": "late", "known": True,
             "hint": "Missed today's window (2-4pm)", "local_time": "9:40pm",
             "window": "2-4pm"},
    "shut_today": {"good_now": False, "state": "shut_today", "known": True,
                   "hint": "Closed today — try Tue", "local_time": "8:00pm",
                   "window": None},
    "permanently_closed": {"good_now": False, "state": "permanently_closed",
                           "known": True, "hint": "Marked permanently closed",
                           "local_time": "8:00pm", "window": None},
}


TOP_UPS: list = []


def _run(rows, states, limit=60):
    """call_now over `rows`, with each row's window forced by name via `states`."""
    by_name = dict(zip([r["name"] for r in rows], states))
    orig_db, orig_win, orig_top = crm.get_db, crm._call_window, crm._top_up_call_list
    crm.get_db = lambda: _Conn(rows)
    crm._top_up_call_list = lambda: TOP_UPS.append(1) or 0
    crm._call_window = lambda *a, **k: None  # replaced below per-row
    # _call_window is called positionally with (tz_offset, opening_hours, tz_name);
    # the row's name is smuggled through opening_hours so the stub can key on it.
    crm._call_window = lambda off, hours=None, tz=None: dict(WINDOWS[by_name[hours]])
    try:
        return crm.call_now(limit=limit)
    finally:
        crm.get_db, crm._call_window = orig_db, orig_win
        crm._top_up_call_list = orig_top


def _rows(*specs):
    """specs are (name, state) — opening_hours carries the name for the stub."""
    return ([_lead(n, opening_hours=n) for n, _ in specs],
            [s for _, s in specs])


def test_nothing_is_dropped_whatever_the_window():
    rows, states = _rows(("A", "good"), ("B", "early"), ("C", "late"),
                         ("D", "shut_today"), ("E", "permanently_closed"))
    d = _run(rows, states)
    assert d["ready_count"] + d["soon_count"] + d["rest_count"] == 5
    assert d["dialable_total"] == 5
    seen = {l["name"] for l in d["ready"] + d["soon"] + d["rest"]}
    assert seen == {"A", "B", "C", "D", "E"}


def test_out_of_window_leads_reach_the_page_with_none_ready():
    """The exact reported bug: no ready leads used to mean an empty screen."""
    rows, states = _rows(("C", "late"), ("D", "shut_today"))
    d = _run(rows, states)
    assert d["ready"] == [] and d["soon"] == []
    assert [l["name"] for l in d["rest"]] == ["C", "D"]
    assert d["next"] is not None and d["next"]["name"] == "C"


def test_rest_puts_still_open_venues_above_shut_ones():
    rows, states = _rows(("shut", "shut_today"), ("gone", "permanently_closed"),
                         ("open", "late"))
    d = _run(rows, states)
    assert [l["name"] for l in d["rest"]] == ["open", "shut", "gone"]


def test_headline_does_not_call_an_open_venue_shut():
    rows, states = _rows(("open", "late"))
    d = _run(rows, states)
    assert "shut" not in d["headline"].lower()
    assert "still open" in d["headline"]


def test_headline_says_shut_when_everything_really_is():
    rows, states = _rows(("a", "shut_today"), ("b", "shut_today"))
    d = _run(rows, states)
    assert "shut" in d["headline"].lower()


def test_next_is_the_ready_lead_when_one_exists():
    rows, states = _rows(("late", "late"), ("ready", "good"))
    d = _run(rows, states)
    assert d["next"]["name"] == "ready"


def test_undialable_numbers_are_not_offered_and_do_not_look_like_a_clock_problem():
    rows = [_lead("no-phone", phone="not a number", opening_hours="no-phone")]
    d = _run(rows, ["good"])
    assert d["dialable_total"] == 0
    assert d["unworked_total"] == 1
    assert d["list_empty"] is False
    assert "dialable number" in d["headline"]


def test_rest_count_is_the_real_total_when_the_page_is_capped():
    specs = [(f"v{i}", "late") for i in range(30)]
    rows, states = _rows(*specs)
    d = _run(rows, states, limit=5)
    assert len(d["rest"]) == 5
    assert d["rest_count"] == 30


def test_only_numbers_their_website_vouches_for_are_offered():
    # Measured on 102 real Denver bars: where the bar's own site listed a
    # number, the map's disagreed about one time in five. A generated lead is
    # only dialled once its website has vouched for the number.
    rows, states = _rows(("ok", "good"), ("fixed", "good"), ("unchecked", "good"),
                         ("conflict", "good"), ("mine", "good"))
    for r in rows:
        r["source"] = "leadgen"
        r["fit_status"] = "ok"
    rows[0]["phone_status"] = "confirmed"
    rows[1]["phone_status"] = "from_site"
    rows[3]["phone_status"] = "conflict"
    rows[4]["source"] = "manual"          # the operator's own entry: trusted as typed
    names = {l["name"] for l in _run(rows, states)["ready"]}
    assert names == {"ok", "fixed", "mine"}


def test_the_prep_sheet_profile_is_only_whats_on_file():
    # Kind, website and hours, all already stored: a generated lead's come
    # from its candidate, a quick-added one's website from its notes.
    row = {"cand_website": "https://devonspub.example", "cand_amenity": "pub",
           "opening_hours": "Mo-Th 15:00-24:00", "notes": ""}
    assert crm._venue_profile(row) == {"kind": "pub", "website": "https://devonspub.example",
                                       "hours": "Mo-Th 15:00-24:00"}
    quick = {"cand_website": None, "cand_amenity": None, "opening_hours": None,
             "notes": "Decision makers: Mike · Website: https://oldetown.example | Next step: call"}
    assert crm._venue_profile(quick)["website"] == "https://oldetown.example"
    bare = {"cand_website": None, "cand_amenity": None, "opening_hours": None, "notes": None}
    assert crm._venue_profile(bare) == {"kind": None, "website": None, "hours": None}


# ── The owner's priorities (2026-09-25) ──────────────────────────────────────
# 1. no chains, 2. pours liquor, 3. no tourist strips — filters, applied by the
# generator and the background check, which stamp fit_status. 4. an email if
# they have one — the first thing the ready list sorts by. And only venues in
# their own calling window are "ready".

def test_a_generated_lead_is_offered_only_once_it_passed_the_owners_rules():
    rows, states = _rows(("passed", "good"), ("unchecked", "good"), ("strip", "good"),
                         ("mine", "good"))
    for r in rows:
        r.update(source="leadgen", phone_status="confirmed")
    rows[0]["fit_status"] = "ok"
    rows[2]["fit_status"] = "blocked"
    rows[3]["source"] = "manual"           # the operator's own entry: trusted as typed
    assert {l["name"] for l in _run(rows, states)["ready"]} == {"passed", "mine"}


def test_a_lead_with_an_email_comes_first_whatever_else_it_has():
    rows, states = _rows(("named, no email", "good"), ("role email", "good"),
                         ("direct email", "good"), ("nothing", "good"))
    rows[0].update(manager_name="Larry", lead_score=20)
    rows[1].update(email="info@bar.example", email_kind="role")
    rows[2].update(email="dave@bar.example", email_kind="personal")
    assert [l["name"] for l in _run(rows, states)["ready"]] == [
        "direct email", "role email", "named, no email", "nothing"]


def test_no_timezone_is_no_window_so_never_ready():
    rows, states = _rows(("zoneless", "unknown"), ("open", "good"))
    WINDOWS["unknown"] = {"good_now": True, "state": "unknown", "known": False,
                          "hint": "", "local_time": None, "window": None}
    try:
        d = _run(rows, states)
    finally:
        WINDOWS.pop("unknown")
    assert [l["name"] for l in d["ready"]] == ["open"]
    assert [l["name"] for l in d["rest"]] == ["zoneless"]


def test_the_generator_counts_exactly_the_leads_the_call_list_shows():
    # The list counts as full at LEADGEN_CALL_LIST_SIZE. Counting leads /now
    # hides (number not vouched for by the venue's site, owner's rules not
    # checked) would call it full while it showed two — and nothing refilled
    # it.
    import itertools
    import leadgen
    rows = []
    for source, phone, ps, fs in itertools.product(
            ("leadgen", "manual"), ("+1-615-742-9095", "555-0100", ""),
            (None, "confirmed", "from_site", "conflict", "unconfirmed", "wrong"),
            (None, "ok", "blocked")):
        name = f"{source}|{phone}|{ps}|{fs}"
        rows.append(_lead(name, phone=phone, source=source, phone_status=ps,
                          fit_status=fs, opening_hours=name))
    shown = {l["name"] for l in _run(rows, ["good"] * len(rows), limit=200)["ready"]}
    counted = {r["name"] for r in rows if leadgen.on_call_list(r)}
    assert counted == shown
    assert "leadgen|+1-615-742-9095|confirmed|ok" in shown
    assert "leadgen|+1-615-742-9095|confirmed|None" not in counted


# ── One list of up to 50, refilled one for one ──────────────────────────────

def test_opening_the_list_tops_it_up_first():
    # The page reloads /now after every logged call and delete, so this is
    # what puts the next bar in the slot the last one left.
    TOP_UPS.clear()
    rows, states = _rows(("A", "good"))
    _run(rows, states)
    assert TOP_UPS == [1]


def test_the_list_holds_at_most_the_call_list_size():
    import leadgen
    rows, states = _rows(*[(f"bar{i}", "good") for i in range(leadgen.CALL_LIST_SIZE + 10)])
    got = _run(rows, states, limit=None)
    assert len(got["ready"]) == leadgen.CALL_LIST_SIZE == 50
    assert got["ready_count"] == leadgen.CALL_LIST_SIZE + 10


class _BankCursor:
    """Hands promote_leads its bank, best score first as the SQL orders it,
    and one full row when it asks for a candidate by id."""

    def __init__(self, bank):
        self.bank, self.sql, self._one = bank, [], None

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "WHERE id = %s" in sql:
            self._one = next((c for c in self.bank if c["id"] == params[0]), None)

    def fetchall(self):
        return self.bank

    def fetchone(self):
        return self._one


class _BankConn:
    def __init__(self, cur):
        self.cur, self.committed = cur, False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _bank(monkeypatch, bank, in_window, refuse=()):
    import leadgen
    cur = _BankCursor(bank)
    conn = _BankConn(cur)
    promoted = []
    monkeypatch.setattr(leadgen, "get_db", lambda: conn)
    monkeypatch.setattr(leadgen, "corporate_index", lambda c: {})
    monkeypatch.setattr(leadgen, "_in_window_now", lambda c: c["id"] in in_window)
    monkeypatch.setattr(leadgen, "_promote_one", lambda c, cand, now, corp=None:
                        None if cand["id"] in refuse else promoted.append(cand["id"]) or "L")
    return promoted, conn


def test_only_bars_in_their_window_now_are_promoted_best_first(monkeypatch):
    import leadgen
    bank = [{"id": i} for i in ("best-shut", "best-open", "next-open", "third-open", "later")]
    promoted, conn = _bank(monkeypatch, bank, {"best-open", "next-open", "third-open"})
    assert leadgen.promote_leads(2) == 2
    assert promoted == ["best-open", "next-open"] and conn.committed


def test_a_refused_candidate_is_walked_past_not_counted(monkeypatch):
    import leadgen
    bank = [{"id": i} for i in ("dupe", "a", "b")]
    promoted, _ = _bank(monkeypatch, bank, {"dupe", "a", "b"}, refuse={"dupe"})
    assert leadgen.promote_leads(2) == 2 and promoted == ["a", "b"]


def test_top_up_fills_to_the_size_and_one_out_is_one_in(monkeypatch):
    import leadgen
    asked = []
    monkeypatch.setattr(leadgen, "promote_leads", lambda n: asked.append(n) or n)
    monkeypatch.setattr(leadgen, "ready_count", lambda *a: 12)
    assert leadgen.top_up() == leadgen.CALL_LIST_SIZE - 12
    monkeypatch.setattr(leadgen, "ready_count", lambda *a: leadgen.CALL_LIST_SIZE - 1)
    assert leadgen.top_up() == 1                  # a call logged: one replaced
    monkeypatch.setattr(leadgen, "ready_count", lambda *a: leadgen.CALL_LIST_SIZE)
    assert leadgen.top_up() == 0                  # full: nothing promoted
    assert asked == [leadgen.CALL_LIST_SIZE - 12, 1]


def test_ready_count_is_what_the_list_shows_in_a_window_now(monkeypatch):
    import leadgen
    rows = [_lead("open", source="leadgen", phone_status="confirmed", fit_status="ok"),
            _lead("shut", source="leadgen", phone_status="confirmed", fit_status="ok"),
            _lead("unchecked", source="leadgen", phone_status=None, fit_status="ok"),
            _lead("mine")]
    monkeypatch.setattr(leadgen, "_in_window_now", lambda r: r["name"] != "shut")
    cur = _BankCursor(rows)
    assert leadgen.ready_count(cur) == 2          # "open" and the operator's own


def test_no_timezone_is_never_in_a_window():
    # /now never lists a row with no zone as ready, so the top-up must never
    # promote one to fill a slot: it would count as filling it and not show.
    import leadgen
    assert leadgen._in_window_now({"opening_hours": "24/7", "tz_name": None,
                                   "tz_offset_hours": None}) is False
