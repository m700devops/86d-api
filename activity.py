"""Scans and background crawls take turns.

The CRM's background crawling (the daily lead run, phone checks, the
restaurant recheck) reads bar websites in this same process as the bottle
scanner, on half a CPU, and reading pages strangers wrote holds Python's GIL
while it works. It has taken the whole server down before. So the crawl steps
aside: every scan, and the app opening its scan screen, marks the scanner
busy, and a background crawl waits before each site until nothing has been
scanned for QUIET_SECONDS. A count is a burst of scans a few seconds apart, so
the crawl simply waits it out.

Only background work waits. What someone clicked and is waiting on — the
CRM's quick-add email lookup, a wrong-number check — never does.

Pure state and a clock, no database: covered by test_crawl_quiet.py.
"""
import os
import threading
import time
from typing import Callable, Optional

# How long after the last scan a count is still "in progress". Scans in a count
# come seconds apart, with the odd minute's gap to reach another shelf.
QUIET_SECONDS = max(0, int(os.getenv("CRAWL_QUIET_SECONDS", "180")))
POLL_SECONDS = 10

_clock: Callable[[], float] = time.monotonic
_last_scan: Optional[float] = None
_lock = threading.Lock()
_paused_since: Optional[float] = None


def scan_seen() -> None:
    """A scan happened (or the scan screen opened): the scanner has the CPU."""
    global _last_scan
    _last_scan = _clock()


def scanning() -> bool:
    return _last_scan is not None and _clock() - _last_scan < QUIET_SECONDS


def wait_for_quiet(until: Optional[float] = None, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Block while someone is scanning. True once it's quiet; False if `until`
    (a time.monotonic() value) comes first, for a caller with a time budget.
    Logs one CRAWL_PAUSED when a pause starts and one CRAWL_RESUMED when it
    ends, however many crawl threads are waiting."""
    global _paused_since
    while scanning():
        with _lock:
            if _paused_since is None:
                _paused_since = _clock()
                print("[leadgen] CRAWL_PAUSED a bottle count is in progress — "
                      "crawling waits until it's quiet", flush=True)
        if until is not None and _clock() >= until:
            return False
        sleep(POLL_SECONDS if until is None else max(0.0, min(POLL_SECONDS, until - _clock())))
    with _lock:
        if _paused_since is not None:
            print(f"[leadgen] CRAWL_RESUMED after {int(_clock() - _paused_since)}s", flush=True)
            _paused_since = None
    return True
