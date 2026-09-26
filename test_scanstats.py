"""The scanner report (scanstats.py, GET /v1/crm/scanner) and the outcome route
that feeds it (POST /v1/scans/{scan_id}/outcome).

The report is pure, so it's tested row by row. What matters most is WHICH
evidence counts: a flagged disagreement settled by staff is a win or a loss, a
disagreement nobody was shown is never a win, and a row nobody touched is
neither. No database, no network.
"""
import contextlib
import json
import os
import sys

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import scanstats  # noqa: E402
from scanstats import summarize  # noqa: E402


def other(provider="gemini", model="g-flash", status="ok", ms=900, product_id="p2"):
    return json.dumps({"provider": provider, "model": model, "status": status,
                       "provider_ms": ms, "product_id": product_id, "output_tokens": 40})


def scan(status="ok", provider="openai", model="gpt-4o", path="both", opinion="agree",
         second=None, outcome=None, ms=1500, total=1700, fallback=None, out=60):
    return {"provider": provider, "model": model, "status": status, "path": path,
            "second_opinion": opinion, "second_answer": second, "provider_ms": ms,
            "total_ms": total, "output_tokens": out, "fallback_from": fallback, "outcome": outcome}


def model(report, provider):
    return next(m for m in report["models"] if m["provider"] == provider)


def test_nothing_yet():
    r = summarize([], 30)
    assert r["overall"]["scans"] == 0 and r["models"] == [] and r["thin"] is True
    assert r["overall"]["wait_median_ms"] is None


def test_what_each_scan_ended_as():
    r = summarize([scan(), scan(status="unreadable"), scan(status="label_unsupported"),
                   scan(status="no_bottle"), scan(status="timeout"), scan(status=None)], 30)
    o = r["overall"]
    assert (o["scans"], o["identified"], o["retakes"], o["no_bottle"], o["failed"]) == (6, 1, 2, 1, 2)


def test_both_models_are_counted_on_the_same_photos():
    r = summarize([
        scan(second=other()),
        scan(second=other(status="unreadable")),       # Gemini couldn't read it; OpenAI could
        scan(status="unreadable", second=other(status="unreadable")),
    ], 30)
    oa, ge = model(r, "openai"), model(r, "gemini")
    assert (oa["answers"], oa["unreadable"], oa["shown"]) == (3, 1, 2)
    assert (ge["answers"], ge["unreadable"], ge["shown"]) == (3, 2, 0)


def test_a_confirmed_flag_is_a_win_for_the_reading_shown_and_a_loss_for_the_other():
    r = summarize([scan(opinion="disagree", second=other(), outcome="confirmed")], 30)
    assert (model(r, "openai")["disputes_won"], model(r, "openai")["disputes_lost"]) == (1, 0)
    assert (model(r, "gemini")["disputes_won"], model(r, "gemini")["disputes_lost"]) == (0, 1)
    assert (r["overall"]["flagged"], r["overall"]["flagged_confirmed"], r["settled"]) == (1, 1, 1)


def test_a_removed_flag_is_a_loss_for_the_reading_shown_only():
    r = summarize([scan(opinion="disagree", second=other(), outcome="removed")], 30)
    assert (model(r, "openai")["disputes_won"], model(r, "openai")["disputes_lost"]) == (0, 1)
    # both may have been wrong: the other reading gets nothing
    assert (model(r, "gemini")["disputes_won"], model(r, "gemini")["disputes_lost"]) == (0, 0)
    assert r["overall"]["flagged_removed"] == 1 and r["overall"]["removed"] == 1


def test_a_flag_nobody_settled_is_no_evidence():
    r = summarize([scan(opinion="disagree", second=other())], 30)
    assert r["overall"]["flagged"] == 1 and r["settled"] == 0
    assert model(r, "openai")["disputes_won"] == model(r, "gemini")["disputes_lost"] == 0


@pytest.mark.parametrize("path", ["fast", "window"])
def test_a_disagreement_after_the_reply_was_never_shown_so_it_never_wins(path):
    r = summarize([scan(path=path, opinion="disagree", second=other(), outcome="confirmed"),
                   scan(path=path, opinion="disagree", second=other(), outcome="removed")], 30)
    o = r["overall"]
    assert (o["flagged"], o["late_contradicted"], o["late_contradicted_removed"]) == (0, 2, 1)
    assert model(r, "openai")["disputes_won"] == model(r, "openai")["disputes_lost"] == 0
    assert model(r, "openai")["removed"] == 1       # still a removed row


def test_agreement_is_not_a_dispute():
    r = summarize([scan(second=other(), outcome="confirmed")], 30)
    assert r["overall"]["flagged"] == 0 and r["settled"] == 0


def test_removed_counts_only_answers_that_were_shown():
    r = summarize([scan(outcome="removed"), scan(), scan(status="unreadable", outcome="removed")], 30)
    oa = model(r, "openai")
    assert (oa["shown"], oa["removed"], r["overall"]["removed"]) == (2, 1, 1)


def test_speed_is_each_models_own_reply_time():
    rows = [scan(ms=ms, second=other(ms=g), total=t)
            for ms, g, t in [(1000, 500, 1100), (2000, 600, 2100), (3000, 700, 3100),
                             (4000, 800, 4100), (9000, 5000, 9100)]]
    r = summarize(rows, 30)
    assert (model(r, "openai")["median_ms"], model(r, "openai")["p90_ms"]) == (3000, 9000)
    assert (model(r, "gemini")["median_ms"], model(r, "gemini")["p90_ms"]) == (700, 5000)
    assert (r["overall"]["wait_median_ms"], r["overall"]["wait_p90_ms"]) == (3100, 9100)
    assert model(r, "openai")["avg_output_tokens"] == 60.0


def test_failures_before_and_after_the_reply():
    r = summarize([
        scan(provider="gemini", fallback="openai:timeout"),
        scan(provider="gemini", fallback="openai:AuthenticationError"),
        scan(status="timeout", fallback="openai:timeout,gemini:timeout"),
        scan(path="fast", opinion="other_failed:unparseable"),       # Gemini, after an OpenAI fast reply
    ], 30)
    assert r["failures"] == {"openai": {"timeout": 2, "AuthenticationError": 1},
                             "gemini": {"timeout": 1, "unparseable": 1}}


def test_a_bad_second_answer_never_breaks_the_report():
    for bad in ("{not json", "[]", json.dumps({"status": "ok"}), 42):
        r = summarize([scan(second=bad)], 30)
        assert [m["provider"] for m in r["models"]] == ["openai"]


def test_thin_until_enough_disputes_are_settled():
    settled = [scan(opinion="disagree", second=other(), outcome="confirmed")] * scanstats.SETTLED_ENOUGH
    assert summarize(settled[:-1], 30)["thin"] is True
    assert summarize(settled, 30)["thin"] is False


def test_models_with_the_most_replies_first():
    r = summarize([scan(provider="gemini", model="g")] * 3 + [scan()], 30)
    assert [m["provider"] for m in r["models"]] == ["gemini", "openai"]


# ─── the routes ──────────────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=()):
        if self.db.fail:
            raise RuntimeError("database down")
        self.db.queries.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.db.rows


class FakeDB:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail, self.queries, self.commits = list(rows), fail, [], 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


@pytest.fixture
def app_client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    db = FakeDB()
    monkeypatch.setattr(main, "get_db", lambda: contextlib.nullcontext(db))
    main.app.dependency_overrides[main.get_current_user] = lambda: "u1"
    client = TestClient(main.app)
    yield client, db
    main.app.dependency_overrides.pop(main.get_current_user, None)


def test_outcome_is_recorded_for_the_caller(app_client):
    client, db = app_client
    r = client.post("/v1/scans/0b7f5a3c-9d1e-4f2a-8c6b-1e2d3f4a5b6c/outcome", json={"outcome": "removed"})
    assert r.status_code == 202 and r.json() == {"accepted": True}
    sql, params = db.queries[0]
    assert sql.startswith("INSERT INTO scan_outcomes")
    assert "WHERE scan_outcomes.user_id = EXCLUDED.user_id" in sql   # nobody overwrites another's
    assert params[:3] == ("0b7f5a3c-9d1e-4f2a-8c6b-1e2d3f4a5b6c", "u1", "removed")
    assert db.commits == 1


def test_outcome_rejects_what_it_does_not_know(app_client):
    client, db = app_client
    assert client.post("/v1/scans/0b7f5a3c-9d1e/outcome", json={"outcome": "kept"}).status_code == 422
    assert client.post("/v1/scans/x;drop/outcome", json={"outcome": "removed"}).json() == {"accepted": False}
    assert db.queries == []


def test_outcome_never_fails_the_app(app_client, monkeypatch):
    import main
    client, _ = app_client
    monkeypatch.setattr(main, "get_db", lambda: contextlib.nullcontext(FakeDB(fail=True)))
    r = client.post("/v1/scans/0b7f5a3c-9d1e-4f2a/outcome", json={"outcome": "confirmed"})
    assert r.status_code == 202 and r.json() == {"accepted": False}


def test_crm_report_reads_the_log_without_test_accounts(monkeypatch):
    import crm
    db = FakeDB(rows=[scan(opinion="disagree", second=other(), outcome="confirmed")])
    monkeypatch.setattr(crm, "get_db", lambda: contextlib.nullcontext(db))
    report = crm.scanner_report(days=9999, _=True)
    sql, params = db.queries[0]
    assert "LEFT JOIN scan_outcomes o ON o.scan_id = s.id AND o.user_id = s.user_id" in sql
    assert params[1] == crm.TEST_EMAIL_PATTERN and "!~*" in sql
    assert report["days"] == 365                       # clamped
    assert report["settled"] == 1 and report["capped"] is False
