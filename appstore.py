"""App Store Connect numbers for the CRM's App Store tab.

So the operator doesn't have to log in to App Store Connect to see how the app
is doing: downloads per day (Sales and Trends), the live version and recent
builds, and the latest customer reviews — next to our own signup count, which
App Store Connect can't show, so "downloads → accounts" is one screen.

Auth is an App Store Connect API key (Users and Access → Integrations → App
Store Connect API → Team Keys), signed as an ES256 JWT with python-jose, which
is already a dependency via auth.py — no new package. Env:
  ASC_ISSUER_ID, ASC_KEY_ID, ASC_PRIVATE_KEY (the .p8 contents: raw PEM,
  PEM with literal \\n, or base64 of the file) or ASC_PRIVATE_KEY_PATH,
  ASC_VENDOR_NUMBER (Sales and Trends; needed for downloads only),
  ASC_BUNDLE_ID (default com.my86d.app) or ASC_APP_ID.

Daily sales reports never change once Apple publishes them, so each day is
fetched once and kept in `crm_appstore_daily`. Only recent days that Apple
hasn't published yet are re-asked.

Not covered: impressions, product page views and crash counts. Those come from
Apple's separate Analytics Reports API, which has to be requested and then
takes a day or two to start producing files.
"""
import base64
import csv
import gzip
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

API = "https://api.appstoreconnect.apple.com"
BUNDLE_ID = os.getenv("ASC_BUNDLE_ID", "com.my86d.app")

# Product Type Identifier column of the Summary Sales report. Apple's
# reference: 1/1F/1T = a first download (free or paid), 3/3F/3T = a
# re-download, 7/7F/7T = an update. IA*/FI1 are in-app purchases, which this
# app doesn't use (billing is Stripe) but are counted in case that changes.
DOWNLOAD_TYPES = {"1", "1F", "1T", "1E", "1EP", "1EU", "F1"}
REDOWNLOAD_TYPES = {"3", "3F", "3T", "F3"}
UPDATE_TYPES = {"7", "7F", "7T", "F7"}


def missing_config() -> list:
    need = [k for k in ("ASC_ISSUER_ID", "ASC_KEY_ID") if not os.getenv(k)]
    if not (os.getenv("ASC_PRIVATE_KEY") or os.getenv("ASC_PRIVATE_KEY_PATH")):
        need.append("ASC_PRIVATE_KEY")
    return need


def is_configured() -> bool:
    return not missing_config()


def normalize_private_key(raw: str) -> str:
    """The .p8 as PEM, however it was pasted into the dashboard.

    Env var UIs mangle multi-line values: the newlines arrive as a literal
    "\\n", or the whole thing was base64'd to survive. Accept all three."""
    raw = (raw or "").strip().strip('"').strip("'")
    if "\\n" in raw:
        raw = raw.replace("\\n", "\n")
    if "BEGIN" not in raw:
        try:
            decoded = base64.b64decode(raw, validate=True).decode()
            if "BEGIN" in decoded:
                raw = decoded
            else:
                raw = "-----BEGIN PRIVATE KEY-----\n" + raw + "\n-----END PRIVATE KEY-----"
        except Exception:
            raw = "-----BEGIN PRIVATE KEY-----\n" + raw + "\n-----END PRIVATE KEY-----"
    # A PEM pasted onto one line: header, body and footer separated by spaces.
    if raw.count("\n") < 2 and "-----BEGIN" in raw:
        body = raw.replace("-----BEGIN PRIVATE KEY-----", "").replace(
            "-----END PRIVATE KEY-----", "").strip().replace(" ", "\n")
        raw = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    return raw + ("\n" if not raw.endswith("\n") else "")


def _private_key() -> str:
    path = os.getenv("ASC_PRIVATE_KEY_PATH")
    if path:
        with open(path) as fh:
            return normalize_private_key(fh.read())
    return normalize_private_key(os.getenv("ASC_PRIVATE_KEY", ""))


_token_cache = {"token": None, "exp": 0.0}
_token_lock = threading.Lock()


def _token() -> str:
    """A signed JWT, reused until a minute before it expires. Apple caps
    lifetime at 20 minutes."""
    from jose import jwt

    with _token_lock:
        if _token_cache["token"] and _token_cache["exp"] - 60 > time.time():
            return _token_cache["token"]
        now = int(time.time())
        exp = now + 15 * 60
        token = jwt.encode(
            {"iss": os.getenv("ASC_ISSUER_ID"), "iat": now, "exp": exp,
             "aud": "appstoreconnect-v1"},
            _private_key(), algorithm="ES256",
            headers={"kid": os.getenv("ASC_KEY_ID"), "typ": "JWT"})
        _token_cache.update(token=token, exp=float(exp))
        return token


class AppStoreError(Exception):
    pass


def _get(path: str, params: Optional[dict] = None, accept: str = "application/json"):
    resp = httpx.get(API + path, params=params or {}, timeout=30.0,
                     headers={"Authorization": f"Bearer {_token()}", "Accept": accept})
    return resp


def _json(path: str, params: Optional[dict] = None) -> dict:
    resp = _get(path, params)
    if resp.status_code != 200:
        raise AppStoreError(_error_text(resp))
    return resp.json()


def _error_text(resp) -> str:
    try:
        err = resp.json()["errors"][0]
        return f"{resp.status_code} {err.get('title', '')}: {err.get('detail', '')}".strip()
    except Exception:
        return f"{resp.status_code} {resp.text[:160]}"


# ---------------------------------------------------------------- sales

def parse_sales_report(text: str) -> dict:
    """One day's Summary Sales report (tab-separated) → the numbers we show.
    Pure, so it's tested without Apple."""
    out = {"downloads": 0, "redownloads": 0, "updates": 0, "in_app": 0,
           "countries": {}, "devices": {}, "proceeds": {}}
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    for row in reader:
        kind = (row.get("Product Type Identifier") or "").strip()
        try:
            units = int(float(row.get("Units") or 0))
        except ValueError:
            units = 0
        if kind in DOWNLOAD_TYPES:
            out["downloads"] += units
            cc = (row.get("Country Code") or "??").strip()
            out["countries"][cc] = out["countries"].get(cc, 0) + units
            dev = (row.get("Device") or "").strip() or "Unknown"
            out["devices"][dev] = out["devices"].get(dev, 0) + units
        elif kind in REDOWNLOAD_TYPES:
            out["redownloads"] += units
        elif kind in UPDATE_TYPES:
            out["updates"] += units
        elif kind.startswith("IA") or kind == "FI1":
            out["in_app"] += units
        try:
            proceeds = float(row.get("Developer Proceeds") or 0) * units
        except ValueError:
            proceeds = 0.0
        if proceeds:
            cur = (row.get("Currency of Proceeds") or "USD").strip()
            out["proceeds"][cur] = round(out["proceeds"].get(cur, 0.0) + proceeds, 2)
    return out


def _fetch_day(day: date) -> tuple[str, Optional[dict]]:
    """('ok', numbers) | ('none', zeros) | ('pending', None) | ('error', None)."""
    vendor = os.getenv("ASC_VENDOR_NUMBER")
    resp = _get("/v1/salesReports", {
        "filter[frequency]": "DAILY", "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY", "filter[vendorNumber]": vendor,
        "filter[reportDate]": day.isoformat(), "filter[version]": "1_0",
    }, accept="application/a-gzip")
    if resp.status_code == 200:
        raw = resp.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return "ok", parse_sales_report(raw.decode("utf-8", "replace"))
    if resp.status_code == 404:
        msg = _error_text(resp).lower()
        if "not available yet" in msg or "not yet" in msg:
            return "pending", None
        # Apple answers 404 for a day with no sales at all.
        return "none", parse_sales_report("")
    print(f"[appstore] sales {day}: {_error_text(resp)}", flush=True)
    return "error", None


def _ensure_table(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crm_appstore_daily (
            report_date TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """)


def daily_sales(days: int) -> dict:
    """{'YYYY-MM-DD': numbers} for the last `days` days Apple has published.
    Cached per day in the database."""
    from database import get_db

    # Reports are dated in Pacific time and yesterday's lands the next
    # morning, so "today" is never available.
    end = (datetime.now(timezone.utc) - timedelta(hours=8)).date() - timedelta(days=1)
    wanted = [end - timedelta(days=i) for i in range(days)]
    with get_db() as conn:
        cursor = conn.cursor()
        _ensure_table(cursor)
        cursor.execute("SELECT report_date, data FROM crm_appstore_daily WHERE report_date = ANY(%s)",
                       ([d.isoformat() for d in wanted],))
        have = {r["report_date"]: json.loads(r["data"]) for r in cursor.fetchall()}
        conn.commit()

    missing = [d for d in wanted if d.isoformat() not in have]
    fetched = {}
    errors = 0
    if missing:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for d, (state, numbers) in zip(missing, pool.map(_fetch_day, missing)):
                if numbers is not None:
                    fetched[d.isoformat()] = (state, numbers)
                elif state == "error":
                    errors += 1

    if fetched:
        with get_db() as conn:
            cursor = conn.cursor()
            recent = (end - timedelta(days=2)).isoformat()
            for day, (state, numbers) in fetched.items():
                # A "no sales" answer for the last couple of days might just be
                # Apple running late, so only an old empty day is kept for good.
                if state == "none" and day >= recent:
                    continue
                cursor.execute("""
                    INSERT INTO crm_appstore_daily (report_date, data, fetched_at)
                    VALUES (%s, %s, %s) ON CONFLICT (report_date) DO NOTHING
                """, (day, json.dumps(numbers), datetime.now(timezone.utc).isoformat()))
            conn.commit()

    series = {**have, **{d: n for d, (_, n) in fetched.items()}}
    return {"days": series, "errors": errors}


# ---------------------------------------------------------------- the app

def _app() -> dict:
    app_id = os.getenv("ASC_APP_ID")
    if app_id:
        body = _json(f"/v1/apps/{app_id}", {"fields[apps]": "name,bundleId,sku"})
        return {"id": app_id, **body["data"]["attributes"]}
    body = _json("/v1/apps", {"filter[bundleId]": BUNDLE_ID,
                              "fields[apps]": "name,bundleId,sku"})
    if not body.get("data"):
        raise AppStoreError(f"No app with bundle id {BUNDLE_ID} on this account.")
    first = body["data"][0]
    return {"id": first["id"], **first["attributes"]}


def _versions(app_id: str) -> list:
    body = _json(f"/v1/apps/{app_id}/appStoreVersions", {"limit": 4})
    return [{"version": v["attributes"].get("versionString"),
             "state": v["attributes"].get("appVersionState") or v["attributes"].get("appStoreState"),
             "platform": v["attributes"].get("platform"),
             "created": v["attributes"].get("createdDate")} for v in body.get("data", [])]


def _builds(app_id: str) -> list:
    body = _json("/v1/builds", {"filter[app]": app_id, "sort": "-uploadedDate", "limit": 6,
                                "include": "preReleaseVersion",
                                "fields[preReleaseVersions]": "version"})
    versions = {i["id"]: i["attributes"].get("version")
                for i in body.get("included", []) if i.get("type") == "preReleaseVersions"}
    out = []
    for b in body.get("data", []):
        rel = (b.get("relationships", {}).get("preReleaseVersion", {}) or {}).get("data") or {}
        a = b["attributes"]
        out.append({"build": a.get("version"), "version": versions.get(rel.get("id")),
                    "state": a.get("processingState"), "uploaded": a.get("uploadedDate"),
                    "expired": a.get("expired")})
    return out


def _reviews(app_id: str) -> list:
    body = _json(f"/v1/apps/{app_id}/customerReviews", {"sort": "-createdDate", "limit": 20})
    return [{"rating": r["attributes"].get("rating"), "title": r["attributes"].get("title"),
             "body": r["attributes"].get("body"), "who": r["attributes"].get("reviewerNickname"),
             "date": r["attributes"].get("createdDate"),
             "territory": r["attributes"].get("territory")} for r in body.get("data", [])]


def _signups(days: int) -> dict:
    """Our own account signups per day, which Apple can't show — the other
    half of "did the downloads turn into users"."""
    from database import get_db
    from crm import TEST_EMAIL_PATTERN

    since = (datetime.now(timezone.utc) - timedelta(days=days + 1)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT SUBSTRING(CAST(created_at AS TEXT), 1, 10) AS day, COUNT(*) AS n
              FROM users
             WHERE deleted_at IS NULL AND created_at >= %s AND email !~* %s
             GROUP BY 1
        """, (since, TEST_EMAIL_PATTERN))
        return {r["day"]: r["n"] for r in cursor.fetchall()}


_summary_cache: dict = {}
_summary_lock = threading.Lock()


def summary(days: int = 30, refresh: bool = False) -> dict:
    """Everything the tab shows. Each section fails on its own — a key without
    the Sales role still shows reviews and builds, and says why downloads are
    missing, rather than the whole tab going blank."""
    days = max(7, min(days, 180))
    key = days
    with _summary_lock:
        hit = _summary_cache.get(key)
        if hit and not refresh and time.time() - hit[0] < 600:
            return hit[1]

    out: dict = {"configured": True, "days": days, "errors": {},
                 "generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        app = _app()
        out["app"] = app
    except Exception as exc:
        out["errors"]["app"] = str(exc)
        app = None

    if app:
        for name, fn in (("versions", _versions), ("builds", _builds), ("reviews", _reviews)):
            try:
                out[name] = fn(app["id"])
            except Exception as exc:
                out["errors"][name] = str(exc)
                out[name] = []

    if os.getenv("ASC_VENDOR_NUMBER"):
        try:
            sales = daily_sales(days)
            out["daily"] = sales["days"]
            if sales["errors"]:
                out["errors"]["sales"] = (f"{sales['errors']} day(s) couldn't be fetched — "
                                          "check the key has the Sales (or Admin) role.")
        except Exception as exc:
            out["errors"]["sales"] = str(exc)
            out["daily"] = {}
    else:
        out["daily"] = {}
        out["errors"]["sales"] = "Set ASC_VENDOR_NUMBER to see downloads."

    try:
        out["signups"] = _signups(days)
    except Exception as exc:
        out["errors"]["signups"] = str(exc)
        out["signups"] = {}

    out["totals"] = totals(out["daily"], out["signups"], out.get("reviews") or [])
    with _summary_lock:
        _summary_cache[key] = (time.time(), out)
    return out


def totals(daily: dict, signups: dict, reviews: list) -> dict:
    t = {"downloads": 0, "redownloads": 0, "updates": 0, "signups": sum(signups.values()),
         "countries": {}, "proceeds": {}}
    for numbers in daily.values():
        for k in ("downloads", "redownloads", "updates"):
            t[k] += numbers.get(k, 0)
        for cc, n in (numbers.get("countries") or {}).items():
            t["countries"][cc] = t["countries"].get(cc, 0) + n
        for cur, v in (numbers.get("proceeds") or {}).items():
            t["proceeds"][cur] = round(t["proceeds"].get(cur, 0.0) + v, 2)
    t["top_countries"] = sorted(t["countries"].items(), key=lambda kv: -kv[1])[:6]
    t["signup_rate_pct"] = (round(100 * t["signups"] / t["downloads"])
                            if t["downloads"] else None)
    rated = [r["rating"] for r in reviews if isinstance(r.get("rating"), int)]
    t["recent_rating"] = round(sum(rated) / len(rated), 1) if rated else None
    t["recent_reviews"] = len(rated)
    return t
