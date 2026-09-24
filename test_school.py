"""School refresh: scheduling, parsing and validation. No network, no database."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import school

MNL = ZoneInfo("Asia/Manila")


def at(d, h, m=0):
    return datetime(2026, 9, d, h, m, tzinfo=MNL)


def test_not_before_10am_manila():
    assert not school.is_due(at(24, 9, 59), None)
    assert school.is_due(at(24, 10, 0), None)


def test_every_three_days_with_slack():
    last = at(24, 10, 5)
    assert not school.is_due(at(26, 23, 0), last)
    assert school.is_due(at(27, 10, 0), last)          # 10:05 run, next cycle at 10:00 still fires
    late = at(24, 15, 0)                               # service was asleep until 3pm
    assert not school.is_due(at(27, 10, 0), late)
    assert school.is_due(at(27, 13, 0), late)


def test_next_run_is_10am():
    n = school.next_run(at(24, 12), at(24, 10, 5))
    assert (n.day, n.hour) == (27, 10)


def test_queries_rotate_and_cover_every_step():
    a, b = school.queries_for(0), school.queries_for(1)
    assert {s for s, _ in a} == {0, 1, 2, 3, 4, 5, "x"}
    assert a != b


def test_lengths():
    assert school.parse_len("9:39") == 579 and school.parse_len("1:02:03") == 3723
    assert school.parse_len("LIVE") is None
    assert school.parse_iso_duration("PT12M5S") == 725 and school.parse_iso_duration("bad") is None


def test_filter_drops_long_short_dupes_and_recent():
    c = [{"id": "a", "secs": 600}, {"id": "a", "secs": 600}, {"id": "b", "secs": 100},
         {"id": "c", "secs": 30 * 60}, {"id": "d", "secs": 400}]
    assert [x["id"] for x in school.filter_candidates(c, {"d"})] == ["a"]


def test_verdicts_must_be_real_relevant_and_explained():
    by = {"a": {"id": "a", "title": "t", "channel": "c", "secs": 600}}
    raw = [{"id": "a", "relevance": 8, "step": 3, "why": "w", "steal": "s"},
           {"id": "zzz", "relevance": 9, "step": 1, "why": "w", "steal": "s"},
           {"id": "a", "relevance": 4, "step": 1, "why": "w", "steal": "s"}]
    out = school.validate_verdicts(raw, by)
    assert len(out) == 1 and out[0]["step"] == 3 and out[0]["len"] == "10:00"
    assert school.validate_verdicts("nope", by) == []


def test_assemble_prefers_relevance_then_ten_minutes():
    vs = [{"id": str(i), "step": 0, "relevance": r, "secs": s} for i, (r, s) in
          enumerate([(9, 1200), (9, 590), (7, 600), (10, 300)])]
    out = school.assemble(vs)
    assert [v["id"] for v in out["steps"]["0"]] == ["3", "1"]
    assert all(v["secs"] <= 630 for v in out["daily"])


def test_generated_content_is_validated():
    g = school.validate_gauntlet([{"who": "x", "line": "l", "options": ["a", "b", "c"], "best": 2, "why": "w"},
                                  {"line": "l", "options": ["a", "b"], "best": 0},
                                  {"line": "l", "options": ["a", "b", "c"], "best": 5}])
    assert g == [["x", "l", ["a", "b", "c"], 2, "w"]]
    q = school.validate_quiz([{"question": "q", "options": ["a", "b", "c", "d"], "answer": 1, "why": "w"},
                              {"question": "q", "options": ["a", "b", "c", "d"], "answer": "x"}])
    assert len(q) == 1


def test_pack_usable_threshold():
    assert not school.pack_is_usable({"videos": {"steps": {"0": [1]}, "daily": [1, 2, 3]}})
    assert school.pack_is_usable({"videos": {"steps": {str(i): [1] for i in range(4)}, "daily": [1, 2, 3]}})


def test_build_pack_survives_ai_failure(monkeypatch):
    monkeypatch.setattr(school, "search_youtube", lambda q: [{"id": q[:11].ljust(11, "_"), "title": q, "channel": "c", "secs": 600, "embed_checked": True}])
    def ask(*a):
        raise RuntimeError("AI down")
    p = school.build_pack(0, set(), ask)
    assert p["stats"]["candidates"] > 0 and p["stats"]["kept"] == 0 and not school.pack_is_usable(p)
