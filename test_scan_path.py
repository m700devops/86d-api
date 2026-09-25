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
    main._gemini_models.clear()
    main._openai_plain_models.clear()
    main._gemini_plain_models.clear()
    yield
    main._openai_clients.clear()
    main._gemini_models.clear()
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
    assert stats == {"input_tokens": 3400, "cached_tokens": 2560, "output_tokens": 40}


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


# ─── Gemini ───────────────────────────────────────────────────────────────────

class FakeGeminiModel:
    def __init__(self, reject_json=False):
        self.calls = []
        self.reject_json = reject_json

    def generate_content(self, contents, generation_config=None):
        self.calls.append(generation_config)
        if generation_config and self.reject_json:
            raise type("InvalidArgument", (Exception,), {})("400 response_mime_type not supported")
        return type("Response", (), {"text": json.dumps(GOOD), "usage_metadata": None})()


def test_gemini_is_configured_once(monkeypatch):
    configured, built = [], []
    monkeypatch.setattr(main.genai, "configure", lambda **kw: configured.append(kw))
    monkeypatch.setattr(main.genai, "GenerativeModel", lambda name: built.append(name) or FakeGeminiModel())
    assert main._gemini_model("g-key") is main._gemini_model("g-key")
    assert len(configured) == 1 and len(built) == 1


def test_gemini_asks_for_json_and_falls_back_when_refused(monkeypatch):
    model = FakeGeminiModel(reject_json=True)
    monkeypatch.setattr(main, "_gemini_model", lambda key: model)
    text = asyncio.run(main._call_gemini("g-key", main.BOTTLE_PROMPT, IMAGE))
    assert json.loads(text) == GOOD
    assert model.calls == [{"response_mime_type": "application/json"}, None]
    assert main.GEMINI_MODEL in main._gemini_plain_models


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


def test_unreadable_label_is_never_matched(monkeypatch):
    def matcher(*args):
        raise AssertionError("an unreadable read must not be matched to a product")
    monkeypatch.setattr(main, "_match_or_create_product", matcher)
    event = {"id": "scan-1"}
    answer = dict(GOOD, name="Sports Drink", brand="Gatorade", confidence=0.5)
    response = main._process_ai_result(json.dumps(answer), _request(), "user-1", event)
    assert response.matched_product_id is None
    assert response.needs_rescan is True
    assert response.match_method == "unreadable"
    assert response.scan_id == "scan-1"
    assert event["status"] == "unreadable"


def test_confident_read_is_matched(monkeypatch):
    monkeypatch.setattr(main, "_match_or_create_product", lambda result, user: ("prod-7", False, "exact"))
    event = {"id": "scan-2"}
    response = main._process_ai_result(json.dumps(GOOD), _request(), "user-1", event)
    assert (response.matched_product_id, response.match_method, response.needs_rescan) == ("prod-7", "exact", False)
    assert event["matched_product_id"] == "prod-7" and event["status"] == "ok"


def test_no_bottle_is_still_an_empty_200(monkeypatch):
    monkeypatch.setattr(main, "_match_or_create_product", lambda *a: pytest.fail("nothing to match"))
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
    monkeypatch.setattr(main, "_match_or_create_product", lambda result, user: ("prod-7", False, "exact"))
    event = {"id": "scan-3"}
    response = asyncio.run(main._run_providers("sk", "g", main.BOTTLE_PROMPT, _request(), "user-1", event))
    assert response.matched_product_id == "prod-7"
    assert event["provider"] == "gemini" and event["fallback_from"] == "openai:unparseable"
    assert event["input_tokens"] == 1200


def test_total_timeout_is_a_504_not_no_bottle(monkeypatch):
    logged = []

    async def never_answers(*args):
        await asyncio.sleep(5)

    monkeypatch.setattr(main, "_scan_subscription_row",
                        lambda user: {"subscription_status": "active", "trial_ends_at": None})
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

    monkeypatch.setattr(main, "_scan_subscription_row",
                        lambda user: {"subscription_status": "active", "trial_ends_at": None})
    monkeypatch.setattr(main, "_run_providers", answers)
    monkeypatch.setattr(main, "_match_or_create_product", lambda result, user: ("prod-7", False, "exact"))
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
