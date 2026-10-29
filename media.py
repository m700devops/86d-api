"""Serving the walkthrough video: byte ranges, pure.

iPhone Safari will not play an <video> unless the server answers byte-range
requests (it opens with `Range: bytes=0-1` and expects a 206), and the pinned
Starlette's FileResponse doesn't do ranges. A 2MB file, read whole, sliced.
"""
import re
from typing import Optional, Tuple, Union

_RANGE = re.compile(r"^bytes=(\d{0,15})-(\d{0,15})$")


def parse_range(header: Optional[str], size: int) -> Union[None, str, Tuple[int, int]]:
    """(start, end) inclusive for a single satisfiable range; None to send the
    whole file (no header, or one we don't serve — multiple ranges, other
    units: answering 200 with everything is always allowed); "unsatisfiable"
    for a range past the end (416)."""
    if not header:
        return None
    m = _RANGE.match(header.strip())
    if not m:
        return None
    first, last = m.group(1), m.group(2)
    if first == "" and last == "":
        return None
    if first == "":                      # suffix: the last N bytes
        n = int(last)
        if n == 0:
            return "unsatisfiable"
        return (max(0, size - n), size - 1)
    start = int(first)
    if start >= size:
        return "unsatisfiable"
    end = size - 1 if last == "" else min(int(last), size - 1)
    if end < start:
        return None
    return (start, end)
