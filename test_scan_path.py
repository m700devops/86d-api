"""The bottle-scan path: the request each provider gets, what happens to the
answer, and how failures fall through.

Most of these run the real OpenAI SDK against a local fake server, because the
bugs they guard against lived in the request itself: the image sent before the
instructions (so nothing could be cached), no temperature (so every scan of one
bottle could be phrased differently), a new client per scan (so the warm-up
warmed nothing), and SDK retries eating the 9-second window before Gemini ran.
No network and no database: product matching and the scan log are stubbed.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
# Other test files stub `database` with only get_db; main also imports init_db.
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

GOOD = {"name": "Old No. 7", "brand": "Jack Daniel's", "category": "spirits",
        "product_type": "Tennessee Whiskey", "confidence": 0.92}
IMAGE = "aGVsbG8="  # base64 "hello" — the fake server never looks at it


@pytest.fixture(autouse=True)
def _fresh_clients():
    # A client's connections belong to the event loop that opened them, and each
    # test runs its own loop.
    main._openai_clients.clear()
    main._gemini_clients.clear()
    main._openai_plain_models.clear()
    main._gemini_plain_models.clear()
    yield
    main._openai_clients.clear()
    main._gemini_clients.clear()
    main._openai_plain_models.clear()
    main._gemini_plain_models.clear()


# ─── a fake OpenAI ────────────────────────────────────────────────────────────

class FakeOpenAI:
    """Records every request; `reject` decides which bodies get a 400."""

    def __init__(self, reject=lambda body: False):
        self.requests = []
        self.reject = reject
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                model = self.path.rsplit("/", 1)[-1]
                self._send(200, {"id": model, "object": "model", "created": 0, "owned_by": "openai"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                fake.requests.append(("POST", self.path, body))
                if fake.reject(body):
                    self._send(400, {"error": {"message": "Invalid parameter", "type": "invalid_request_error",
                                               "param": None, "code": None}})
                    return
                self._send(200, {
                    "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": body["model"],
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": json.dumps(GOOD)}}],
                    "usage": {"prompt_tokens": 3400, "completion_tokens": 40, "total_tokens": 3440,
                              "prompt_tokens_details": {"cached_tokens": 2560}},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self):
        return [body for method, _, body in self.requests if method == "POST"]


@pytest.fixture
def fake_openai(monkeypatch):
    servers = []

    def start(**kwargs):
        server = FakeOpenAI(**kwargs)
        servers.append(server)
        monkeypatch.setenv("OPENAI_BASE_URL", server.url)
        return server

    yield start
    for server in servers:
        server.server.shutdown()


# ─── the request ──────────────────────────────────────────────────────────────

def test_instructions_go_first_and_the_image_last():
    kwargs = main._openai_request("gpt-4o", main.BOTTLE_PROMPT, IMAGE)
    system, user = kwargs["messages"]
    assert system == {"role": "system", "content": main.BOTTLE_PROMPT}
    assert user["content"][0]["type"] == "image_url"
    assert user["content"][0]["image_url"]["detail"] == "high"
    assert user["content"][-1] == {"type": "text", "text": main.SCAN_USER_TEXT}


def test_answers_are_deterministic_and_schema_shaped():
    kwargs = main._openai_request("gpt-4o", main.BOTTLE_PROMPT, IMAGE)
    assert kwargs["temperature"] == 0
    fmt = kwargs["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["properties"]["category"]["enum"] == main.SCAN_CATEGORIES
    assert "max_tokens" not in kwargs and kwargs["max_completion_tokens"] == 300


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "o4-mini", "o3"])
def test_reasoning_models_get_parameters_they_accept(model):
    # They reject max_tokens and any temperature but the default; sending either
    # made every OpenAI call 400 and every scan fall through to Gemini.
    for plain in (False, True):
        kwargs = main._openai_request(model, main.BOTTLE_PROMPT, IMAGE, plain=plain)
        assert "max_tokens" not in kwargs
        assert "temperature" not in kwargs
        assert kwargs["max_completion_tokens"] >= 1000


def test_plain_request_is_the_request_that_ran_before():
    kwargs = main._openai_request("gpt-4o", main.BOTTLE_PROMPT, IMAGE, plain=True)
    assert kwargs == {
        "model": "gpt-4o",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{IMAGE}", "detail": "high"}},
            {"type": "text", "text": main.BOTTLE_PROMPT},
        ]}],
    }


def test_prompt_says_which_bottle_on_a_crowded_shelf():
    assert "nearest the centre of the photo" in main.BOTTLE_PROMPT
    assert "centre" in main.SCAN_USER_TEXT


# ─── the real SDK against the fake server ─────────────────────────────────────

def test_sdk_sends_the_structured_request(fake_openai):
    server = fake_openai()
    stats = {}
    text = asyncio.run(main._call_openai("sk-test", main.BOTTLE_PROMPT, IMAGE, stats))
    assert json.loads(text) == GOOD
    [body] = server.posts()
    assert body["messages"][0]["role"] == "system"
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    assert stats == {"input_tokens": 3400, "cached_tokens": 2560, "output_tokens": 40,
                     "thinking_tokens": None}                # gpt-4o doesn't reason


def test_rejected_structured_request_falls_back_to_plain_and_remembers(fake_openai):
    server = fake_openai(reject=lambda body: "response_format" in body)

    async def two_scans():
        first = await main._call_openai("sk-test", main.BOTTLE_PROMPT, IMAGE)
        second = await main._call_openai("sk-test", main.BOTTLE_PROMPT, IMAGE)
        return first, second

    first, second = asyncio.run(two_scans())
    assert json.loads(first) == json.loads(second) == GOOD
    # Structured (400), plain (200), then straight to plain for the second scan.
    assert ["response_format" in body for body in server.posts()] == [True, False, False]
    assert main.OPENAI_MODEL in main._openai_plain_models


def test_a_bad_photo_does_not_switch_structured_output_off(fake_openai):
    fake_openai(reject=lambda body: True)  # every request 400s: the input is the problem
    with pytest.raises(main.openai.BadRequestError):
        asyncio.run(main._call_openai("sk-test", main.BOTTLE_PROMPT, IMAGE))
    assert main.OPENAI_MODEL not in main._openai_plain_models


def test_sdk_does_not_retry_on_its_own(fake_openai):
    server = fake_openai(reject=lambda body: True)
    with pytest.raises(main.openai.BadRequestError):
        asyncio.run(main._call_openai("sk-test", main.BOTTLE_PROMPT, IMAGE))
    assert len(server.posts()) == 2  # structured + plain, no SDK retries on top
    assert main._openai_client("sk-test").max_retries == 0


def test_one_shared_client_with_long_keepalive():
    client = main._openai_client("sk-test")
    assert main._openai_client("sk-test") is client
    pool = client._client._transport._pool
    assert pool._keepalive_expiry == main.AI_KEEPALIVE_SECONDS >= 60


def test_warmup_warms_the_client_scans_use(fake_openai, monkeypatch):
    server = fake_openai()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-warm")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    warmed = asyncio.run(main._warm_providers())
    assert warmed["openai"] is True
    assert server.requests == [("GET", f"/v1/models/{main.OPENAI_MODEL}", None)]  # no tokens spent
    assert "sk-warm" in main._openai_clients


# ─── Gemini (its REST API, called directly) ───────────────────────────────────

GEMINI_USAGE = {"promptTokenCount": 4950, "cachedContentTokenCount": 3800,
                "candidatesTokenCount": 70, "thoughtsTokenCount": 180, "totalTokenCount": 5200}


class FakeGemini:
    """A local stand-in for Gemini's REST API. Records every request (method,
    path, API-key header, body). `reject` decides which bodies get a 400,
    `status` forces an error on every POST, `delay` stalls the reply."""

    def __init__(self, reject=lambda body: False, status=200, delay=0.0, missing=False, reply=None):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                data = json.dumps(payload).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass                     # the caller gave up, as it should

            def do_GET(self):
                fake.requests.append(("GET", self.path, self.headers.get("x-goog-api-key"), None))
                if missing:
                    self._send(404, {"error": {"code": 404, "message": "model not found", "status": "NOT_FOUND"}})
                else:
                    self._send(200, {"name": "models/" + self.path.rsplit("/", 1)[-1]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                fake.requests.append(("POST", self.path, self.headers.get("x-goog-api-key"), body))
                if delay:
                    import time as _t
                    _t.sleep(delay)
                if status != 200:
                    self._send(status, {"error": {"code": status, "message": "unavailable", "status": "UNAVAILABLE"}})
                elif reject(body):
                    self._send(400, {"error": {"code": 400, "message": "Invalid JSON payload", "status": "INVALID_ARGUMENT"}})
                else:
                    self._send(200, reply if reply is not None else {
                        "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(GOOD)}]},
                                        "finishReason": "STOP"}],
                        "usageMetadata": GEMINI_USAGE})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1beta"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


@pytest.fixture
def fake_gemini(monkeypatch):
    servers = []

    def start(**kwargs):
        server = FakeGemini(**kwargs)
        servers.append(server)
        monkeypatch.setattr(main, "GEMINI_API_BASE", server.url)
        return server

    yield start
    for server in servers:
        server.server.shutdown()


def _gemini(stats=None):
    return asyncio.run(main._call_gemini("g-key", main.BOTTLE_PROMPT, IMAGE, stats))


def test_gemini_reads_the_instructions_first_and_thinks_low(fake_gemini):
    server = fake_gemini()
    stats = {}
    assert json.loads(_gemini(stats)) == GOOD
    [(method, path, key, body)] = server.requests
    assert (method, path) == ("POST", f"/v1beta/models/{main.GEMINI_MODEL}:generateContent")
    assert key == "g-key" and "key=" not in path            # in a header, never in a URL or a log
    text, image = body["contents"][0]["parts"]
    assert text == {"text": main.BOTTLE_PROMPT}            # the same text first on every scan: cacheable
    assert image == {"inlineData": {"mimeType": "image/jpeg", "data": IMAGE}}
    assert body["generationConfig"] == {"responseMimeType": "application/json",
                                        "thinkingConfig": {"thinkingLevel": "LOW"}}
    assert "temperature" not in body["generationConfig"]   # Google: keep Gemini 3 at its default
    # What Google bills: the answer AND the thinking (the old SDK logged the answer alone)
    assert stats == {"input_tokens": 4950, "cached_tokens": 3800, "output_tokens": 250, "thinking_tokens": 180}


@pytest.mark.parametrize("setting, sent", [("MINIMAL", {"thinkingLevel": "MINIMAL"}),
                                           ("HIGH", {"thinkingLevel": "HIGH"}), ("DEFAULT", None)])
def test_the_thinking_level_is_a_setting(fake_gemini, monkeypatch, setting, sent):
    server = fake_gemini()
    monkeypatch.setattr(main, "GEMINI_THINKING", setting)
    _gemini()
    assert server.posts()[0][3]["generationConfig"].get("thinkingConfig") == sent


def test_a_refused_full_request_falls_back_to_plain_and_is_remembered(fake_gemini):
    server = fake_gemini(reject=lambda body: "generationConfig" in body)
    assert json.loads(_gemini()) == GOOD
    full, plain = server.posts()
    assert "generationConfig" in full[3] and "generationConfig" not in plain[3]
    assert main.GEMINI_MODEL in main._gemini_plain_models
    _gemini()
    assert len(server.posts()) == 3                          # straight to plain from then on


def test_a_bad_photo_does_not_switch_gemini_to_plain(fake_gemini):
    fake_gemini(reject=lambda body: True)                    # every request refused
    with pytest.raises(main.GeminiError):
        _gemini()
    assert main.GEMINI_MODEL not in main._gemini_plain_models


def test_a_gemini_error_is_not_retried(fake_gemini):
    server = fake_gemini(status=503)
    with pytest.raises(main.GeminiError) as err:
        _gemini()
    assert len(server.posts()) == 1                           # the other provider is the retry
    assert main._failure_label(err.value) == "http_503"
    import httpx
    assert main._failure_label(httpx.ReadTimeout("slow")) == "timeout"   # both time limits say timeout


def test_a_slow_gemini_is_cut_off_at_the_time_limit(fake_gemini, monkeypatch):
    """The old SDK call ran on a thread that could only be abandoned, not
    stopped; this is an async request the time limit cancels."""
    import time
    fake_gemini(delay=3)
    monkeypatch.setattr(main, "PROVIDER_TIMEOUT", 0.3)
    started = time.monotonic()
    with pytest.raises(BaseException) as err:
        _gemini()
    assert time.monotonic() - started < 1.5
    assert main._failure_label(err.value) == "timeout"


def test_blocked_or_empty_answers_fall_through_and_thoughts_are_never_the_answer():
    with pytest.raises(ValueError):
        main._gemini_text({"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(ValueError):
        main._gemini_text({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]})
    assert main._gemini_text({"candidates": [{"content": {"parts": [
        {"text": "let me look at the label", "thought": True}, {"text": " {\"name\": \"x\"} "}]}}]}) == '{"name": "x"}'


def test_gemini_warm_up_looks_the_model_up_and_spends_nothing(fake_gemini, monkeypatch):
    server = fake_gemini()
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert asyncio.run(main._warm_providers())["gemini"] is True
    assert server.requests == [("GET", f"/v1beta/models/{main.GEMINI_MODEL}", "g-key", None)]


def test_a_retired_gemini_model_fails_the_warm_up_loudly(fake_gemini, monkeypatch, capsys):
    fake_gemini(missing=True)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert asyncio.run(main._warm_providers())["gemini"] is False
    assert "NOT available: gemini" in capsys.readouterr().out


def test_one_gemini_connection_pool_per_loop(fake_gemini):
    fake_gemini()

    async def two_scans():
        first = main._gemini_client("g-key")
        await main._call_gemini("g-key", main.BOTTLE_PROMPT, IMAGE)
        await main._call_gemini("g-key", main.BOTTLE_PROMPT, IMAGE)
        return first is main._gemini_client("g-key"), first
    same, client = asyncio.run(two_scans())
    assert same
    assert client._transport._pool._keepalive_expiry == main.AI_KEEPALIVE_SECONDS


def test_openai_reasoning_tokens_are_the_thinking_figure():
    from types import SimpleNamespace as NS
    usage = NS(prompt_tokens=5000, completion_tokens=400, prompt_tokens_details=NS(cached_tokens=3968),
               completion_tokens_details=NS(reasoning_tokens=320))
    assert main._openai_usage(NS(usage=usage)) == {
        "input_tokens": 5000, "cached_tokens": 3968, "output_tokens": 400, "thinking_tokens": 320}


# ─── what happens to the answer ───────────────────────────────────────────────

def test_parse_forces_every_field_into_shape():
    r = main._parse_ai_result('```json\n{"name": null, "brand": " Gatorade ", "category": "Liqueur", '
                              '"confidence": "high"}\n```')
    assert (r["name"], r["brand"], r["category"], r["confidence"], r["product_type"]) == \
        ("", "Gatorade", "other", 0.0, "")
    assert main._parse_ai_result('{"name": "x", "confidence": 7}')["confidence"] == 1.0
    assert main._parse_ai_result('{"name": "x", "category": "SPIRITS"}')["category"] == "spirits"
    with pytest.raises(ValueError):
        main._parse_ai_result('["not", "an", "object"]')


def _request():
    return main.ScanAnalyzeRequest(image=IMAGE, location_id="loc-1")


def _catalog(monkeypatch, found=("prod-7", "exact"), forbid=None):
    """Stub the catalog: _find_product (the read-only lookup) returns `found`,
    _record_match (the one write) echoes it. With `forbid`, reaching either
    fails the test with that message. Returns the calls made."""
    calls = []

    def find(result, user, location=None):
        if forbid:
            pytest.fail(forbid)
        calls.append(("find", user, location))
        return found

    def record(result, user, product_id, method, allow_create=True):
        if forbid:
            pytest.fail(forbid)
        calls.append(("record", product_id, method, allow_create))
        return (product_id, False, method) if product_id else (None, False, "none")

    monkeypatch.setattr(main, "_find_product", find)
    monkeypatch.setattr(main, "_record_match", record)
    return calls


def _active(user):
    return {"subscription_status": "active", "trial_ends_at": None}, None


def test_unreadable_label_is_never_matched(monkeypatch):
    _catalog(monkeypatch, forbid="an unreadable read must not be matched to a product")
    event = {"id": "scan-1"}
    answer = dict(GOOD, name="Sports Drink", brand="Gatorade", confidence=0.5)
    response = main._process_ai_result(json.dumps(answer), _request(), "user-1", event)
    assert response.matched_product_id is None
    assert response.needs_rescan is True
    assert response.match_method == "unreadable"
    assert response.scan_id == "scan-1"
    assert event["status"] == "unreadable"


def test_confident_read_is_matched_against_the_scanning_bar(monkeypatch):
    calls = _catalog(monkeypatch, found=("prod-7", "bar_book"))
    event = {"id": "scan-2"}
    response = main._process_ai_result(json.dumps(GOOD), _request(), "user-1", event)
    assert (response.matched_product_id, response.match_method, response.needs_rescan) == ("prod-7", "bar_book", False)
    assert calls == [("find", "user-1", "loc-1"), ("record", "prod-7", "bar_book", True)]
    assert event["matched_product_id"] == "prod-7" and event["status"] == "ok"


def test_no_bottle_is_still_an_empty_200(monkeypatch):
    _catalog(monkeypatch, forbid="nothing to match")
    event = {}
    response = main._process_ai_result('{"name": "", "brand": "", "category": "other", '
                                       '"product_type": "", "confidence": 0}', _request(), "u", event)
    assert isinstance(response, JSONResponse) and response.status_code == 200 and response.body == b"null"
    assert event["status"] == "no_bottle"


def test_unusable_openai_answer_falls_through_to_gemini(monkeypatch):
    async def openai_says_sorry(key, prompt, image, stats):
        return "I'm sorry, I can't help with that."

    async def gemini_answers(key, prompt, image, stats):
        stats["input_tokens"] = 1200
        return json.dumps(GOOD)

    monkeypatch.setattr(main, "_call_openai", openai_says_sorry)
    monkeypatch.setattr(main, "_call_gemini", gemini_answers)
    _catalog(monkeypatch)
    event = {"id": "scan-3"}
    response = asyncio.run(main._run_providers("sk", "g", main.BOTTLE_PROMPT, _request(), "user-1", event))
    assert response.matched_product_id == "prod-7"
    assert event["provider"] == "gemini" and event["fallback_from"] == "openai:unparseable"
    assert event["input_tokens"] == 1200


def test_total_timeout_is_a_504_not_no_bottle(monkeypatch):
    logged = []

    async def never_answers(*args):
        await asyncio.sleep(5)

    monkeypatch.setattr(main, "_scan_context", _active)
    monkeypatch.setattr(main, "_run_providers", never_answers)
    monkeypatch.setattr(main, "_record_scan_event", logged.append)
    monkeypatch.setattr(main, "TOTAL_SCAN_TIMEOUT_SEC", 0.05)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    async def scan():
        with pytest.raises(HTTPException) as err:
            await main.analyze_bottle(_request(), "user-1")
        await asyncio.sleep(0.05)  # let the background log write run
        return err.value

    err = asyncio.run(scan())
    assert err.status_code == 504 and err.detail["error"] == "ai_timeout"
    [event] = logged
    assert event["status"] == "timeout" and event["location_id"] == "loc-1" and event["total_ms"] >= 50


def test_every_scan_is_logged_with_its_scan_id(monkeypatch):
    logged = []

    async def answers(openai_key, gemini_key, prompt, request, user_id, event):
        event.update(provider="openai", model="gpt-4o", provider_ms=1500)
        return await asyncio.to_thread(main._process_ai_result, json.dumps(GOOD), request, user_id, event)

    monkeypatch.setattr(main, "_scan_context", _active)
    monkeypatch.setattr(main, "_run_providers", answers)
    _catalog(monkeypatch)
    monkeypatch.setattr(main, "_record_scan_event", logged.append)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")

    async def scan():
        response = await main.analyze_bottle(_request(), "user-1")
        await asyncio.sleep(0.05)
        return response

    response = asyncio.run(scan())
    [event] = logged
    assert response.scan_id == event["id"]
    assert (event["status"], event["matched_product_id"], event["user_id"]) == ("ok", "prod-7", "user-1")


# ─── what the count ended up as ───────────────────────────────────────────────

def test_scan_finals_reads_only_well_formed_scanned_rows():
    bottles = [
        {"id": "b1", "scanId": "scan-1", "productId": "prod-1"},
        {"id": "b2", "scanId": "scan-2"},                           # still identifying
        {"id": "b3", "productId": "prod-3"},                        # added by hand, never scanned
        {"id": "b4", "scanId": 42, "productId": "prod-4"},          # not ours
        {"id": "b5", "scanId": "x" * 65, "productId": "prod-5"},    # not a scan id
        "garbage",
        {"id": "b6", "scanId": "scan-6", "productId": "prod-6"},
    ]
    assert main._scan_finals(bottles) == {"scan-1": "prod-1", "scan-6": "prod-6"}
    assert main._scan_finals(None) == {}


# ─── the label-text check ─────────────────────────────────────────────────────

def test_the_model_writes_what_it_read_before_the_answer():
    schema = main.SCAN_SCHEMA
    assert list(schema["properties"])[0] == "label_text"   # strict output writes keys in this order
    assert "label_text" in schema["required"]
    assert main.BOTTLE_PROMPT.index('"label_text"') < main.BOTTLE_PROMPT.index('"name": "Variant')


def test_parse_keeps_label_text_as_a_bounded_string():
    assert main._parse_ai_result('{"name": "x"}')["label_text"] == ""
    assert main._parse_ai_result('{"name": "x", "label_text": null}')["label_text"] == ""
    assert len(main._parse_ai_result(json.dumps({"name": "x", "label_text": "A " * 1000}))["label_text"]) == 600


def test_a_name_from_memory_is_not_matched(monkeypatch):
    _catalog(monkeypatch, forbid="a name missing from the model's own reading must not be matched")
    event = {"id": "scan-4"}
    answer = dict(GOOD, name="Glacier Freeze", brand="Gatorade", confidence=0.93,
                  label_text="GATORADE THIRST QUENCHER BLUE BOLT")
    response = main._process_ai_result(json.dumps(answer), _request(), "user-1", event)
    assert (response.matched_product_id, response.match_method, response.needs_rescan) == (None, "unreadable", True)
    assert (event["status"], event["label_supported"]) == ("label_unsupported", False)
    assert event["label_text"] == "GATORADE THIRST QUENCHER BLUE BOLT"


def test_a_name_on_the_label_is_matched(monkeypatch):
    _catalog(monkeypatch, found=("prod-bb", "exact"))
    event = {"id": "scan-5"}
    answer = dict(GOOD, name="Blue Bolt", brand="Gatorade", label_text="GATORADE BLUE BOLT")
    response = main._process_ai_result(json.dumps(answer), _request(), "user-1", event)
    assert response.matched_product_id == "prod-bb"
    assert (event["status"], event["label_supported"]) == ("ok", True)


def test_label_check_can_be_set_to_log_only(monkeypatch):
    monkeypatch.setattr(main, "LABEL_CHECK", "log")
    _catalog(monkeypatch, found=("prod-gf", "exact"))
    event = {"id": "scan-6"}
    answer = dict(GOOD, name="Glacier Freeze", brand="Gatorade", label_text="GATORADE BLUE BOLT")
    response = main._process_ai_result(json.dumps(answer), _request(), "user-1", event)
    assert response.matched_product_id == "prod-gf"                  # matched as before...
    assert (event["status"], event["label_supported"]) == ("ok", False)  # ...but recorded


def test_low_confidence_stays_unreadable_not_unsupported(monkeypatch):
    _catalog(monkeypatch, forbid="unreadable")
    event = {"id": "scan-7"}
    answer = dict(GOOD, name="Sports Drink", brand="Gatorade", confidence=0.5, label_text="GATORADE")
    main._process_ai_result(json.dumps(answer), _request(), "user-1", event)
    assert event["status"] == "unreadable"


def test_sdk_request_asks_for_label_text_first(fake_openai):
    server = fake_openai()
    asyncio.run(main._call_openai("sk-test", main.BOTTLE_PROMPT, IMAGE))
    [body] = server.posts()
    assert list(body["response_format"]["json_schema"]["schema"]["properties"])[0] == "label_text"


# ─── a database that doesn't answer is not "no such bottle" ──────────────────

def _db_down(monkeypatch, writes=None):
    """get_db that fails every lookup; with `writes`, the connection works but
    records statements, for the write half."""
    import contextlib

    @contextlib.contextmanager
    def down():
        raise RuntimeError("connection pool exhausted")
        yield

    monkeypatch.setattr(main, "get_db", down)


def test_a_failed_lookup_says_so(monkeypatch):
    _db_down(monkeypatch)
    assert main._find_product(dict(GOOD), "user-1", "loc-1") == (None, "lookup_failed")


def test_a_failed_lookup_never_creates_a_product(monkeypatch):
    """Even when the database is answering again by the time the scan is counted
    (a pool that was only busy): the bottle is most likely in the catalog."""
    import contextlib
    written = []

    class Cur:
        def execute(self, sql, params=()):
            written.append(sql)

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            pass
    monkeypatch.setattr(main, "get_db", lambda: contextlib.nullcontext(Conn()))
    assert main._record_match(dict(GOOD, confidence=0.99), "user-1", None, "lookup_failed",
                              allow_create=True) == (None, False, "lookup_failed")
    assert written == []


def test_a_found_product_survives_a_failed_count(monkeypatch):
    _db_down(monkeypatch)
    assert main._record_match(dict(GOOD), "user-1", "prod-7", "bar_book") == ("prod-7", False, "bar_book")
    assert main._record_match(dict(GOOD), "user-1", None, "none") == (None, False, "lookup_failed")


def test_the_scan_is_retried_not_answered_no_match(monkeypatch):
    """"No match" tells the bartender to add the bottle by hand — a duplicate in
    the making. A 503 is what the app's retry sweep picks up."""
    monkeypatch.setattr(main, "_find_product", lambda result, user, location=None: (None, "lookup_failed"))
    _db_down(monkeypatch)
    event = {"id": "scan-9"}
    with pytest.raises(HTTPException) as err:
        main._process_ai_result(json.dumps(GOOD), _request(), "user-1", event)
    assert err.value.status_code == 503 and err.value.detail["error"] == "catalog_unavailable"
    assert event["status"] == "lookup_failed"
