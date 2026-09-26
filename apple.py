"""App Store Connect analytics for the CRM's Apple Analytics tab.

The numbers App Store Connect shows under App Analytics — impressions, product
page views, downloads, purchases/proceeds, sessions, installs and deletions,
crashes — come out of Apple's Analytics Reports API, not a single "give me the
dashboard" call:

  1. ask Apple ONCE to start generating reports for the app
     (POST /v1/analyticsReportRequests, accessType ONGOING)
  2. Apple then produces report INSTANCES — one per day — for each report
     (daily TSVs, gzipped, behind pre-signed download URLs)
  3. download each instance's segments and total them up

The first instances take Apple about a day or two to produce after step 1, so a
freshly connected account shows nothing for a while. That is normal, and the
page says so rather than looking broken.

Auth is a JWT signed ES256 with the team API key (.p8) — Key ID in the header,
Issuer ID as `iss`, audience `appstoreconnect-v1`, at most 20 minutes to live.

Everything here is pure or takes an injected HTTP client, so it's testable
without a database or Apple. crm.py owns storage and the routes.
"""
from __future__ import annotations

import csv
import gzip
import io
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

ASC_BASE = "https://api.appstoreconnect.apple.com"
TOKEN_TTL = 19 * 60          # Apple rejects anything over 20 minutes

# ── auth ────────────────────────────────────────────────────────────────────


def make_token(key_id: str, issuer_id: str, private_key: str,
               now: Optional[float] = None) -> str:
    """The bearer token App Store Connect expects."""
    from jose import jwt  # python-jose[cryptography], already a dependency

    iat = int(now if now is not None else time.time())
    return jwt.encode(
        {"iss": issuer_id, "iat": iat, "exp": iat + TOKEN_TTL,
         "aud": "appstoreconnect-v1"},
        normalize_key(private_key), algorithm="ES256",
        headers={"kid": key_id, "typ": "JWT"})


def normalize_key(pem: str) -> str:
    """A .p8 as pasted, uploaded or stored in an env var, back into PEM.

    Env vars commonly carry the key with literal "\\n" in place of newlines,
    and pasting can mangle line breaks entirely; the body is base64, so it can
    be rebuilt from the text between the markers.
    """
    text = (pem or "").strip().replace("\\n", "\n")
    m = re.search(r"-----BEGIN PRIVATE KEY-----(.*?)-----END PRIVATE KEY-----", text, re.S)
    body = re.sub(r"\s+", "", m.group(1) if m else text)
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"


# ── client ──────────────────────────────────────────────────────────────────


class AppleError(Exception):
    """Something Apple said no to, phrased for the person reading the page."""


class ASC:
    """A thin App Store Connect client. `http` is an httpx.Client (or a fake)."""

    def __init__(self, key_id: str, issuer_id: str, private_key: str, http=None):
        import httpx

        self.key_id, self.issuer_id, self.private_key = key_id, issuer_id, private_key
        self.http = http or httpx.Client(timeout=40.0)
        self._token, self._token_at = None, 0.0

    def _auth(self) -> dict:
        if not self._token or time.time() - self._token_at > TOKEN_TTL - 60:
            try:
                self._token = make_token(self.key_id, self.issuer_id, self.private_key)
            except Exception as exc:
                raise AppleError("That private key couldn't be read — paste the whole "
                                 f".p8 file, BEGIN/END lines included. ({exc.__class__.__name__})")
            self._token_at = time.time()
        return {"Authorization": f"Bearer {self._token}"}

    def _check(self, resp, what: str):
        if resp.status_code < 300:
            return resp.json() if resp.content else {}
        detail = ""
        try:
            errs = resp.json().get("errors") or []
            detail = "; ".join(e.get("detail") or e.get("title") or "" for e in errs)
        except Exception:
            detail = resp.text[:200]
        if resp.status_code == 401:
            raise AppleError("Apple rejected the key — check the Issuer ID, Key ID and "
                             "that the .p8 belongs to that Key ID.")
        if resp.status_code == 403:
            raise AppleError(f"The key doesn't have permission to {what}. Use a Team key "
                             f"with the Admin role. ({detail})")
        raise AppleError(f"Apple returned {resp.status_code} while trying to {what}: {detail}")

    def get(self, path: str, params: Optional[dict] = None, what: str = "read data") -> dict:
        url = path if path.startswith("http") else ASC_BASE + path
        return self._check(self.http.get(url, params=params, headers=self._auth()), what)

    def post(self, path: str, body: dict, what: str = "make a request") -> dict:
        return self._check(self.http.post(ASC_BASE + path, json=body, headers={
            **self._auth(), "Content-Type": "application/json"}), what)

    def pages(self, path: str, params: Optional[dict] = None, what: str = "read data",
              max_pages: int = 10) -> Iterable[dict]:
        """Every item across Apple's `links.next` pagination."""
        body = self.get(path, params, what)
        for _ in range(max_pages):
            yield from body.get("data") or []
            nxt = (body.get("links") or {}).get("next")
            if not nxt:
                return
            body = self.get(nxt, None, what)

    def download(self, url: str) -> bytes:
        # Segment URLs are pre-signed — no Authorization header, or the
        # storage host rejects the request.
        resp = self.http.get(url)
        if resp.status_code >= 300:
            raise AppleError(f"Couldn't download a report file ({resp.status_code}).")
        return resp.content


def resolve_app(asc: ASC, app_ref: Optional[str]) -> dict:
    """{id, name, bundle_id} from an Apple ID, a bundle ID, or nothing at all.

    Blank works when the account has exactly one app, which is the usual case.
    """
    ref = (app_ref or "").strip()
    if ref.isdigit():
        a = asc.get(f"/v1/apps/{ref}", what="read the app")["data"]
    else:
        params = {"limit": 200}
        if ref:
            params["filter[bundleId]"] = ref
        apps = list(asc.pages("/v1/apps", params, what="list your apps", max_pages=2))
        if not apps:
            raise AppleError(f"No app found{' with bundle ID ' + ref if ref else ''} on this account.")
        if len(apps) > 1 and not ref:
            names = ", ".join(x["attributes"].get("name", "?") for x in apps[:6])
            raise AppleError(f"This account has {len(apps)} apps ({names}) — enter the "
                             "bundle ID or Apple ID of the one to track.")
        a = apps[0]
    attrs = a.get("attributes") or {}
    return {"id": a["id"], "name": attrs.get("name") or "", "bundle_id": attrs.get("bundleId") or ""}


def ensure_report_request(asc: ASC, app_id: str, known_id: Optional[str]) -> str:
    """The ONGOING analytics report request for this app, creating it if needed.

    Apple allows one ONGOING request per app, so an existing one is reused
    rather than creating a duplicate (which Apple would refuse anyway). One
    Apple has STOPPED (`stoppedDueToInactivity`: nobody fetched its reports
    for a long time) produces nothing new however long it's kept, so it's
    replaced — the saved id used to be trusted as long as Apple answered,
    and the numbers would quietly have stopped moving.
    """
    if known_id:
        try:
            got = asc.get(f"/v1/analyticsReportRequests/{known_id}",
                          what="read the report request")
            if not (((got or {}).get("data") or {}).get("attributes") or {}).get(
                    "stoppedDueToInactivity"):
                return known_id
        except AppleError:
            pass
    existing = list(asc.pages(f"/v1/apps/{app_id}/analyticsReportRequests",
                              {"filter[accessType]": "ONGOING"},
                              what="read the report requests", max_pages=1))
    live = [r for r in existing if not (r.get("attributes") or {}).get("stoppedDueToInactivity")]
    if live:
        return live[0]["id"]
    made = asc.post("/v1/analyticsReportRequests", {"data": {
        "type": "analyticsReportRequests",
        "attributes": {"accessType": "ONGOING"},
        "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}},
        what="start analytics reports (this needs the Admin role)")
    return made["data"]["id"]


# ── parsing ─────────────────────────────────────────────────────────────────

# Apple's report files vary by report, so nothing below assumes a fixed set of
# columns. A column is a METRIC if its name says it counts or sums something;
# every other column is a dimension, and the first dimension from DIM_PREFERENCE
# that's present is the one the numbers get split by.
METRIC_RE = re.compile(
    r"count|proceeds|sales|sessions?|duration|crashes|devices|units|downloads?"
    r"|installations?|deletions?|impressions|views|revenue|amount|total", re.I)
DIM_PREFERENCE = [
    "Event", "Download Type", "Purchase Type", "Installation Type", "Deletion Type",
    "Crash Type", "Content Type", "Page Type", "Source Type", "Device",
]
NOT_METRIC = {"date", "app name", "app apple identifier", "territory", "platform",
              "platform version", "device", "source info", "page title"}


def parse_tsv(raw: bytes) -> list[dict]:
    """Rows of an Apple report file (gzipped or not) as dicts."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def _num(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _day(v) -> Optional[str]:
    s = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def aggregate(rows: list[dict]) -> dict:
    """{(day, dim, metric): total} across every row of one report instance."""
    if not rows:
        return {}
    cols = list(rows[0].keys())
    date_col = next((c for c in cols if c and c.strip().lower() == "date"), None)
    if not date_col:
        return {}
    metrics = [c for c in cols if c and c.strip().lower() not in NOT_METRIC
               and METRIC_RE.search(c)
               and any(_num(r.get(c)) is not None for r in rows[:50])]
    lowered = {c.strip().lower(): c for c in cols if c}
    dim_col = next((lowered[d.lower()] for d in DIM_PREFERENCE if d.lower() in lowered), None)
    page_col = lowered.get("page type")

    out: dict = {}
    for r in rows:
        day = _day(r.get(date_col))
        if not day:
            continue
        dim = (r.get(dim_col) or "").strip() if dim_col else "Total"
        dim = dim or "Other"
        # A page view keeps the page it was on ("Page view · Product page").
        # App Store Connect's Product Page Views — and the page views its
        # Impressions include — are the product page only, while the report's
        # page views also count version history, privacy, developer and
        # in-app event pages.
        if page_col and dim.lower() == "page view":
            dim = f"{dim} · {(r.get(page_col) or '').strip() or 'Other'}"
        for m in metrics:
            v = _num(r.get(m))
            if v is None:
                continue
            key = (day, dim, m.strip())
            out[key] = out.get(key, 0.0) + v
    return out


# ── sync ────────────────────────────────────────────────────────────────────


# What the tab reads: how the App Store shows and sells the app, and how it's
# used. The framework-usage and performance reports (dozens — ARKit,
# Bluetooth, disk space…) are left out; nothing here reads them.
WANTED_CATEGORIES = {"APP_STORE_ENGAGEMENT", "APP_STORE_COMMERCE", "APP_USAGE"}


def _report_name(r: dict) -> str:
    return ((r.get("attributes") or {}).get("name") or "").lower()


def wanted_reports(reports: list[dict]) -> list[dict]:
    """Engagement, commerce and usage reports — never a "Detailed" one, which
    carries the same numbers as its Standard twin split finer (and noised),
    so importing both would double every total.

    Only names with "Standard" in them used to be kept, and that also threw
    away App Crashes: it comes in one version, with no "Standard" in its name,
    so the Crashes tile could never show a number. Falls back to the Standard
    reports if Apple ever stops sending a category.
    """
    keep = [r for r in reports if "detailed" not in _report_name(r)
            and (r.get("attributes") or {}).get("category") in WANTED_CATEGORIES]
    return (keep or [r for r in reports if "standard" in _report_name(r)]
            or [r for r in reports if "detailed" not in _report_name(r)])


def is_usage(report: dict) -> bool:
    """Sessions, installs and deletions, crashes: the reports Apple leaves a
    whole DAY out of when fewer than five people who share analytics used the
    app that day (its privacy rule). For a small app that's most quiet days,
    so their tiles come from the WEEKLY files, where the same rule costs far
    less — and those are imported as well as the daily ones."""
    a = report.get("attributes") or {}
    return a.get("category") == "APP_USAGE" or bool(
        re.search(r"session|install|delet|crash", a.get("name") or "", re.I))


def sync(asc: ASC, request_id: str, already: Callable[[str], bool],
         save: Callable[..., None], days: int = 120,
         max_files: int = 600, today: Optional[date] = None) -> dict:
    """Import every instance not imported yet. Returns a small summary.

    `already(instance_id)` says whether an instance was imported before;
    `save(instance_id, report_name, processing_date, totals, granularity)`
    stores one, replacing whatever was stored for each date it carries.

    Oldest first, always. A daily file carries the newest day AND the full,
    restated numbers for the few days before it (late-arriving events), and
    Apple's rule is that the file with the later processingDate wins. The
    order used to be whatever Apple listed, so an older file saved after a
    newer one could put a day's incomplete numbers back over its complete ones.
    """
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    reports = wanted_reports(list(asc.pages(
        f"/v1/analyticsReportRequests/{request_id}/reports", {"limit": 200},
        what="list reports", max_pages=5)))
    imported = files = 0
    for rep in reports:
        name = (rep.get("attributes") or {}).get("name") or rep["id"]
        for grain in (("DAILY", "WEEKLY") if is_usage(rep) else ("DAILY",)):
            instances = list(asc.pages(f"/v1/analyticsReports/{rep['id']}/instances",
                                       {"filter[granularity]": grain, "limit": 200},
                                       what="list report days", max_pages=3))
            instances.sort(key=lambda i: (i.get("attributes") or {}).get("processingDate") or "")
            for inst in instances:
                pdate = (inst.get("attributes") or {}).get("processingDate") or ""
                if pdate and pdate < cutoff or already(inst["id"]):
                    continue
                totals: dict = {}
                for seg in asc.pages(f"/v1/analyticsReportInstances/{inst['id']}/segments",
                                     None, what="list report files", max_pages=2):
                    url = (seg.get("attributes") or {}).get("url")
                    if not url:
                        continue
                    if files >= max_files:
                        return {"reports": len(reports), "imported": imported, "partial": True}
                    files += 1
                    for k, v in aggregate(parse_tsv(asc.download(url))).items():
                        totals[k] = totals.get(k, 0.0) + v
                save(inst["id"], name, pdate, totals, grain)
                imported += 1
    return {"reports": len(reports), "imported": imported, "partial": False}


# ── summary ─────────────────────────────────────────────────────────────────

# Product page views the way App Store Connect counts them: the product page,
# and the same page opened inside another app through StoreKit (a "store
# sheet"). Not version history, privacy, developer or in-app event pages.
PRODUCT_PAGE = r"^page ?view · (product page|store sheet)$"

# The headline tiles, in App Store Connect's own order and by its own
# definitions (developer.apple.com/help/app-store-connect-analytics, "Metric
# definitions"). Each picks its rows by report name + dimension + metric,
# case-insensitive regexes, because Apple's exact wording varies between
# reports. A tile whose rows aren't there shows "—" rather than a wrong
# number. `up_is_good` colours the change.
KPIS = [
    # App Store Connect's Impressions "include product page views"; the
    # report's Impression event doesn't ("page views are not included"), so
    # they're added back in.
    ("impressions", "Impressions", "discovery|engagement",
     rf"^impression|{PRODUCT_PAGE}", "^counts$", True),
    ("page_views", "Product page views", "discovery|engagement", PRODUCT_PAGE, "^counts$", True),
    # Total downloads = first-time downloads + redownloads. Never updates or
    # restores, which the same report also carries.
    ("downloads", "Downloads", "download", r"^(first|re-?download)", "^counts$", True),
    ("proceeds", "Proceeds (USD)", "purchase|commerce", "", "proceeds", True),
    ("sessions", "Sessions", "session", "", "^sessions$|^counts$", True),
    # The Installations and Deletions report only: every usage report is
    # imported now, and "install" alone would also match Platform App Installs.
    ("installs", "Installations", r"install\w* and delet", r"install", "^counts$", True),
    ("deletions", "Deletions", r"install\w* and delet", r"delet", "^counts$", False),
    ("crashes", "Crashes", "crash", "", "^crashes$|^counts$", False),
]
# Read from the WEEKLY files (see is_usage): the last N full Monday-to-Sunday
# weeks, N for each window the page offers.
USAGE_KPIS = {"sessions", "installs", "deletions", "crashes"}
WEEKS_FOR = {7: 1, 30: 4, 90: 13}


def _match(pat: str, text: Optional[str]) -> bool:
    return not pat or bool(re.search(pat, text or "", re.I))


def _pick(rows: list[dict], rep_pat: str, dim_pat: str, met_pat: str) -> list[dict]:
    picked = [r for r in rows if _match(rep_pat, r["report"]) and _match(dim_pat, r["dim"])
              and _match(met_pat, r["metric"])]
    if not picked:
        return []
    # One metric per tile: if several matched (e.g. both "Counts" and
    # "Unique Counts" slipped through a loose pattern) keep the first.
    metric = picked[0]["metric"]
    return [r for r in picked if r["metric"] == metric]


def _change(cur: float, prev: Optional[float]) -> Optional[float]:
    return round((cur - prev) / prev * 100, 1) if prev else None


def summarize(rows: list[dict], window: int = 30, today: Optional[date] = None,
              weekly: Optional[list[dict]] = None, since: Optional[str] = None,
              since_week: Optional[str] = None) -> dict:
    """Tiles and per-report tables from stored rows {report, day, dim, metric,
    value} (daily) and `weekly` rows {report, week, dim, metric, value}.

    Windows end at the latest day Apple has reported, not at today: the most
    recent day or two are never in yet, and counting them as zeros would make
    every number look like it just fell off a cliff.

    `since` is the first day any data exists for (the day the reports were
    first requested, give or take — Apple sends nothing from before that).
    Nothing is compared against a window that starts before it: "+400% vs
    the previous 30 days" against days nobody imported is noise. A window
    that starts before it says how many of its days are covered.
    """
    weekly = weekly or []
    if not rows and not weekly:
        return {"kpis": [], "reports": [], "through": None, "since": None, "window": window}
    last = max((r["day"] for r in rows), default=None)
    since = since or min((r["day"] for r in rows), default=None)
    if last:
        end = date.fromisoformat(last)
        cur_from = (end - timedelta(days=window - 1)).isoformat()
        prev_from = (end - timedelta(days=2 * window - 1)).isoformat()
        prev_ok = bool(since) and prev_from >= since
        shown = [(end - timedelta(days=i)).isoformat() for i in range(window - 1, -1, -1)]
        shown = [d for d in shown if not since or d >= since]
    # Weekly: the N full weeks ending at the latest week Apple has sent (any
    # usage report — a week one report skipped for having fewer than five
    # users counts as nothing, never as a gap to slide past).
    n = WEEKS_FOR.get(window, max(1, round(window / 7)))
    last_week = max((r["week"] for r in weekly), default=None)
    since_week = since_week or min((r["week"] for r in weekly), default=None)
    if last_week:
        wend = date.fromisoformat(last_week)
        weeks = [(wend - timedelta(weeks=i)).isoformat() for i in range(n - 1, -1, -1)]
        prev_weeks = [(wend - timedelta(weeks=n + i)).isoformat() for i in range(n - 1, -1, -1)]
        wprev_ok = bool(since_week) and prev_weeks[0] >= since_week

    kpis = []
    by: dict = {}
    for key, label, rep_pat, dim_pat, met_pat, up_good in KPIS:
        picked_w = (_pick([{**r, "day": r["week"]} for r in weekly], rep_pat, dim_pat, met_pat)
                    if key in USAGE_KPIS and last_week else [])
        picked = [] if picked_w else (_pick(rows, rep_pat, dim_pat, met_pat) if last else [])
        if picked_w:
            cur = sum(r["value"] for r in picked_w if r["day"] in weeks)
            prev = sum(r["value"] for r in picked_w if r["day"] in prev_weeks) if wprev_ok else None
            per = {w: 0.0 for w in weeks if w >= since_week}
            for r in picked_w:
                if r["day"] in per:
                    per[r["day"]] += r["value"]
            k = {"key": key, "label": label, "value": cur, "previous": prev,
                 "change_pct": _change(cur, prev), "up_is_good": up_good,
                 "period": "weeks", "weeks": n, "weeks_covered": len(per),
                 "from": weeks[0], "to": (wend + timedelta(days=6)).isoformat(),
                 "daily": [{"day": w, "value": v} for w, v in per.items()]}
        elif picked:
            cur = sum(r["value"] for r in picked if r["day"] >= cur_from)
            prev = (sum(r["value"] for r in picked if prev_from <= r["day"] < cur_from)
                    if prev_ok else None)
            daily = {d: 0.0 for d in shown}
            for r in picked:
                if r["day"] in daily:
                    daily[r["day"]] += r["value"]
            k = {"key": key, "label": label, "value": cur, "previous": prev,
                 "change_pct": _change(cur, prev), "up_is_good": up_good,
                 "period": "days", "days": window, "days_covered": len(shown),
                 "daily": [{"day": d, "value": daily[d]} for d in shown]}
            if key in USAGE_KPIS:
                # No weekly file yet (they come every Friday for the week
                # before): these are the daily files, which skip any day with
                # fewer than five users.
                k["partial"] = True
        else:
            k = {"key": key, "label": label, "value": None}
        kpis.append(k)
        by[key] = k

    # Conversion the way App Store Connect defines it: total downloads divided
    # by UNIQUE-device impressions (which include unique product page views).
    # Apple's files give unique counts per day and per row, so this is their
    # sum — the same figure App Store Connect builds a range from, give or
    # take a device counted on two rows of one day.
    imp_dim = KPIS[0][3]
    uniq = _pick(rows, "discovery|engagement", imp_dim, "^unique counts$") if last else []
    dl = by.get("downloads") or {}
    if uniq and dl.get("value") is not None:
        u_cur = sum(r["value"] for r in uniq if r["day"] >= cur_from)
        u_prev = (sum(r["value"] for r in uniq if prev_from <= r["day"] < cur_from)
                  if prev_ok else None)
        if u_cur:
            conv = dl["value"] / u_cur * 100
            prev = (dl["previous"] / u_prev * 100
                    if u_prev and dl.get("previous") is not None else None)
            kpis.insert(2, {"key": "conversion", "label": "Conversion rate", "value": conv,
                            "unit": "%", "previous": prev, "up_is_good": True,
                            "change_pct": round(conv - prev, 1) if prev is not None else None,
                            "change_is_points": True, "period": "days", "days": window,
                            "days_covered": len(shown)})

    # Everything else Apple reported, as plain tables — nothing is dropped.
    reports: dict = {}
    for r in rows:
        if r["day"] < prev_from:
            continue
        t = reports.setdefault(r["report"], {})
        cell = t.setdefault((r["dim"], r["metric"]),
                            {"last7": 0.0, "current": 0.0, "previous": 0.0 if prev_ok else None})
        if r["day"] >= cur_from:
            cell["current"] += r["value"]
            if r["day"] >= (end - timedelta(days=6)).isoformat():
                cell["last7"] += r["value"]
        elif prev_ok:
            cell["previous"] += r["value"]
    tables = [{"report": name, "rows": [
        {"dim": d, "metric": m, **v}
        for (d, m), v in sorted(cells.items(), key=lambda kv: (-kv[1]["current"], kv[0]))]}
        for name, cells in sorted(reports.items())]
    return {"kpis": kpis, "reports": tables, "through": last, "since": since,
            "weeks_through": ((date.fromisoformat(last_week) + timedelta(days=6)).isoformat()
                              if last_week else None),
            "window": window}
