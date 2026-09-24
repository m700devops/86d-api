"""Every CRM AI call goes through crm._claude(): one model and effort, the
refusal fallback, prompt caching, a plain retry on a 400, and a usage line.

Caching is the cost lever once everything runs on Opus 5: a repeat within five
minutes reads a cached prefix at a tenth of the input price. The AI bar sends
the whole book (15-25k tokens) every message, so the book goes in its own
cached block BEFORE the new message, and the system prompt is cached too.
"""
import sys
import types

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import assist  # noqa: E402
import crm  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def _ok(text='{"ok": true}', **usage):
    return _Resp(200, {"stop_reason": "end_turn", "model": "claude-opus-5",
                       "usage": usage, "content": [
                           {"type": "thinking", "thinking": ""},
                           {"type": "text", "text": text}]})


def _record(monkeypatch, *responses):
    import httpx
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"headers": headers, "json": json, "timeout": timeout})
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_the_system_prompt_is_cached_and_the_fallback_is_on(monkeypatch):
    calls = _record(monkeypatch, _ok())
    crm._ask_claude("SYSTEM", "notes", purpose="call-notes")
    body, headers = calls[0]["json"], calls[0]["headers"]
    assert body["system"] == [{"type": "text", "text": "SYSTEM",
                               "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": "notes"}]
    assert body["fallbacks"] == "default"
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert body["output_config"] == {"effort": "medium"}


def test_a_context_block_is_cached_ahead_of_what_changes(monkeypatch):
    calls = _record(monkeypatch, _ok())
    crm._claude_json("SYS", "NEW MESSAGE: hi", {"type": "object"}, context="THE BOOK")
    content = calls[0]["json"]["messages"][0]["content"]
    assert content == [{"type": "text", "text": "THE BOOK", "cache_control": {"type": "ephemeral"}},
                       {"type": "text", "text": "NEW MESSAGE: hi"}]


def test_the_plain_retry_strips_everything_that_could_be_refused(monkeypatch):
    calls = _record(monkeypatch, _Resp(400, {"error": "x"}), _ok())
    assert crm._claude_json("SYS", "msg", {"type": "object"}, context="BOOK") == {"ok": True}
    retry = calls[1]
    assert isinstance(retry["json"]["system"], str) and "JSON schema" in retry["json"]["system"]
    assert retry["json"]["messages"][0]["content"] == "BOOK\n\nmsg"
    for gone in ("output_config", "fallbacks"):
        assert gone not in retry["json"]
    assert "anthropic-beta" not in retry["headers"]


def test_every_call_logs_its_usage(monkeypatch, capsys):
    _record(monkeypatch, _ok(input_tokens=900, cache_read_input_tokens=20000,
                             cache_creation_input_tokens=0, output_tokens=150))
    crm._ask_claude("S", "u", purpose="prep-sheet")
    line = capsys.readouterr().out
    assert "AI_USAGE prep-sheet" in line and "cache_read=20000" in line and "out=150" in line


def test_a_reply_that_is_not_an_object_is_unreadable(monkeypatch):
    _record(monkeypatch, _ok('["a", "b"]'))
    with pytest.raises(HTTPException) as e:
        crm._ask_claude("S", "u")
    assert e.value.detail["error"] == "ai_unreadable"


def test_the_ai_bar_sends_the_book_as_the_cached_block():
    book, dates, log = "LEADS ...", "2026-09-25 Friday (today)", "TOUCHES ..."
    ctx = assist.context_block(book, dates, log)
    msg = assist.message_block("Olde Town said no", [{"you": "hi", "ai": "hello"}])
    assert ctx.startswith("DATES") and "LEADS" in ctx and "TOUCHES" in ctx
    assert "Olde Town" not in ctx                        # the volatile part stays out
    assert msg.endswith("NEW MESSAGE: Olde Town said no") and "Them: hi" in msg
    assert assist.user_message(book, dates, "x", [], log) == ctx + "\n\n" + assist.message_block("x", [])
