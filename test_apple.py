"""Apple Analytics: parsing Apple's report files, the import loop, the tiles.

No network and no database: the App Store Connect client is driven through a
fake HTTP object that answers the same paths Apple does, and storage is two
callbacks. What matters here is that Apple's varying report columns are read
without guessing, nothing is imported twice, and a tile with no data says so
instead of showing a wrong number.
"""
import gzip
from datetime import date

import apple


# ── parsing ─────────────────────────────────────────────────────────────────

ENGAGEMENT = (
    "Date\tApp Name\tApp Apple Identifier\tEvent\tPage Type\tSource Type\tDevice\tTerritory\tCounts\tUnique Counts\n"
    "2026-09-20\t86'd\t123\tImpression\tProduct page\tApp Store search\tiPhone\tUS\t100\t80\n"
    "2026-09-20\t86'd\t123\tImpression\tProduct page\tApp Store browse\tiPad\tUS\t20\t15\n"
    "2026-09-20\t86'd\t123\tPage view\tProduct page\tApp Store search\tiPhone\tUS\t30\t25\n"
    "2026-09-21\t86'd\t123\tImpression\tProduct page\tApp Store search\tiPhone\tUS\t1,050\t900\n"
)


def test_parse_reads_gzipped_tsv():
    rows = apple.parse_tsv(gzip.compress(ENGAGEMENT.encode()))
    assert len(rows) == 4 and rows[0]["Event"] == "Impression"


def test_aggregate_splits_by_event_and_sums_every_count_column():
    totals = apple.aggregate(apple.parse_tsv(ENGAGEMENT.encode()))
    assert totals[("2026-09-20", "Impression", "Counts")] == 120      # summed across devices
    assert totals[("2026-09-20", "Impression", "Unique Counts")] == 95
    assert totals[("2026-09-20", "Page view", "Counts")] == 30
    assert totals[("2026-09-21", "Impression", "Counts")] == 1050     # "1,050" read as a number
    # The app's own Apple ID is a number but not a metric.
    assert not any(k[2] == "App Apple Identifier" for k in totals)


def test_aggregate_without_a_split_column_totals_everything():
    tsv = b"Date\tApp Name\tSessions\tTotal Session Duration\n2026-09-20\tx\t5\t300\n2026-09-20\tx\t2\t60\n"
    totals = apple.aggregate(apple.parse_tsv(tsv))
    assert totals[("2026-09-20", "Total", "Sessions")] == 7
    assert totals[("2026-09-20", "Total", "Total Session Duration")] == 360


def test_aggregate_with_no_date_column_gives_nothing_rather_than_guessing():
    assert apple.aggregate([{"Counts": "5"}]) == {}


def test_normalize_key_rebuilds_a_mangled_paste():
    pem = apple.normalize_key("-----BEGIN PRIVATE KEY-----\\nABCD EFGH\\n-----END PRIVATE KEY-----")
    assert pem == "-----BEGIN PRIVATE KEY-----\nABCDEFGH\n-----END PRIVATE KEY-----\n"


# ── the import loop, against a fake Apple ───────────────────────────────────

class _Resp:
    def __init__(self, status, body=None, content=b""):
        self.status_code, self._body = status, body
        self.content = content if body is None else b"x"
        self.text = ""
    def json(self):
        return self._body


class _FakeApple:
    """Answers the App Store Connect paths sync() walks."""
    def __init__(self):
        self.downloads = 0
        tsv = gzip.compress(ENGAGEMENT.encode())
        self.routes = {
            "/v1/analyticsReportRequests/R1/reports": {"data": [
                {"id": "rep-std", "attributes": {"name": "App Store Discovery and Engagement Standard"}},
                {"id": "rep-det", "attributes": {"name": "App Store Discovery and Engagement Detailed"}},
            ]},
            "/v1/analyticsReports/rep-std/instances": {"data": [
                {"id": "inst-new", "attributes": {"processingDate": "2026-09-21"}},
                {"id": "inst-old", "attributes": {"processingDate": "2026-09-20"}},
                {"id": "inst-ancient", "attributes": {"processingDate": "2025-01-01"}},
            ]},
            "/v1/analyticsReportInstances/inst-new/segments": {"data": [
                {"id": "s1", "attributes": {"url": "https://signed.example/s1"}}]},
        }
        self.files = {"https://signed.example/s1": tsv}

    def get(self, url, params=None, headers=None):
        if url in self.files:
            assert headers is None      # pre-signed: no bearer token
            self.downloads += 1
            return _Resp(200, content=self.files[url])
        path = url.replace(apple.ASC_BASE, "")
        if path in self.routes:
            return _Resp(200, self.routes[path])
        return _Resp(404, {"errors": [{"detail": "not found"}]})

    def post(self, url, json=None, headers=None):
        return _Resp(201, {"data": {"id": "R-new"}})


def _client(fake):
    asc = apple.ASC("KEY", "ISSUER", "unused", http=fake)
    asc._auth = lambda: {"Authorization": "Bearer t"}
    return asc


def test_sync_imports_only_new_standard_instances():
    fake = _FakeApple()
    saved = []
    result = apple.sync(_client(fake), "R1",
                        already=lambda i: i == "inst-old",
                        save=lambda *a: saved.append(a),
                        today=date(2026, 9, 24))
    # Detailed report skipped (would double every total), the already-imported
    # day skipped, the too-old day skipped: exactly one instance imported.
    assert [s[0] for s in saved] == ["inst-new"]
    assert saved[0][1] == "App Store Discovery and Engagement Standard"
    assert saved[0][3][("2026-09-20", "Impression", "Counts")] == 120
    assert fake.downloads == 1
    assert result == {"reports": 1, "imported": 1, "partial": False}


def test_ensure_report_request_reuses_an_existing_ongoing_one():
    fake = _FakeApple()
    fake.routes["/v1/apps/APP/analyticsReportRequests"] = {"data": [
        {"id": "R-existing", "attributes": {"accessType": "ONGOING"}}]}
    assert apple.ensure_report_request(_client(fake), "APP", None) == "R-existing"


def test_ensure_report_request_creates_one_when_none_exists():
    fake = _FakeApple()
    fake.routes["/v1/apps/APP/analyticsReportRequests"] = {"data": []}
    assert apple.ensure_report_request(_client(fake), "APP", None) == "R-new"


def test_resolve_app_needs_a_choice_when_the_account_has_several():
    fake = _FakeApple()
    fake.routes["/v1/apps"] = {"data": [
        {"id": "1", "attributes": {"name": "86'd", "bundleId": "com.x.86d"}},
        {"id": "2", "attributes": {"name": "Other", "bundleId": "com.x.other"}}]}
    try:
        apple.resolve_app(_client(fake), None)
        assert False, "should have asked which app"
    except apple.AppleError as e:
        assert "2 apps" in str(e)


def test_a_rejected_key_says_what_to_check():
    fake = _FakeApple()
    fake.get = lambda *a, **k: _Resp(401, {"errors": [{"detail": "bad"}]})
    try:
        _client(fake).get("/v1/apps")
        assert False
    except apple.AppleError as e:
        assert "Issuer ID" in str(e)


# ── the tiles ───────────────────────────────────────────────────────────────

def _rows():
    rows = []
    for i in range(60):
        day = date.fromordinal(date(2026, 9, 21).toordinal() - i).isoformat()
        current = i < 30
        rows += [
            {"report": "App Store Discovery and Engagement Standard", "day": day,
             "dim": "Impression", "metric": "Counts", "value": 10.0 if current else 5.0},
            {"report": "App Store Discovery and Engagement Standard", "day": day,
             "dim": "Impression", "metric": "Unique Counts", "value": 999.0},
            {"report": "App Downloads Standard", "day": day,
             "dim": "First-time download", "metric": "Counts", "value": 1.0},
            {"report": "App Crashes Standard", "day": day,
             "dim": "Total", "metric": "Crashes", "value": 2.0 if current else 1.0},
        ]
    return rows


def test_summary_tiles_compare_against_the_previous_window():
    s = apple.summarize(_rows(), 30)
    k = {x["key"]: x for x in s["kpis"]}
    assert s["through"] == "2026-09-21"
    assert k["impressions"]["value"] == 300 and k["impressions"]["previous"] == 150
    assert k["impressions"]["change_pct"] == 100.0
    assert len(k["impressions"]["daily"]) == 30
    assert k["downloads"]["value"] == 30
    assert round(k["conversion"]["value"], 1) == 10.0          # 30 downloads / 300 impressions
    assert k["crashes"]["value"] == 60 and k["crashes"]["up_is_good"] is False
    assert k["proceeds"]["value"] is None                        # not reported -> "—", never 0


def test_summary_keeps_every_report_row_for_the_tables():
    s = apple.summarize(_rows(), 7)
    names = [t["report"] for t in s["reports"]]
    assert "App Crashes Standard" in names
    eng = next(t for t in s["reports"] if t["report"].startswith("App Store Discovery"))
    assert {(r["dim"], r["metric"]) for r in eng["rows"]} == {
        ("Impression", "Counts"), ("Impression", "Unique Counts")}


def test_summary_of_nothing_is_empty_not_an_error():
    assert apple.summarize([], 30)["kpis"] == []
