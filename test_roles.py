"""APP_ROLE: one codebase run as the product API, the CRM, or both.

The split exists so the sales tool's crawling can't slow the scanner, and it
must never do the opposite of what it's for: a job running on BOTH services
sends the same email twice or reads the inbox twice, and a product route
missing from the API service is an outage. The role is read at import, so each
role's real app is built in its own interpreter. No database, no network.
"""
import inspect
import json
import os
import subprocess
import sys

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")
if "database" in sys.modules and not hasattr(sys.modules["database"], "init_db"):
    sys.modules["database"].init_db = lambda: None

import main  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.parametrize("value, role", [
    (None, "all"), ("", "all"), ("api", "api"), (" CRM ", "crm"), ("all", "all"),
    ("cmr", "all"),        # a typo runs everything rather than taking the product down
])
def test_read_role(value, role):
    assert main._read_role(value) == role


def test_every_background_job_runs_on_exactly_one_side():
    names = [name for name, _side, _start in main.BACKGROUND_JOBS]
    assert len(names) == len(set(names))
    assert {side for _n, side, _s in main.BACKGROUND_JOBS} == {"api", "crm"}
    api = {n for n, _ in main.jobs_for("api")}
    crm = {n for n, _ in main.jobs_for("crm")}
    assert api == {"warm_providers", "trial_reminders"}
    assert crm == {"leadgen_daily", "scheduled_email", "inbox", "phone_check", "playbook", "school_refresh"}
    assert not api & crm
    assert {n for n, _ in main.jobs_for("all")} == api | crm


def test_every_loop_in_the_file_is_a_listed_job():
    # A loop started any other way would run on both services.
    loops = {name for name, fn in inspect.getmembers(main, inspect.iscoroutinefunction)
             if name.startswith("_") and name.endswith("_loop")}
    listed = " ".join(inspect.getsource(start) for _n, _s, start in main.BACKGROUND_JOBS)
    assert loops and all(loop in listed for loop in loops), loops
    assert inspect.getsource(main.lifespan).count("asyncio.create_task(") == 1


PROBE = r"""
import json, os, sys
sys.path.insert(0, os.environ["HERE"])
import main
from fastapi.testclient import TestClient
paths = sorted({r.path for r in main.app.routes})
client = TestClient(main.app)      # no `with`: the lifespan (database, loops) doesn't run
page = client.get("/crm", follow_redirects=False)
icon = client.get("/crm/icon.png")
print(json.dumps({"paths": paths, "page": page.status_code, "location": page.headers.get("location"),
                  "icon": icon.status_code, "health": client.get("/").status_code}))
"""


def probe(**env):
    full = {**os.environ, "HERE": HERE, "DATABASE_URL": "postgresql://dummy/dummy", **env}
    for key in ("APP_ROLE", "CRM_URL"):
        if key not in env:
            full.pop(key, None)
    out = subprocess.run([sys.executable, "-c", PROBE], env=full, capture_output=True, text=True,
                         timeout=120, cwd=HERE)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_api_role_serves_the_product_and_sends_the_operator_to_the_crm():
    r = probe(APP_ROLE="api", CRM_URL="https://86d-crm.example.com/")
    assert "/v1/scans/analyze" in r["paths"] and "/v1/auth/login" in r["paths"]
    assert not [p for p in r["paths"] if p.startswith("/v1/crm")]
    assert (r["page"], r["location"]) == (307, "https://86d-crm.example.com/crm")
    assert r["icon"] == 404 and r["health"] == 200


def test_api_role_without_a_crm_url_says_where_the_crm_went():
    r = probe(APP_ROLE="api")
    assert r["page"] == 404


def test_crm_role_serves_only_the_crm():
    r = probe(APP_ROLE="crm")
    assert "/v1/crm/leads" in r["paths"] and "/v1/crm/scanner" in r["paths"]
    assert not [p for p in r["paths"] if p.startswith("/v1/") and not p.startswith("/v1/crm")]
    assert (r["page"], r["icon"], r["health"]) == (200, 200, 200)


def test_no_role_is_everything_as_before():
    r = probe()
    assert "/v1/scans/analyze" in r["paths"] and "/v1/crm/leads" in r["paths"]
    assert r["page"] == 200


@pytest.mark.parametrize("role, inits, jobs", [
    ("api", ["db"], {"warm_providers", "trial_reminders"}),
    ("crm", ["crm", "leadgen", "school"],
     {"leadgen_daily", "scheduled_email", "inbox", "phone_check", "playbook", "school_refresh"}),
    ("all", ["db", "crm", "leadgen", "school"], {n for n, _s, _f in main.BACKGROUND_JOBS}),
])
def test_startup_does_only_its_own_half(monkeypatch, role, inits, jobs):
    # Only the API service sets up the product's tables (two services racing the
    # same migrations is how CREATE TABLE IF NOT EXISTS collides) and only the
    # CRM service sets up the CRM's.
    import asyncio
    import types
    ran, started = [], []
    monkeypatch.setattr(main, "APP_ROLE", role)
    monkeypatch.setattr(main, "SERVES_API", role in ("all", "api"))
    monkeypatch.setattr(main, "SERVES_CRM", role in ("all", "crm"))
    monkeypatch.setattr(main, "init_db", lambda: ran.append("db"))
    monkeypatch.setattr(main, "init_crm_tables", lambda: ran.append("crm"))
    monkeypatch.setattr(main, "init_leadgen_tables", lambda: ran.append("leadgen"))
    school = types.ModuleType("school")
    school.init_school_tables = lambda: ran.append("school")
    monkeypatch.setitem(sys.modules, "school", school)

    async def idle():
        return None

    def recorder(name):
        def start():
            started.append(name)
            return idle()
        return start
    monkeypatch.setattr(main, "BACKGROUND_JOBS",
                        [(n, side, recorder(n)) for n, side, _f in main.BACKGROUND_JOBS])

    async def boot():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
    asyncio.run(boot())
    assert ran == inits
    assert set(started) == jobs
