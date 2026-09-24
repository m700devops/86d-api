"""The AI quick-add lead flow, and the debrief refactor it was built on top of.

`_apply_call_notes()` is the guts of `POST /leads/{id}/debrief` (an existing
lead) extracted so `POST /leads/quick-add` (a lead this same request just
inserted) can share it exactly rather than re-implementing a thinner copy —
see the docstring on `_apply_call_notes` in crm.py. The extraction moved every
line of debrief's SQL-building without touching its logic, which is exactly
the kind of change that silently breaks — a `sets`/`params` list falling out
of lockstep raises in production, not in a diff review. These tests exercise
the real function against a fake cursor that enforces the one invariant that
matters (every `%s` in a query has exactly one param) and applies UPDATEs
onto a row dict, so the tests can also assert on what actually got written.
"""
import re
import sys
import types

import pytest

# crm imports `database`, which raises at import time without DATABASE_URL.
if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402


def _split_top_level(s: str) -> list:
    """Split on commas that aren't inside parens — plain str.split(",") breaks
    on notes' `COALESCE(notes || E'\\n', '') || %s` expression, which has a
    comma of its own inside the COALESCE call.
    """
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _sql_literal(tok: str):
    """A non-%s VALUES token ('new', 'manual', 0) back to a Python value."""
    tok = tok.strip()
    if tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1]
    if tok.lstrip("-").isdigit():
        return int(tok)
    if tok.upper() == "NULL":
        return None
    return tok


class _FakeCursor:
    """A cursor that enforces param-count/placeholder-count parity and keeps
    a running row so RETURNING-style fetches reflect what was written.
    """

    def __init__(self, initial_row=None, existing=None):
        self.row = dict(initial_row) if initial_row else {}
        # Leads already in the book, for quick-add's "is this bar here?" lookups.
        self.existing = [dict(r) for r in (existing or [])]
        self._last = None
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        params = list(params or [])
        n = sql.count("%s")
        assert n == len(params), (
            f"SQL wants {n} params but got {len(params)}:\n{sql}\nparams={params}"
        )
        self.executed.append((sql.strip(), params))
        s = sql.strip()

        if s.startswith("INSERT INTO crm_leads"):
            cols = re.search(r"\(([^)]+)\)\s*VALUES", s, re.S).group(1)
            cols = [c.strip() for c in cols.split(",")]
            vals = re.search(r"VALUES\s*\(([^)]+)\)", s, re.S).group(1)
            tokens = [v.strip() for v in vals.split(",")]
            assert len(cols) == len(tokens), f"{cols} vs {tokens}"
            param_iter = iter(params)
            inserted = {col: (next(param_iter) if tok == "%s" else _sql_literal(tok))
                       for col, tok in zip(cols, tokens)}
            # RETURNING * gives every column, not just the ones this INSERT
            # named — the rest are NULL defaults, same as real Postgres.
            self.row = {k: None for k in crm.LEAD_COLUMNS}
            self.row.update(inserted)
            self._last = dict(self.row)
        elif s.startswith("UPDATE crm_leads SET"):
            set_clause = re.search(r"SET (.+) WHERE id = %s", s, re.S).group(1)
            col_exprs = [c.strip() for c in _split_top_level(set_clause)]
            values = params[:-1]  # last param is the WHERE id
            assert len(col_exprs) == len(values)
            for expr, val in zip(col_exprs, values):
                col = expr.split("=")[0].strip()
                if col == "notes":
                    # COALESCE(notes || E'\n', '') || %s
                    prior = self.row.get("notes") or ""
                    self.row["notes"] = (prior + "\n" + val) if prior else val
                else:
                    self.row[col] = val
            self._last = dict(self.row)
        elif s.startswith("SELECT * FROM crm_leads"):
            if "FOR UPDATE" in s:
                hit = next((r for r in self.existing if r["id"] == params[0]), None)
                self.row = dict(hit) if hit else {}
                self._last = dict(hit) if hit else None
            else:
                if "regexp_replace" in s:
                    keep = lambda r: re.sub(r"\D", "", r.get("phone") or "")[-10:] == params[0]
                elif "LOWER(email)" in s:
                    keep = lambda r: (r.get("email") or "").lower() == params[0].lower()
                elif "split_part" in s:
                    keep = lambda r: (r.get("loc") or "").split(",")[0].strip().lower() == params[0]
                else:
                    raise AssertionError(f"unexpected lookup in test: {s[:80]}")
                self._rows = [dict(r) for r in self.existing if keep(r)]
                self._last = self._rows[0] if self._rows else None
        elif s.startswith("UPDATE crm_counters"):
            self._last = {k: 0 for k in crm.COUNTER_COLUMNS}
            self._last.update(id=1, daily_calls_remaining=24,
                              daily_emails_remaining=10, daily_fb_remaining=10,
                              touch_ticker_remaining=99)
        elif (s.startswith("INSERT INTO crm_lead_undo")
              or s.startswith("INSERT INTO crm_touches")
              or s.startswith("UPDATE crm_lead_undo")):
            self._last = None
        else:
            raise AssertionError(f"unexpected SQL in test: {s[:80]}")

    def fetchone(self):
        return self._last

    def fetchall(self):
        return list(self._rows)


def _lead(**kw):
    row = {k: None for k in crm.LEAD_COLUMNS}
    row.update(id="L1", name="Murphy's Pub", loc="Nashville, TN", status="new",
              attempts=0, tz_offset_hours=-6, email_date=None)
    row.update(kw)
    return row


def test_apply_call_notes_on_an_existing_lead_matches_debrief_shape():
    cur = _FakeCursor()
    lead = _lead()
    extracted = {"status": "warm", "outcome": "callback", "contact": "Sarah",
                "followup_in_days": 3,
                "summary": "Talked to Sarah, she wants a callback Thursday."}
    updated, applied, undo_id, counters = crm._apply_call_notes(
        cur, lead, extracted, "raw text here", "call", "2026-09-22", "2026-09-22T12:00:00Z")

    assert updated["status"] == "warm"
    assert updated["contact"] == "Sarah"
    assert updated["attempts"] == 1
    assert updated["last_outcome"] == "callback"
    assert applied["attempt"] == 1
    assert applied["status"] == "warm"
    assert applied["outcome"] == "callback"
    # +3 days from the day the call is logged — the same "today" the note is
    # stamped with, never a second read of the clock.
    assert applied["followup_date"] == "2026-09-25"
    assert counters["daily_calls_remaining"] == 24
    assert undo_id


def test_apply_call_notes_uses_the_models_outcome_not_a_status_guess():
    # The bug: "left a voicemail" correctly lands status "contacted" (a
    # voicemail is still contact attempted), but the OLD code then re-derived
    # last_outcome from status alone — anything landing on warm/won/contacted
    # became "answered", so a voicemail and an actual conversation were
    # indistinguishable on screen. The model is now asked for outcome
    # directly and that value must win.
    cur = _FakeCursor()
    lead = _lead()
    extracted = {"status": "contacted", "outcome": "voicemail",
                "summary": "Left a voicemail, no answer."}
    updated, applied, _, _ = crm._apply_call_notes(
        cur, lead, extracted, "left a voicemail", "call",
        "2026-09-22", "2026-09-22T12:00:00Z")

    assert updated["status"] == "contacted"
    assert updated["last_outcome"] == "voicemail"
    assert applied["outcome"] == "voicemail"
    # A voicemail didn't reach anyone, so the cadence ladder should still
    # schedule the next attempt — the old "answered" mislabel made _cadence
    # think the call succeeded and skipped scheduling one entirely.
    assert applied["followup_date"]


def test_apply_call_notes_gatekeeper_outcome_is_not_collapsed_to_answered():
    cur = _FakeCursor()
    lead = _lead()
    extracted = {"status": "contacted", "outcome": "gatekeeper",
                "summary": "Spoke to a bartender, manager wasn't in."}
    updated, _, _, _ = crm._apply_call_notes(
        cur, lead, extracted, "spoke to staff", "call",
        "2026-09-22", "2026-09-22T12:00:00Z")
    assert updated["last_outcome"] == "gatekeeper"


def test_apply_call_notes_falls_back_to_status_guess_when_model_omits_outcome():
    # Older extractions, or a model that skips the field: don't lose the
    # outcome entirely, fall back to the coarse status-based guess.
    cur = _FakeCursor()
    lead = _lead()
    extracted = {"status": "warm", "summary": "Sounded interested."}
    updated, applied, _, _ = crm._apply_call_notes(
        cur, lead, extracted, "raw text", "call", "2026-09-22", "2026-09-22T12:00:00Z")
    assert updated["last_outcome"] == "callback"
    assert applied["outcome"] == "callback"


def test_nobody_picked_up_is_never_answered_even_if_the_model_says_so():
    # Pig & the Sprout: "no one picked up the phone, and you can't leave a
    # message" was logged "Answered". The operator's own words win.
    cur = _FakeCursor()
    lead = _lead()
    text = "no one picked up the phone, and you cant leave a message"
    extracted = {"status": "contacted", "outcome": "answered", "summary": text}
    updated, applied, _, _ = crm._apply_call_notes(
        cur, lead, extracted, text, "call", "2026-09-22", "2026-09-22T12:00:00Z")
    assert updated["last_outcome"] == "no_answer"
    # Nobody reached, so the retry ladder still schedules the next try.
    assert applied["followup_date"]


def test_contacted_with_no_outcome_is_not_guessed_as_answered():
    cur = _FakeCursor()
    lead = _lead()
    updated, _, _, _ = crm._apply_call_notes(
        cur, lead, {"status": "contacted"}, "called them", "call",
        "2026-09-22", "2026-09-22T12:00:00Z")
    assert updated["last_outcome"] != "answered"


def test_a_real_callback_is_not_overridden_by_no_answer_wording():
    cur = _FakeCursor()
    lead = _lead()
    text = "nobody answered the first time, called back and Dave wants a demo Tuesday"
    updated, _, _, _ = crm._apply_call_notes(
        cur, lead, {"status": "warm", "outcome": "callback"}, text, "call",
        "2026-09-22", "2026-09-22T12:00:00Z")
    assert updated["last_outcome"] == "callback"


def test_the_operators_own_words_are_kept_verbatim_under_the_summary():
    # The Barrel House: the summary kept "spoke with Laura for 40 minutes" and
    # lost the cat, the $800 vet bill and the patent comment — the details a
    # callback opens with. The raw notes now always ride along.
    cur = _FakeCursor()
    lead = _lead()
    text = ("Talked to Laura the manager for 40 min. She just paid $800 at the vet\n"
            "for her cat yesterday. Thinks I should get a patent on the tech. Passing to her boss.")
    extracted = {"status": "warm", "outcome": "callback",
                 "summary": "Spoke with Laura, the manager, who will pass info to her boss."}
    updated, applied, _, _ = crm._apply_call_notes(
        cur, lead, extracted, text, "call", "2026-09-24", "2026-09-24T12:00:00Z")
    note = updated["notes"].splitlines()[-1]          # still ONE line per call
    assert "will pass info to her boss" in note
    assert "$800 at the vet for her cat" in note
    assert "patent" in note
    assert applied["note"] == note


def test_raw_notes_are_not_repeated_when_they_are_the_summary():
    cur = _FakeCursor()
    lead = _lead()
    updated, _, _, _ = crm._apply_call_notes(
        cur, lead, {}, "left a voicemail", "call", "2026-09-24", "2026-09-24T12:00:00Z")
    assert "Your notes" not in updated["notes"]


def test_left_a_voicemail_stays_voicemail():
    assert crm._no_answer_outcome("left a voicemail, no answer") == "voicemail"
    assert crm._no_answer_outcome("rang out, mailbox full") == "no_answer"
    assert crm._no_answer_outcome("talked to the owner") is None


def test_apply_call_notes_falls_back_to_raw_text_when_model_gives_no_summary():
    cur = _FakeCursor()
    lead = _lead()
    updated, applied, _, _ = crm._apply_call_notes(
        cur, lead, {}, "left a voicemail, no answer", "call",
        "2026-09-22", "2026-09-22T12:00:00Z")
    assert "left a voicemail, no answer" in updated["notes"]


def test_apply_call_notes_cadence_forces_a_status_when_model_gives_none():
    # No status, no answered/callback/not_interested outcome -> the cadence
    # ladder decides, same as a real /debrief with thin notes.
    cur = _FakeCursor()
    lead = _lead(attempts=crm.MAX_ATTEMPTS - 1)
    updated, applied, _, _ = crm._apply_call_notes(
        cur, lead, {}, "no answer again", "call", "2026-09-22", "2026-09-22T12:00:00Z")
    assert updated["status"] == "dead"
    assert applied["status"] == "dead"


class _Conn:
    def __init__(self, cur): self.cur = cur
    def cursor(self): return self.cur
    def commit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _no_lookup(monkeypatch, website=None, email=None):
    import leadgen
    monkeypatch.setattr(leadgen, "find_venue_website", lambda name, loc=None: website)
    monkeypatch.setattr(leadgen, "find_email_on_site",
                        lambda site: (email, site + "/contact") if email else (None, None))


def test_quick_add_lead_creates_and_logs_in_one_transaction(monkeypatch):
    cur = _FakeCursor()
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    _no_lookup(monkeypatch)
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text, *a: {
        "name": "Murphy's Pub", "loc": "Nashville, TN", "status": "warm",
        "outcome": "callback", "contact": "Sarah", "email": "sarah@murphys.com",
        "followup_in_days": 2, "summary": "Sarah wants a callback Thursday.",
    })
    result = crm.quick_add_lead(crm.QuickAdd(text="Murphy's Pub, talked to Sarah..."), True)
    assert result["lead"]["name"] == "Murphy's Pub"
    assert result["lead"]["status"] == "warm"
    assert result["applied"]["outcome"] == "callback"
    assert result["undo_id"]


def test_quick_add_falls_back_to_the_leading_text_when_the_model_drops_the_name(monkeypatch):
    # The real input that failed: name stated twice in pasted listing text,
    # model still returned no name. The operator must not have to retype it.
    cur = _FakeCursor()
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    _no_lookup(monkeypatch)
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text, *a: {"outcome": "gatekeeper"})
    text = ("Olde Town Tavern & Grill at (720) 242-9667 or (303) 467-1472.Location & "
            "ContactAddress: 7355 Ralston Rd, Arvada, CO 80002 ... Taylor the bartender picked up")
    result = crm.quick_add_lead(crm.QuickAdd(text=text), True)
    assert result["lead"]["name"] == "Olde Town Tavern & Grill"


def test_quick_add_finds_the_email_on_their_website(monkeypatch):
    cur = _FakeCursor()
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    _no_lookup(monkeypatch, website="https://oldetowntavern.com", email="owners@oldetowntavern.com")
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text, *a: {
        "name": "Olde Town Tavern & Grill", "loc": "Arvada, CO", "email_on_website": True,
        "decision_makers": "Mallory and Mike (owners)", "contact": "Taylor (bartender)",
        "outcome": "gatekeeper", "status": "contacted",
    })
    result = crm.quick_add_lead(crm.QuickAdd(text="Olde Town Tavern & Grill..."), True)
    assert result["lead"]["email"] == "owners@oldetowntavern.com"
    assert result["applied"]["email_found_on"] == "https://oldetowntavern.com/contact"
    notes = result["lead"]["notes"]
    assert "Decision makers: Mallory and Mike (owners)" in notes
    assert "Website: https://oldetowntavern.com" in notes


def test_quick_add_refuses_only_when_no_name_anywhere(monkeypatch):
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(_FakeCursor()))
    _no_lookup(monkeypatch)
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text, *a: {})
    with pytest.raises(crm.HTTPException) as exc:
        crm.quick_add_lead(crm.QuickAdd(text="(720) 242-9667"), True)
    assert exc.value.detail["error"] == "no_name"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_quick_add_logs_onto_the_bar_already_on_the_call_list(monkeypatch):
    # The real duplicate: the generator had "Olde Town Tavern" on the call
    # list; quick-adding "Olde Town Tavern & Grill" made a second row, and the
    # never-called copy stayed on the call list.
    on_list = _lead(id="GEN1", name="Olde Town Tavern", loc="Arvada, CO",
                    phone="720-242-9667", source="leadgen", last_touch_at=None,
                    created_at="2026-09-18T00:00:00Z")
    cur = _FakeCursor(existing=[on_list])
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    _no_lookup(monkeypatch)
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text, *a: {
        "name": "Olde Town Tavern & Grill", "loc": "Arvada, CO", "phone": "(720) 242-9667",
        "outcome": "gatekeeper", "contact": "Taylor", "summary": "Taylor the bartender picked up.",
        "decision_makers": "Mike (owner)",
    })
    result = crm.quick_add_lead(crm.QuickAdd(text="Olde Town Tavern & Grill at (720) 242-9667 ..."), True)
    assert not any(sql.startswith("INSERT INTO crm_leads") for sql, _ in cur.executed)
    assert result["lead"]["id"] == "GEN1"                 # the call is on the existing row
    assert result["applied"]["matched_existing"] == "Olde Town Tavern"
    assert result["lead"]["last_touch_at"]               # so it has left the call list
    assert "Decision makers: Mike (owner)" in result["lead"]["notes"]


def test_quick_add_still_creates_a_lead_for_a_different_bar_on_the_same_phone(monkeypatch):
    # One owner, two bars, one number: a shared phone alone is not a duplicate.
    sister = _lead(id="GEN2", name="Blue Room", loc="Arvada, CO", phone="720-242-9667",
                   source="leadgen", created_at="2026-09-18T00:00:00Z")
    cur = _FakeCursor(existing=[sister])
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    _no_lookup(monkeypatch)
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text, *a: {
        "name": "The Monkey Bar", "phone": "720-242-9667", "outcome": "voicemail"})
    result = crm.quick_add_lead(crm.QuickAdd(text="The Monkey Bar, 720-242-9667, voicemail"), True)
    assert any(sql.startswith("INSERT INTO crm_leads") for sql, _ in cur.executed)
    assert result["lead"]["name"] == "The Monkey Bar"
    assert "matched_existing" not in result["applied"]


def test_quick_add_matches_on_any_number_in_the_paste(monkeypatch):
    on_list = _lead(id="GEN3", name="Olde Town Tavern", loc="Arvada, CO",
                    phone="303-467-1472", source="leadgen", created_at="2026-09-18T00:00:00Z")
    cur = _FakeCursor(existing=[on_list])
    monkeypatch.setattr(crm, "get_db", lambda: _Conn(cur))
    _no_lookup(monkeypatch)
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text, *a: {
        "name": "Olde Town Tavern & Grill", "phone": "(720) 242-9667", "outcome": "gatekeeper"})
    result = crm.quick_add_lead(crm.QuickAdd(
        text="Olde Town Tavern & Grill at (720) 242-9667 or (303) 467-1472. Taylor picked up"), True)
    assert result["lead"]["id"] == "GEN3"
