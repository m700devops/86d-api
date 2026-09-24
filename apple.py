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
    rather than creating a duplicate (which Apple would refuse anyway).
    """
    if known_id:
        try:
            asc.get(f"/v1/analyticsReportRequests/{known_id}", what="read the report request")
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

    out: dict = {}
    for r in rows:
        day = _day(r.get(date_col))
        if not day:
            continue
        dim = (r.get(dim_col) or "").strip() if dim_col else "Total"
        dim = dim or "Other"
        for m in metrics:
            v = _num(r.get(m))
            if v is None:
                continue
            key = (day, dim, m.strip())
            out[key] = out.get(key, 0.0) + v
    return out


# ── sync ────────────────────────────────────────────────────────────────────


def wanted_reports(reports: list[dict]) -> list[dict]:
    """Apple's "Standard" reports — the aggregate ones App Analytics is built on.

    The "Detailed" variants carry the same numbers split finer, and importing
    both would double every total. Falls back to everything if Apple ever
    stops using the word.
    """
    std = [r for r in reports if "standard" in (r.get("attributes", {}).get("name") or "").lower()]
    return std or reports


def sync(asc: ASC, request_id: str, already: Callable[[str], bool],
         save: Callable[[str, str, str, dict], None], days: int = 120,
         max_files: int = 600, today: Optional[date] = None) -> dict:
    """Import every daily instance not imported yet. Returns a small summary.

    `already(instance_id)` says whether an instance was imported before;
    `save(instance_id, report_name, processing_date, totals)` stores one.
    """
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    reports = wanted_reports(list(asc.pages(
        f"/v1/analyticsReportRequests/{request_id}/reports", {"limit": 200},
        what="list reports", max_pages=5)))
    imported = files = 0
    for rep in reports:
        name = (rep.get("attributes") or {}).get("name") or rep["id"]
        for inst in asc.pages(f"/v1/analyticsReports/{rep['id']}/instances",
                              {"filter[granularity]": "DAILY", "limit": 200},
                              what="list report days", max_pages=3):
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
            save(inst["id"], name, pdate, totals)
            imported += 1
    return {"reports": len(reports), "imported": imported, "partial": False}


# ── summary ─────────────────────────────────────────────────────────────────

# The headline tiles, in App Store Connect's own order. Each picks its rows by
# report name + dimension + metric, all case-insensitive substrings, because
# Apple's exact wording varies between reports. A tile whose rows aren't there
# shows "—" rather than a wrong number. `up_is_good` colours the change.
KPIS = [
    ("impressions", "Impressions", "discovery|engagement", r"^impression", "^counts$", True),
    ("page_views", "Product page views", "discovery|engagement", r"page ?view", "^counts$", True),
    ("downloads", "Downloads", "download", r"first|re-?download|total|^download", "^counts$", True),
    ("proceeds", "Proceeds (USD)", "purchase|commerce", "", "proceeds", True),
    ("sessions", "Sessions", "session", "", "^sessions$|^counts$", True),
    ("installs", "Installations", "install", r"install", "^counts$", True),
    ("deletions", "Deletions", "install|delet", r"delet", "^counts$", False),
    ("crashes", "Crashes", "crash", "", "^crashes$|^counts$", False),
]


def summarize(rows: list[dict], window: int = 30, today: Optional[date] = None) -> dict:
    """Tiles and per-report tables from stored rows {report, day, dim, metric, value}.

    Windows end at the latest day Apple has reported, not at today: the most
    recent day or two are never in yet, and counting them as zeros would make
    every number look like it just fell off a cliff.
    """
    if not rows:
        return {"kpis": [], "reports": [], "through": None, "window": window}
    last = max(r["day"] for r in rows)
    end = date.fromisoformat(last)
    cur_from = (end - timedelta(days=window - 1)).isoformat()
    prev_from = (end - timedelta(days=2 * window - 1)).isoformat()
    days = [(end - timedelta(days=i)).isoformat() for i in range(window - 1, -1, -1)]

    def match(pat, text):
        return not pat or re.search(pat, text or "", re.I)

    kpis = []
    for key, label, rep_pat, dim_pat, met_pat, up_good in KPIS:
        picked = [r for r in rows if match(rep_pat, r["report"]) and match(dim_pat, r["dim"])
                  and match(met_pat, r["metric"])]
        if not picked:
            kpis.append({"key": key, "label": label, "value": None})
            continue
        # One metric per tile: if several matched (e.g. both "Counts" and
        # "Unique Counts" slipped through a loose pattern) keep the first.
        metric = picked[0]["metric"]
        picked = [r for r in picked if r["metric"] == metric]
        cur = sum(r["value"] for r in picked if r["day"] >= cur_from)
        prev = sum(r["value"] for r in picked if prev_from <= r["day"] < cur_from)
        daily = {d: 0.0 for d in days}
        for r in picked:
            if r["day"] in daily:
                daily[r["day"]] += r["value"]
        kpis.append({"key": key, "label": label, "value": cur, "previous": prev,
                     "change_pct": round((cur - prev) / prev * 100, 1) if prev else None,
                     "up_is_good": up_good,
                     "daily": [{"day": d, "value": daily[d]} for d in days]})

    # Conversion the way App Store Connect frames it: downloads per impression.
    by = {k["key"]: k for k in kpis}
    imp, dl = by.get("impressions") or {}, by.get("downloads") or {}
    if imp.get("value") and dl.get("value") is not None:
        conv = dl["value"] / imp["value"] * 100
        prev = (dl["previous"] / imp["previous"] * 100) if imp.get("previous") else None
        kpis.insert(2, {"key": "conversion", "label": "Conversion rate", "value": conv,
                        "unit": "%", "previous": prev, "up_is_good": True,
                        "change_pct": round(conv - prev, 1) if prev is not None else None,
                        "change_is_points": True})

    # Everything else Apple reported, as plain tables — nothing is dropped.
    reports: dict = {}
    for r in rows:
        if r["day"] < prev_from:
            continue
        t = reports.setdefault(r["report"], {})
        cell = t.setdefault((r["dim"], r["metric"]), {"last7": 0.0, "current": 0.0, "previous": 0.0})
        if r["day"] >= cur_from:
            cell["current"] += r["value"]
            if r["day"] >= (end - timedelta(days=6)).isoformat():
                cell["last7"] += r["value"]
        else:
            cell["previous"] += r["value"]
    tables = [{"report": name, "rows": [
        {"dim": d, "metric": m, **v}
        for (d, m), v in sorted(cells.items(), key=lambda kv: (-kv[1]["current"], kv[0]))]}
        for name, cells in sorted(reports.items())]
    return {"kpis": kpis, "reports": tables, "through": last, "window": window}
