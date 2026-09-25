"""The second opinion: OpenAI and Gemini asked at the same time, one checking the
other (main._run_providers, main._decide, helpers.answers_agree).

Three layers, each tested on its own:
- answers_agree: do two readings describe the same bottle? Strict on purpose —
  a false "they disagree" costs one glance at a flagged row, a false "they
  agree" is today's unflagged guess.
- _decide: pure — what to answer with, and whether a new product may be made.
- _run_providers: the orchestration, with fake providers on real delays, so the
  fast path, the wait window, failures and cancellation are exercised for real.
No network, no database: the catalog lookup and the one write are stubbed.
"""
import asyncio
import json
import os
import sys
import time

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from helpers import answers_agree  # noqa: E402

IMAGE = "aGVsbG8="


# ─── answers_agree ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a, b", [
    (("Old No. 7", "Jack Daniel's"), ("Old No. 7 Tennessee Whiskey", "Jack Daniel's")),
    (("Old No.7", "Jack Daniels"), ("Old No. 7", "Jack Daniel's")),
    (("Red", "Johnnie Walker"), ("Red Label", "Johnnie Walker")),
    (("Tennessee Honey", "Jack Daniel's"), ("Honey", "Jack Daniel's")),
    (("12", "Glenfiddich"), ("12 Year Old", "Glenfiddich")),
    (("Original", "Grey Goose"), ("Grey Goose", "Grey Goose")),
    (("Silver", "Patrón"), ("Silver", "Patron")),
    (("Citroen", "Ketel One"), ("Citron", "Ketel One")),
    (("Original", "Crown Royal"), ("Original", "Crown Royal Canadian Whisky")),
    (("Handmade", "Tito's"), ("Handmade 750ml", "Titos")),
])
def test_same_bottle_different_wording_agrees(a, b):
    assert answers_agree(*a, *b) and answers_agree(*b, *a)


@pytest.mark.parametrize("a, b", [
    (("Red Label", "Johnnie Walker"), ("Black Label", "Johnnie Walker")),
    (("Bud Light", "Budweiser"), ("Bud Light Lime", "Budweiser")),   # a variant that extends the name
    (("12", "Glenfiddich"), ("15", "Glenfiddich")),
    (("Original", "Grey Goose"), ("Le Citron", "Grey Goose")),
    (("Grey Goose", "Grey Goose"), ("Le Citron", "Grey Goose")),     # a bare brand is the base product
    (("Bourbon", "Bulleit"), ("Rye", "Bulleit")),
    (("Original", "Bulleit"), ("Rye", "Bulleit")),
    (("Citron", "Absolut"), ("Mandrin", "Absolut")),
    (("VS", "Hennessy"), ("VSOP", "Hennessy")),
    (("Handmade 1L", "Tito's"), ("Handmade 750ml", "Tito's")),        # different sizes
    (("Blue Bolt", "Gatorade"), ("Glacier Freeze", "Gatorade")),
    (("Original", "Maker's Mark"), ("46", "Maker's Mark")),
])
def test_different_bottles_disagree(a, b):
    assert not answers_agree(*a, *b) and not answers_agree(*b, *a)


# ─── _decide ─────────────────────────────────────────────────────────────────

def ans(provider="openai", name="Old No. 7", brand="Jack Daniel's", status="ok", supported=True,
        product=None, method="none", confidence=0.92):
    result = {"name": name, "brand": brand, "confidence": confidence, "label_text": f"{brand} {name}"}
    return main._Answer(provider, provider, result, status=status, label_supported=supported,
                        product_id=product, method=method if product or method == "unreadable" else "none")


def test_one_answer_is_used_exactly_as_before():
    only = ans()
    d = main._decide([only])
    assert d["chosen"] is only and d["other"] is None
    assert (d["opinion"], d["allow_create"], d["confirm"]) == ("none", True, False)


def test_agreement_on_a_new_bottle_may_create_it():
    a, b = ans("openai", "Tennessee Honey"), ans("gemini", "Honey")
    d = main._decide([a, b])
    assert (d["opinion"], d["allow_create"], d["confirm"]) == ("agree", True, False)
    assert d["chosen"] is a  # a tie goes to the primary


def test_the_same_product_is_agreement_whatever_the_wording():
    a = ans("openai", "Honey", product="p1", method="match_key")
    b = ans("gemini", "Something Else", product="p1", method="exact")
    assert main._decide([a, b])["opinion"] == "agree"


def test_disagreement_is_flagged_and_never_creates():
    a, b = ans("openai", "Red Label", "Johnnie Walker"), ans("gemini", "Black Label", "Johnnie Walker")
    d = main._decide([a, b])
    assert (d["opinion"], d["allow_create"], d["confirm"]) == ("disagree", False, True)
    assert d["chosen"] is a and d["other"] is b


def test_disagreement_prefers_label_evidence_then_the_bars_own_bottle():
    unsupported = ans("openai", "Red Label", "Johnnie Walker", supported=None)
    supported = ans("gemini", "Black Label", "Johnnie Walker", supported=True)
    assert main._decide([unsupported, supported])["chosen"] is supported
    global_match = ans("openai", "Red Label", "Johnnie Walker", product="p-red", method="match_key")
    bar_bottle = ans("gemini", "Black Label", "Johnnie Walker", product="p-black", method="bar_book")
    assert main._decide([global_match, bar_bottle])["chosen"] is bar_bottle


def test_one_readable_answer_is_used_but_creates_nothing():
    readable, unreadable = ans("gemini"), ans("openai", status="unreadable", method="unreadable")
    d = main._decide([unreadable, readable])
    assert d["chosen"] is readable and d["other"] is unreadable
    assert (d["opinion"], d["allow_create"], d["confirm"]) == ("other_unreadable", False, False)


def test_neither_readable():
    unreadable = ans("openai", status="unreadable", method="unreadable")
    no_bottle = ans("gemini", name="", brand="", status="no_bottle")
    assert main._decide([no_bottle, unreadable])["chosen"] is unreadable   # a retake beats "no bottle"
    both_empty = main._decide([no_bottle, ans("openai", name="", brand="", status="no_bottle")])
    assert both_empty["chosen"].status == "no_bottle" and not both_empty["allow_create"]


def test_strong_needs_the_bars_own_bottle_and_label_evidence():
    assert ans(product="p", method="bar_book", supported=True).strong
    assert not ans(product="p", method="match_key", supported=True).strong   # someone else's catalog row
    assert not ans(product="p", method="bar_book", supported=None).strong    # no reading to check it by
    assert not ans(product="p", method="bar_book", status="label_unsupported").strong


def test_label_is_what_the_app_shows():
    assert ans(name="Black Label", brand="Johnnie Walker").label() == "Johnnie Walker Black Label"
    assert ans(name="Original", brand="Grey Goose").label() == "Grey Goose"


# ─── _run_providers, with fake providers on real delays ──────────────────────

def reading(name, brand, confidence=0.92, label=None):
    return json.dumps({"label_text": label if label is not None else f"{brand} {name}".upper(),
                       "name": name, "brand": brand, "category": "spirits",
                       "product_type": "", "confidence": confidence})


@pytest.fixture
def providers(monkeypatch):
    """providers(openai=(delay, reply-or-exception), gemini=(...)) installs fakes;
    returns what happened to each."""
    seen = {"calls": [], "cancelled": []}

    def install(openai=None, gemini=None):
        def fake(name, delay, outcome):
            async def call(key, prompt, image, stats):
                seen["calls"].append(name)
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    seen["cancelled"].append(name)
                    raise
                if isinstance(outcome, BaseException):
                    raise outcome
                stats["input_tokens"] = 100
                return outcome
            return call
        if openai:
            monkeypatch.setattr(main, "_call_openai", fake("openai", *openai))
        if gemini:
            monkeypatch.setattr(main, "_call_gemini", fake("gemini", *gemini))
        return seen
    return install


@pytest.fixture
def catalog(monkeypatch):
    """catalog({(brand, name): (product_id, method)}) stubs the lookup; returns
    the writes _record_match was asked to make."""
    writes = []

    def install(table=None):
        table = table or {}
        seen_locations = []

        def find(result, user, location=None):
            seen_locations.append(location)
            return table.get((result["brand"], result["name"]), (None, "none"))

        def record(result, user, product_id, method, allow_create=True):
            writes.append({"name": result["name"], "product_id": product_id, "allow_create": allow_create})
            if product_id:
                return (product_id, False, method)
            if allow_create:
                return (f"new-{result['name']}", True, "auto_created")
            return (None, False, "none")

        monkeypatch.setattr(main, "_find_product", find)
        monkeypatch.setattr(main, "_record_match", record)
        writes.clear()
        install.locations = seen_locations
        return writes
    return install


def run(event=None, keys=("sk", "g")):
    """Run _run_providers; returns (response or HTTPException, seconds, event)."""
    event = event if event is not None else {"id": "scan-1", "location_id": "loc-1"}
    request = main.ScanAnalyzeRequest(image=IMAGE, location_id="loc-1")

    async def go():
        started = time.monotonic()
        try:
            response = await main._run_providers(keys[0], keys[1], main.BOTTLE_PROMPT, request, "u1", event)
        except HTTPException as e:
            response = e
        elapsed = time.monotonic() - started
        pending = event.pop("_pending", None)
        chosen = event.pop("_chosen", None)
        if pending is not None:  # what _log_scan does after the reply has gone
            await main._finish_second_opinion(event, pending, chosen)
        return response, elapsed

    response, elapsed = asyncio.run(go())
    return response, elapsed, event


@pytest.fixture(autouse=True)
def _quiet_log(monkeypatch):
    monkeypatch.setattr(main, "_record_scan_event", lambda event: None)
    monkeypatch.setattr(main, "SECOND_OPINION", "on")


def test_fast_path_answers_before_the_slow_provider_and_logs_its_verdict(providers, catalog):
    providers(openai=(1.0, reading("Old No. 7", "Jack Daniel's")),
              gemini=(0.05, reading("Old No. 7", "Jack Daniel's")))
    writes = catalog({("Jack Daniel's", "Old No. 7"): ("p-jd", "bar_book")})
    response, elapsed, event = run()
    assert elapsed < 0.6                                   # didn't wait for the slow one
    assert (response.matched_product_id, response.match_method) == ("p-jd", "bar_book")
    assert (event["path"], event["provider"]) == ("fast", "gemini")
    assert event["second_opinion"] == "agree"              # logged once the slow one landed
    assert json.loads(event["second_answer"])["provider"] == "openai"
    assert writes == [{"name": "Old No. 7", "product_id": "p-jd", "allow_create": True}]  # counted once


def test_fast_path_contradicted_later_is_recorded(providers, catalog):
    providers(openai=(0.4, reading("Gentleman Jack", "Jack Daniel's")),
              gemini=(0.05, reading("Old No. 7", "Jack Daniel's")))
    catalog({("Jack Daniel's", "Old No. 7"): ("p-jd", "bar_book")})
    response, _, event = run()
    assert response.matched_product_id == "p-jd"
    assert event["second_opinion"] == "disagree"


def test_both_agree_on_a_new_bottle_and_it_is_created(providers, catalog):
    providers(openai=(0.05, reading("Tennessee Honey", "Jack Daniel's")),
              gemini=(0.1, reading("Honey", "Jack Daniel's")))
    writes = catalog()
    response, _, event = run()
    assert (response.is_new_product, response.needs_confirmation) == (True, False)
    assert (event["path"], event["second_opinion"]) == ("both", "agree")
    assert writes[0]["allow_create"] is True


def test_disagreement_is_counted_but_flagged_for_a_check(providers, catalog):
    providers(openai=(0.05, reading("Red Label", "Johnnie Walker")),
              gemini=(0.1, reading("Black Label", "Johnnie Walker")))
    writes = catalog({("Johnnie Walker", "Red Label"): ("p-red", "match_key"),
                      ("Johnnie Walker", "Black Label"): ("p-black", "match_key")})
    response, _, event = run()
    assert response.matched_product_id == "p-red"          # the primary, all else equal
    assert (response.needs_confirmation, response.alternative) == (True, "Johnnie Walker Black Label")
    assert event["second_opinion"] == "disagree"
    assert writes == [{"name": "Red Label", "product_id": "p-red", "allow_create": False}]


def test_disagreement_never_creates_a_product(providers, catalog):
    providers(openai=(0.05, reading("Red Label", "Johnnie Walker")),
              gemini=(0.1, reading("Black Label", "Johnnie Walker")))
    catalog()
    response, _, _ = run()
    assert (response.matched_product_id, response.is_new_product, response.needs_confirmation) == (None, False, True)


def test_the_wait_window_bounds_what_the_comparison_costs(providers, catalog, monkeypatch):
    monkeypatch.setattr(main, "SECOND_OPINION_WAIT_SEC", 0.1)
    providers(openai=(0.05, reading("Red Label", "Johnnie Walker")),
              gemini=(0.8, reading("Red Label", "Johnnie Walker")))
    catalog({("Johnnie Walker", "Red Label"): ("p-red", "match_key")})
    response, elapsed, event = run()
    assert elapsed < 0.5
    assert (response.matched_product_id, event["path"]) == ("p-red", "window")
    assert event["second_opinion"] == "agree"              # the late one, logged afterwards


def test_an_unreadable_first_answer_waits_for_the_other(providers, catalog, monkeypatch):
    monkeypatch.setattr(main, "SECOND_OPINION_WAIT_SEC", 0.05)
    providers(openai=(0.02, reading("Sports Drink", "Gatorade", confidence=0.4)),
              gemini=(0.3, reading("Blue Bolt", "Gatorade")))
    writes = catalog()
    response, elapsed, event = run()
    assert elapsed >= 0.3                                  # no window until something is readable
    assert (event["provider"], event["second_opinion"]) == ("gemini", "other_unreadable")
    assert writes[0]["allow_create"] is False              # one model couldn't read it: no new product
    assert response.matched_product_id is None


def test_a_failed_provider_leaves_the_other_to_answer_alone(providers, catalog):
    providers(openai=(0.1, reading("Blue Bolt", "Gatorade")), gemini=(0.02, RuntimeError("boom")))
    writes = catalog()
    response, _, event = run()
    assert (event["path"], event["fallback_from"]) == ("single", "gemini:RuntimeError")
    assert writes[0]["allow_create"] is True               # no second opinion to be had
    assert response.is_new_product is True


def test_a_rejected_openai_key_no_longer_fails_the_scan(providers, catalog):
    import httpx2
    rejected = main.openai.AuthenticationError(
        "bad key", response=httpx2.Response(401, request=httpx2.Request("POST", "https://api.openai.com/v1")),
        body=None)
    providers(openai=(0.01, rejected), gemini=(0.05, reading("Blue Bolt", "Gatorade")))
    catalog({("Gatorade", "Blue Bolt"): ("p-bb", "exact")})
    response, _, event = run()
    assert (response.matched_product_id, event["provider"]) == ("p-bb", "gemini")


def test_no_answer_at_all(providers, catalog):
    catalog()
    providers(openai=(0.01, asyncio.TimeoutError()), gemini=(0.01, asyncio.TimeoutError()))
    response, _, event = run()
    assert isinstance(response, HTTPException) and response.status_code == 504
    assert event["status"] == "timeout"
    providers(openai=(0.01, asyncio.TimeoutError()), gemini=(0.01, RuntimeError("boom")))
    response, _, _ = run()
    assert isinstance(response, HTTPException) and response.status_code == 502


def test_second_opinion_off_is_the_old_order(providers, catalog, monkeypatch):
    monkeypatch.setattr(main, "SECOND_OPINION", "off")
    catalog()
    seen = providers(openai=(0.01, reading("Blue Bolt", "Gatorade")), gemini=(0.01, reading("Blue Bolt", "Gatorade")))
    run()
    assert seen["calls"] == ["openai"]                      # Gemini only if OpenAI fails
    seen["calls"].clear()
    providers(openai=(0.01, RuntimeError("down")), gemini=(0.01, reading("Blue Bolt", "Gatorade")))
    response, _, event = run()
    assert seen["calls"] == ["openai", "gemini"] and event["provider"] == "gemini"


def test_the_total_cap_cancels_both_providers(providers, catalog, monkeypatch):
    catalog()
    seen = providers(openai=(5, reading("x", "y")), gemini=(5, reading("x", "y")))
    monkeypatch.setattr(main, "_scan_context",
                        lambda user: ({"subscription_status": "active", "trial_ends_at": None}, None))
    monkeypatch.setattr(main, "TOTAL_SCAN_TIMEOUT_SEC", 0.1)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("GEMINI_API_KEY", "g")

    async def scan():
        with pytest.raises(HTTPException) as err:
            await main.analyze_bottle(main.ScanAnalyzeRequest(image=IMAGE), "u1")
        await asyncio.sleep(0.05)
        # Checked INSIDE the loop: asyncio.run cancels leftovers at shutdown, which
        # would hide providers the scan itself left running.
        return err.value, sorted(seen["cancelled"])

    err, cancelled = asyncio.run(scan())
    assert err.status_code == 504
    assert cancelled == ["gemini", "openai"]   # nothing left running, nothing billed on


def test_an_old_app_build_gets_its_only_bar(providers, catalog, monkeypatch):
    providers(openai=(0.05, reading("Blue Bolt", "Gatorade")), gemini=(0.05, reading("Blue Bolt", "Gatorade")))
    catalog()
    monkeypatch.setattr(main, "_scan_context",
                        lambda user: ({"subscription_status": "active", "trial_ends_at": None}, "the-only-bar"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("GEMINI_API_KEY", "g")

    async def scan():
        return await main.analyze_bottle(main.ScanAnalyzeRequest(image=IMAGE), "u1")  # no location_id

    asyncio.run(scan())
    assert set(catalog.locations) == {"the-only-bar"}
