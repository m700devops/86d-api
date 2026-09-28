"""The AI bar remembers: what was said (a week, across devices) and what it's told to keep.

It used to keep the last four exchanges in one browser tab — a new tab, the
other box or tomorrow started from nothing — and "remember I don't call on
Mondays" had nowhere to go. Now every exchange is logged server-side and read
back into the next message, and "remember…" is added to the owner's standing
instructions (AI Brain), which the bar itself now reads too.
"""
import sys
import types
from contextlib import contextmanager

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

import assist  # noqa: E402
import crm  # noqa: E402


def _bar(monkeypatch, out, memory=()):
    class Cur:
        def execute(self, *a): pass
        def fetchall(self): return []
        def fetchone(self): return None

    @contextmanager
    def db():
        yield types.SimpleNamespace(cursor=lambda: Cur(), commit=lambda: None)

    sent, logged, kept = {}, [], []
    monkeypatch.setattr(crm, "get_db", db)
    monkeypatch.setattr(crm, "_touch_counts", lambda cur, ids: {})
    monkeypatch.setattr(crm, "_claude_json", lambda system, user, schema, **k:
                        sent.update(user=user, context=k.get("context")) or dict(out))
    monkeypatch.setattr(crm, "_apply_proposed", lambda *a, **k: ([], []))
    monkeypatch.setattr(crm, "_bar_memory", lambda: list(memory))
    monkeypatch.setattr(crm, "_knowledge", lambda playbook=True: "OWNER SAYS: never call on Mondays")
    monkeypatch.setattr(crm, "_log_bar_exchange", lambda you, ai, leads: logged.append((you, ai)))
    monkeypatch.setattr(crm, "_remember", lambda text, today: kept.append(text))
    return sent, logged, kept


OUT = {"reply": "Noted.", "question": None, "changes": [], "new_leads": [], "research": [],
       "remember": ""}


def test_what_was_said_before_is_read_back_in(monkeypatch):
    sent, logged, _ = _bar(monkeypatch, OUT, memory=[
        {"when": "Mon 28 Sep 10:15pm", "you": "Libbey's said they use MarginEdge", "ai": "Saved."}])
    crm.assist_update(crm.AssistRequest(text="what did Libbey's say they use?"))
    assert "Libbey's said they use MarginEdge" in sent["user"]
    assert "[Mon 28 Sep 10:15pm] Them:" in sent["user"]
    assert "OWNER SAYS: never call on Mondays" in sent["context"]     # the bar reads the brain
    assert logged == [("what did Libbey's say they use?", "Noted.")]


def test_the_pages_own_history_is_only_the_fallback(monkeypatch):
    sent, _, _ = _bar(monkeypatch, OUT, memory=[])
    crm.assist_update(crm.AssistRequest(text="and her?", history=[
        crm.AssistTurn(you="Laura at Barrel House", ai="ok")]))
    assert "Laura at Barrel House" in sent["user"]


def test_remember_goes_to_the_brain_in_their_words(monkeypatch):
    text = "from now on, never call a bar on a Monday, they're closed"
    _, _, kept = _bar(monkeypatch, {**OUT, "remember": "never call a bar on a Monday"})
    got = crm.assist_update(crm.AssistRequest(text=text))
    assert kept == ["never call a bar on a Monday"]
    assert "I'll remember that" in got["reply"]
    # A paraphrase isn't kept as theirs: the whole message is saved instead.
    _, _, kept = _bar(monkeypatch, {**OUT, "remember": "skip Mondays"})
    crm.assist_update(crm.AssistRequest(text=text))
    assert kept == [text]


def test_nothing_is_remembered_unless_asked(monkeypatch):
    _, _, kept = _bar(monkeypatch, OUT)
    crm.assist_update(crm.AssistRequest(text="Olde Town said no"))
    assert kept == []


def test_the_bar_is_told_how_memory_works():
    assert "remember" in assist.BAR_SCHEMA["required"]
    assert "remember" not in assist.SCHEMA["properties"]      # never from a stranger's email
    assert "kept for a week across devices" in assist.BAR_SYSTEM
