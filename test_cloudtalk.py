"""CloudTalk: calls in, transcripts read, a salesman score out — the pure parts.

Shapes are CloudTalk's documented ones (developers.cloudtalk.io, v1.7): call
history under responseData.data[].Cdr, transcription as data.segments[] with
data.callers[] saying which caller is the agent. The score is four parts of
0-25 (opener, discovery, objections, ask), totalled here, never by the model;
a call with no real conversation isn't scored at all.
"""
import sys
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import cloudtalk  # noqa: E402
import crm  # noqa: E402

HISTORY = {"responseData": {"itemsCount": 3, "pageCount": 1, "pageNumber": 1, "limit": 100, "data": [
    {"Cdr": {"id": 5001, "type": "outgoing", "public_external": "+14108744223",
             "started_at": "2026-09-28T19:58:01.000Z", "ended_at": "2026-09-28T20:01:31.000Z",
             "talking_time": 190, "billsec": 200, "recorded": True}},
    {"Cdr": {"id": 5002, "type": "outgoing", "public_external": "", "talking_time": 0}},
    {"Cdr": {"id": 5003, "type": "incoming", "public_external": "+1 (720) 242-9667",
             "started_at": "2026-09-28 21:00:00", "talking_time": 0}},
]}}
TRANSCRIPT = {"callId": 5001, "language": "en",
              "callers": [{"id": 5000, "type": "contact", "localIdentifier": "caller1"},
                          {"id": 1000, "type": "agent", "localIdentifier": "caller2"}],
              "segments": [
                  {"start": 1.5, "end": 3.0, "caller": "caller1", "text": "Hideaway, this is Mike."},
                  {"start": 3.2, "end": 9.0, "caller": "caller2",
                   "text": "Hi Mike, it's Stephan, I own 86'd. Jen said to try you at four."},
                  {"start": 9.5, "end": 12.0, "caller": "caller2", "text": "Got two minutes?"},
                  {"start": 12.5, "end": 20.0, "caller": "caller1",
                   "text": "We use MarginEdge and we're happy with it."},
              ]}


def test_call_history_is_read_and_junk_dropped():
    calls, pages = cloudtalk.parse_calls(HISTORY)
    assert pages == 1 and [c["call_id"] for c in calls] == ["5001", "5003"]
    first = calls[0]
    assert first["number"] == "4108744223" and first["talk_seconds"] == 190
    assert first["started_at"] == "2026-09-28T19:58:01+00:00"
    assert calls[1]["started_at"] == "2026-09-28T21:00:00+00:00"   # the space format, UTC


def test_the_transcript_reads_as_a_conversation():
    text = cloudtalk.transcript_text(TRANSCRIPT)
    assert text.splitlines() == [
        "Bar: Hideaway, this is Mike.",
        "Stephan: Hi Mike, it's Stephan, I own 86'd. Jen said to try you at four. Got two minutes?",
        "Bar: We use MarginEdge and we're happy with it."]
    assert cloudtalk.transcript_text({}) == ""


def test_the_score_is_totalled_here_and_the_moment_must_be_real():
    t = cloudtalk.transcript_text(TRANSCRIPT)
    got = cloudtalk.clean_score({"scorable": True, "opener": 22, "discovery": 9.6,
                                 "objections": 40, "ask": -3, "did_well": "Named Jen.",
                                 "fix": "Ask how they count before the pitch.",
                                 "moment": "We use MarginEdge and we're happy with it."}, t)
    assert got["parts"] == {"opener": 22, "discovery": 10, "objections": 25, "ask": 0}
    assert got["score"] == 57 and got["moment"].startswith("We use MarginEdge")
    made_up = cloudtalk.clean_score({"scorable": True, "opener": 10, "moment": "Sounds great!"}, t)
    assert made_up["moment"] == ""
    assert cloudtalk.clean_score({"scorable": False, "opener": 25}, t) is None


def test_the_score_line_the_lead_and_the_playbook_read():
    line = cloudtalk.score_line({"score": 57, "parts": {"opener": 22, "discovery": 10,
                                                        "objections": 25, "ask": 0},
                                 "did_well": "Named Jen.", "fix": "Make one ask."})
    assert line == ("Call score 57/100 (opener 22, discovery 10, objections 25, ask 0) · "
                    "Did well: Named Jen. · Next time: Make one ask.")
    assert cloudtalk.score_line(None) == ""


def test_nothing_runs_without_the_keys(monkeypatch):
    monkeypatch.delenv("CLOUDTALK_KEY_ID", raising=False)
    monkeypatch.delenv("CLOUDTALK_KEY_SECRET", raising=False)
    assert crm.process_cloudtalk() == {"skipped": "no CLOUDTALK_KEY_ID / CLOUDTALK_KEY_SECRET"}


def test_the_reader_is_asked_for_fields_and_the_score_together():
    schema = crm._call_read_schema()
    for key in ("outcome", "ask_for", "callback_time", "current_setup", "summary",
                "scorable", "opener", "discovery", "objections", "ask", "moment"):
        assert key in schema["required"]
    system = crm._call_read_system()
    assert "WORD FOR WORD" in system
    assert "0-25" in system and "Stephan" in system


def test_the_playbook_hears_the_scores():
    import playbook
    lines = playbook.scoreboard_lines({"dials": 40, "connects": 12, "call_scored": 6,
                                       "call_score_avg": 61,
                                       "call_parts": {"opener": 20, "discovery": 9,
                                                      "objections": 17, "ask": 15}})
    assert any("6 recorded conversations: average 61/100" in l and "weakest part is discovery" in l
               for l in lines)
