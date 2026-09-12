"""When to actually ring a bar, derived from its own opening hours.

A fixed 2-5pm window — what this replaced — is wrong for most of the list. Real
OpenStreetMap data from one harvest: `Mo-Fr 16:00-02:00` (opens at four),
`We-Sa 21:00-02:00` (a nightclub, opens at nine), `We-Su 11:00-23:00` (shut
Monday and Tuesday). Calling those at 2pm reaches an empty building, and a dial
that reaches nobody costs the same as one that does.

The heuristic, which is about how bars actually run rather than about clocks:

  opens at or before 11:30  →  ring 2:00-4:30pm. They're doing lunch at open,
                               and the post-lunch lull is the quiet hour when
                               the manager is doing paperwork and ordering.
  opens after 11:30         →  ring from open to two hours after. Staff arrive
                               to set up, the manager is on, nobody's ordering
                               drinks yet.

Everything here is a pure function of (hours string, now) so it can be tested
without a database, a network or a particular time of day.
"""

import re
from datetime import datetime, timedelta
from typing import Optional

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}

# Bars that open for lunch are busy at open; the useful gap is after it.
LUNCH_OPEN_CUTOFF = 11 * 60 + 30     # 11:30
LUNCH_WINDOW = (14 * 60, 16 * 60 + 30)   # 2:00pm - 4:30pm
# Everyone else: the first couple of hours after the doors open.
POST_OPEN_MINUTES = 120

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")
_DAYSPEC_RE = re.compile(r"^((?:Mo|Tu|We|Th|Fr|Sa|Su|PH|,|-|\s)+)", re.I)


def _expand_days(spec: str) -> list[int]:
    """'Mo-Th,Su' -> [0,1,2,3,6]. Unknown tokens are skipped, never fatal."""
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            ai, bi = DAY_INDEX.get(a.title()), DAY_INDEX.get(b.title())
            if ai is None or bi is None:
                continue
            # Ranges wrap: Sa-Mo is Saturday, Sunday, Monday.
            i = ai
            while True:
                out.append(i)
                if i == bi:
                    break
                i = (i + 1) % 7
        else:
            idx = DAY_INDEX.get(part.title())
            if idx is not None:
                out.append(idx)
    return sorted(set(out))


def parse_opening_hours(value: Optional[str]) -> Optional[dict]:
    """OSM opening_hours -> {day_index: [(open_min, close_min), ...]}.

    Returns None when the string can't be understood at all, and an empty dict
    for a venue marked permanently closed. Those two are different: unparseable
    means fall back to a sensible default, closed means drop the lead.

    Deliberately forgiving. The OSM specification is far larger than this
    (`Su off`, `Mo-Fr 09:00-12:00,13:00-17:00`, month ranges, sunset). Anything
    exotic falls through to None and the caller uses the generic window, which
    is no worse than the fixed window this replaced.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    low = text.lower()
    if low in ("closed", "off"):
        return {}
    if low in ("24/7", "24x7", "open"):
        return {d: [(0, 24 * 60)] for d in range(7)}

    schedule: dict[int, list[tuple[int, int]]] = {}
    understood = False

    for rule in text.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        if re.search(r"\boff\b|\bclosed\b", rule, re.I):
            # "Mo off" — an explicit closure for those days.
            m = _DAYSPEC_RE.match(rule)
            if m:
                for d in _expand_days(m.group(1)):
                    schedule[d] = []
                understood = True
            continue

        m = _DAYSPEC_RE.match(rule)
        if m and any(day.lower() in m.group(1).lower() for day in DAYS):
            days = _expand_days(m.group(1))
            rest = rule[m.end():]
        else:
            days = list(range(7))     # no day spec means every day
            rest = rule

        spans = []
        for h1, m1, h2, m2 in _TIME_RE.findall(rest):
            start = int(h1) * 60 + int(m1)
            end = int(h2) * 60 + int(m2)
            if end <= start:
                end += 24 * 60        # closes after midnight
            spans.append((start, end))
        if not spans or not days:
            continue
        understood = True
        for d in days:
            schedule.setdefault(d, []).extend(spans)

    if not understood:
        return None
    return schedule


def opens_at(schedule: Optional[dict], weekday: int) -> Optional[int]:
    """First opening time that weekday, in minutes past midnight.

    A span starting at 00:00 is the tail of the previous night, not a bar that
    opens at midnight, so it's ignored when deciding when the day begins.
    """
    if not schedule:
        return None
    spans = schedule.get(weekday) or []
    daytime = [s for s, _ in spans if s > 0]
    return min(daytime) if daytime else None


def is_open_today(schedule: Optional[dict], weekday: int) -> Optional[bool]:
    """True/False, or None when we simply don't know."""
    if schedule is None:
        return None
    if schedule == {}:
        return False
    return bool(schedule.get(weekday))


def call_window(hours: Optional[str], local_now: datetime) -> dict:
    """When to ring this venue today, and whether now is that time.

    Falls back to the generic afternoon window when the hours are missing or
    unparseable — never worse than the fixed window, better whenever the venue
    told us something.
    """
    weekday = local_now.weekday()
    now_min = local_now.hour * 60 + local_now.minute
    schedule = parse_opening_hours(hours)

    if schedule == {}:
        return {"state": "permanently_closed", "good_now": False,
                "headline": "Marked permanently closed", "window": None,
                "known": True}

    open_min = opens_at(schedule, weekday)

    if schedule is not None and is_open_today(schedule, weekday) is False:
        # Shut today. Say which day to try instead rather than just "no".
        nxt = next((DAYS[(weekday + i) % 7] for i in range(1, 8)
                    if schedule.get((weekday + i) % 7)), None)
        return {"state": "shut_today", "good_now": False, "window": None,
                "known": True,
                "headline": f"Closed today — try {nxt}" if nxt else "Closed today"}

    if open_min is None:
        start, end = LUNCH_WINDOW           # no usable hours: generic afternoon
        source = "generic"
    elif open_min <= LUNCH_OPEN_CUTOFF:
        start, end = LUNCH_WINDOW           # lunch trade: use the afternoon lull
        source = "post-lunch lull"
    else:
        start, end = open_min, open_min + POST_OPEN_MINUTES
        source = "just after they open"

    def hhmm(mins: int) -> str:
        mins %= 24 * 60
        h, m = divmod(mins, 60)
        suffix = "am" if h < 12 else "pm"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d}{suffix}"

    window = f"{hhmm(start)}-{hhmm(end)}"
    if start <= now_min < end:
        return {"state": "good", "good_now": True, "window": window,
                "known": schedule is not None,
                "headline": f"CALL NOW — {source}"}
    if now_min < start:
        wait = start - now_min
        pretty = f"{wait // 60}h {wait % 60}m" if wait >= 60 else f"{wait}m"
        return {"state": "early", "good_now": False, "window": window,
                "known": schedule is not None,
                "headline": f"Too early — best at {hhmm(start)} (in {pretty})"}
    return {"state": "late", "good_now": False, "window": window,
            "known": schedule is not None,
            "headline": f"Missed today's window ({window})"}
