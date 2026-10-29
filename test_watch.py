"""The walkthrough video: /watch, ranged video bytes (Safari needs a 206), the
welcome email's optional link, and the app's two new events."""
import sys
import types

import pytest

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub
if not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import main  # noqa: E402
import media  # noqa: E402
import welcome  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


# ---------- media.parse_range ----------

@pytest.mark.parametrize("header,expect", [
    (None, None),
    ("", None),
    ("bytes=0-1", (0, 1)),                 # Safari's opening probe
    ("bytes=0-", (0, 999)),
    ("bytes=500-", (500, 999)),
    ("bytes=990-5000", (990, 999)),        # past the end: clamp
    ("bytes=-100", (900, 999)),            # suffix
    ("bytes=-5000", (0, 999)),
    ("bytes=1000-", "unsatisfiable"),
    ("bytes=-0", "unsatisfiable"),
    ("bytes=5-2", None),                   # nonsense: send it all
    ("bytes=0-1,5-9", None),               # several ranges: send it all
    ("items=0-1", None),
    ("bytes=-", None),
])
def test_parse_range(header, expect):
    assert media.parse_range(header, 1000) == expect


# ---------- routes ----------

@pytest.fixture
def client():
    return TestClient(main.app)


def test_watch_page_has_the_video_and_what_to_do(client):
    r = client.get("/watch")
    assert r.status_code == 200
    assert '<video src="/watch/first-count.mp4" controls playsinline' in r.text
    assert "Try 3 Bottles" in r.text


def test_the_whole_video(client):
    r = client.get("/watch/first-count.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes"
    size = int(r.headers["content-length"])
    assert size == len(r.content) > 1_000_000
    assert r.content[4:8] == b"ftyp"       # an MP4


def test_safaris_probe_gets_a_206(client):
    full = client.get("/watch/first-count.mp4").content
    r = client.get("/watch/first-count.mp4", headers={"Range": "bytes=0-1"})
    assert r.status_code == 206
    assert r.content == full[:2]
    assert r.headers["content-range"] == f"bytes 0-1/{len(full)}"
    assert r.headers["content-length"] == "2"
    r = client.get("/watch/first-count.mp4", headers={"Range": "bytes=100-"})
    assert r.status_code == 206 and r.content == full[100:]


def test_a_range_past_the_end_is_416(client):
    size = len(client.get("/watch/first-count.mp4").content)
    r = client.get("/watch/first-count.mp4", headers={"Range": f"bytes={size}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{size}"


def test_video_is_never_gzipped(client):
    """A gzipped 206 carries a Content-Range for bytes the browser never gets."""
    full = client.get("/watch/first-count.mp4", headers={"Accept-Encoding": "identity"}).content
    for rng in (None, "bytes=100-", "bytes=0-"):
        h = {"Accept-Encoding": "gzip"}
        if rng:
            h["Range"] = rng
        r = client.get("/watch/first-count.mp4", headers=h)
        assert "content-encoding" not in r.headers
        assert int(r.headers["content-length"]) == len(r.content)
    assert r.content == full


def test_other_routes_still_gzip(client):
    r = client.get("/watch", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"


def test_head_answers_without_a_body(client):
    r = client.head("/watch/first-count.mp4", headers={"Range": "bytes=0-1"})
    assert r.status_code == 206
    assert r.headers["content-length"] == "2"
    assert r.content == b""


# ---------- welcome email ----------

def test_no_url_set_the_email_is_unchanged(monkeypatch):
    monkeypatch.delenv("COMPANY_VIDEO_URL", raising=False)
    body = welcome.compose(name="Dana")["body"]
    assert "35 seconds" not in body
    assert "works exactly the same way.\n\nWhen do you usually" in body


def test_with_a_url_the_email_links_it(monkeypatch):
    monkeypatch.setenv("COMPANY_VIDEO_URL", "https://eight6d-api.onrender.com/watch")
    body = welcome.compose(name="Dana")["body"]
    assert "in 35 seconds: https://eight6d-api.onrender.com/watch\n\nWhen do you usually" in body
    assert body.count("https://") == 1     # the signature's website has no scheme


def test_video_events_are_allowlisted():
    assert {"how_video_opened", "how_video_finished"} <= main.APP_EVENTS
