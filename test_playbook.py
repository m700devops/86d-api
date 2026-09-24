"""The company brain: the owner's standing instructions and the playbook the
AI learns from the log. The gate that matters is `clean()`: a "learning"
that can't be traced to a bar in the log is dropped, which is what keeps the
playbook from filling up with generic sales advice and invented patterns.
"""
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import crm  # noqa: E402
import playbook  # noqa: E402

LOG_NAMES = ["Workhorse Bar", "Olde Town Tavern & Grill", "Rioja", "Mean Eyed Cat"]


# ── the evidence gate ───────────────────────────────────────────────────────

def test_a_point_needs_a_real_bar_behind_it():
    out = {"summary": "Owners decide; bartenders don't.", "sections": [
        {"title": "Objections we hear", "points": [
            {"text": "The owner does the ordering (2 bars).",
             "evidence": ["Workhorse Bar", "olde town tavern"]},
            {"text": "Always follow up within 24 hours.", "evidence": []},
            {"text": "Bars love free trials.", "evidence": ["The Made Up Saloon"]}]},
        {"title": "Stop doing", "points": [{"text": "x", "evidence": ["Nowhere"]}]}]}
    pb = playbook.clean(out, LOG_NAMES)
    assert pb["summary"] == "Owners decide; bartenders don't."
    assert len(pb["sections"]) == 1                     # the empty one is gone
    pts = pb["sections"][0]["points"]
    assert len(pts) == 1
    assert pts[0]["evidence"] == ["Workhorse Bar", "Olde Town Tavern & Grill"]


def test_the_playbook_is_capped():
    out = {"summary": "s", "sections": [{"title": f"T{i}", "points": [
        {"text": "p" * 999, "evidence": ["Rioja"]} for _ in range(20)]} for i in range(20)]}
    pb = playbook.clean(out, LOG_NAMES)
    assert len(pb["sections"]) == playbook.MAX_SECTIONS
    assert all(len(sec["points"]) == playbook.MAX_POINTS for sec in pb["sections"])
    assert len(pb["sections"][0]["points"][0]["text"]) == playbook.POINT_CHARS


def test_prompts_see_counts_never_other_bars_names():
    pb = {"summary": "s", "sections": [{"title": "Objections we hear", "points": [
        {"text": "Owner does the ordering.", "evidence": ["Workhorse Bar", "Rioja"]}]}]}
    text = playbook.render(pb, "2026-09-25T10:00:00+00:00")
    assert "Owner does the ordering. [2 bars]" in text and "(updated 2026-09-25)" in text
    assert "Workhorse" not in text and "Rioja" not in text
    assert "never product facts" in text
    assert playbook.render(None) == "" and playbook.render({"sections": []}) == ""


def test_owner_notes_are_authoritative_and_empty_is_nothing():
    assert "follow them" in playbook.render_owner("Demos Tue/Thu afternoons.")
    assert playbook.render_owner("   ") == ""


def test_the_digest_keeps_the_newest_notes_and_marks_replies():
    leads = [{"name": "Workhorse Bar", "loc": "Austin, TX", "status": "contacted",
              "last_outcome": "gatekeeper", "notes": "old " * 1000 + "NEWEST CALL"}]
    d = playbook.digest(leads, [{"from": "Jed", "about": "Mean Eyed Cat", "said": "talk to me"}],
                        [{"lead": "Rioja", "subject": "inventory in 15 min", "replied": True},
                         {"lead": "Workhorse Bar", "subject": "quick one", "replied": False}],
                        "2026-09-25", notes_chars=200)
    assert "## Workhorse Bar (Austin, TX) | stage contacted" in d
    assert d.count("old") < 60 and "NEWEST CALL" in d
    assert "Jed about Mean Eyed Cat: talk to me" in d
    assert "EMAILS WE SENT (2, 1 got a reply)" in d and "REPLIED" in d


# ── when it re-learns ───────────────────────────────────────────────────────

def _wire(monkeypatch, row, touches, model=None):
    writes = []

    class Cur:
        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            if self.sql.startswith("UPDATE"):
                writes.append((self.sql, params))

        def fetchone(self):
            return row

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_playbook_inputs",
                        lambda cursor, today: ("LOG ...", LOG_NAMES, touches))
    calls = []

    def fake(system, user, schema, **kw):
        calls.append(user)
        return model or {"summary": "s", "sections": [{"title": "Objections we hear", "points": [
            {"text": "Owner orders.", "evidence": ["Rioja"]}]}]}

    monkeypatch.setattr(crm, "_claude_json", fake)
    return writes, calls


def test_too_little_logged_means_no_model_call(monkeypatch):
    writes, calls = _wire(monkeypatch, {}, touches=3)
    out = crm.refresh_playbook(force=True)
    assert "starts learning at" in out["skipped"] and not calls and not writes


def test_a_fresh_playbook_with_nothing_new_is_left_alone(monkeypatch):
    now = crm.now_iso()
    writes, calls = _wire(monkeypatch, {"playbook": "{}", "playbook_refreshed_at": now,
                                        "playbook_touches": 40}, touches=41)
    assert "nothing new" in crm.refresh_playbook()["skipped"] and not calls


def test_new_activity_after_a_day_re_learns(monkeypatch):
    writes, calls = _wire(monkeypatch, {"playbook": "{}", "playbook_refreshed_at":
                                        "2026-09-01T00:00:00+00:00", "playbook_touches": 40},
                          touches=48)
    out = crm.refresh_playbook()
    assert out["refreshed"] and out["points"] == 1 and calls == ["LOG ..."]
    sql, params = writes[0]
    assert "SET playbook = %s" in sql and params[2] == 48


def test_force_re_learns_even_when_quiet(monkeypatch):
    writes, calls = _wire(monkeypatch, {"playbook": "{}", "playbook_refreshed_at": crm.now_iso(),
                                        "playbook_touches": 40}, touches=40)
    assert crm.refresh_playbook(force=True)["refreshed"] and calls


def test_a_model_failure_is_recorded_not_raised(monkeypatch):
    writes, _ = _wire(monkeypatch, {}, touches=10)

    def boom(*a, **k):
        raise crm.HTTPException(status_code=503, detail={"error": "ai_unavailable",
                                                          "message": "The AI returned 529"})
    monkeypatch.setattr(crm, "_claude_json", boom)
    out = crm.refresh_playbook(force=True)
    assert out["refreshed"] is False and "529" in out["error"]
    assert "playbook_error" in writes[0][0]


def test_owner_notes_save_and_come_back(monkeypatch):
    saved = {}

    class Cur:
        def execute(self, sql, params=None):
            if "INSERT INTO crm_ai_brain" in sql:
                saved["notes"] = params[0]

        def fetchone(self):
            return {"owner_notes": saved.get("notes"), "playbook": None}

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    monkeypatch.setattr(crm, "get_db", db)
    crm.save_owner_notes(crm.OwnerNotes(text="  Demos Tue/Thu.  "), True)
    assert saved["notes"] == "Demos Tue/Thu."
    assert "Demos Tue/Thu." in crm._knowledge()
    assert crm.brain(True)["owner_notes"] == "Demos Tue/Thu."
