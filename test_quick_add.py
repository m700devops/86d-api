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

import pydantic
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

    def __init__(self, initial_row=None):
        self.row = dict(initial_row) if initial_row else {}
        self._last = None
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
    assert updated["last_outcome"] == "answered"
    assert applied["outcome"] == "answered"


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


def test_quick_add_lead_creates_and_logs_in_one_transaction(monkeypatch):
    cur = _FakeCursor()

    class _Conn:
        def cursor(self_): return cur
        def commit(self_): pass
        def __enter__(self_): return self_
        def __exit__(self_, *a): return False

    monkeypatch.setattr(crm, "get_db", lambda: _Conn())
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text: {
        "loc": "Nashville, TN", "status": "warm", "outcome": "callback",
        "contact": "Sarah", "followup_in_days": 2,
        "summary": "Sarah the manager wants a callback Thursday.",
    })

    result = crm.quick_add_lead(
        crm.QuickAdd(name="Murphy's Pub", text="talked to Sarah..."), True)

    assert result["lead"]["name"] == "Murphy's Pub"
    assert result["lead"]["status"] == "warm"
    assert result["applied"]["contact"] == "Sarah"
    assert result["applied"]["outcome"] == "callback"
    assert result["undo_id"]


def test_quick_add_lead_name_is_typed_not_extracted(monkeypatch):
    # The bar's name used to be pulled from the free text by the model — a
    # real harvested example (name stated twice, in messy pasted contact-
    # block text) still came back "couldn't tell which bar this was", the
    # exact failure mode a required, directly-typed field can't have: the
    # model's extraction is never even asked for a name any more, and the
    # lead still gets created correctly from whatever the operator typed.
    cur = _FakeCursor()

    class _Conn:
        def cursor(self_): return cur
        def commit(self_): pass
        def __enter__(self_): return self_
        def __exit__(self_, *a): return False

    monkeypatch.setattr(crm, "get_db", lambda: _Conn())
    monkeypatch.setattr(crm, "_quick_add_extract", lambda text: {"summary": "some call"})

    result = crm.quick_add_lead(
        crm.QuickAdd(name="Olde Town Tavern & Grill", text="had a call, went fine"), True)
    assert result["lead"]["name"] == "Olde Town Tavern & Grill"


def test_quick_add_requires_a_name():
    with pytest.raises(pydantic.ValidationError):
        crm.QuickAdd(text="had a call, went fine")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
