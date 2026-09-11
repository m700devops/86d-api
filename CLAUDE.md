# 86'd API

## Project
FastAPI backend for 86'd Mobile — handles auth, inventory, bottle scanning, and AI vision analysis.

## Repos
- Backend: https://github.com/m700devops/86d-api
- Mobile: https://github.com/m700devops/86d-mobile

## Stack
- Python / FastAPI (single-file monolith: main.py)
- PostgreSQL via psycopg2 (requires DATABASE_URL — app crashes without it)
- Deployed on Render at https://eight6d-api.onrender.com
- OpenAI GPT-4o for AI bottle vision (primary)
- Google Gemini 2.0 Flash as fallback if OpenAI is down/rate-limited/times out

## Key Files
- main.py — all routes and app logic (~3690 lines, single-file monolith)
- database.py — PostgreSQL connection (DATABASE_URL required)
- auth.py — JWT access + refresh tokens
- helpers.py — level classification, ID generation, variance calc, order generation
- models.py — Pydantic request/response models
- crm.py — internal sales CRM: `crm_leads` + `crm_counters` tables, a `/v1/crm` router, and
  its own shared-key auth. Deliberately self-contained (own models, own auth, own tables) —
  it shares a process and a database with the product API but is not part of the product.
  Nothing in the inventory/scan/order paths reads from it. See the CRM section below
- static/crm.html — the CRM UI, served at `/crm`. Single self-contained file, no build step;
  replacing this file replaces the UI. Holds no credentials — the operator types the key and
  it lives in their browser's localStorage
- seed_data.py — default product catalog
- test_level_classifier.py — unit tests for helpers.py level logic (run: pytest test_level_classifier.py -v)

## AI Vision Rules
- `POST /v1/scans/analyze` (main.py:3590) tries OpenAI first, falls through to Gemini on timeout/error —
  see `_run_providers()` at main.py:3528
- Model constants (main.py:3206-3207): `OPENAI_MODEL = "gpt-4o"`, `GEMINI_MODEL = "gemini-2.0-flash"`
- Env vars: `OPENAI_API_KEY` (primary), `GEMINI_API_KEY` or `GOOGLE_API_KEY` (fallback — also works alone
  if OPENAI_API_KEY is unset)
- No Anthropic/Claude SDK anywhere in this file — if you're adding a third vision provider, don't assume
  one is already wired up
- Confidence threshold: 0.35 (override via CONFIDENCE_THRESHOLD env var) — below this, `needs_rescan=True`
- Level deadband: ±0.03 (override via LEVEL_DEADBAND env var) — hysteresis for `classify_level()` in
  helpers.py; legacy from the old level-bucketing flow, still live server-side for `/inventory/*/scan` even
  though the mobile client no longer does pen-based level capture

## Key API Routes (all under /v1)
- POST /auth/register, /auth/login, /auth/refresh
- GET/POST /products, GET /products/search, GET /products/barcode/{upc}
- POST /products/{product_id}/merge — merges a duplicate product into a target (aliases, par_levels, distributors)
- GET/POST /locations, GET/POST /locations/{id}/par-levels
- PATCH /locations/{location_id}/products/{product_id} — upserts the `par_levels` row for
  one bottle at one bar (full / current_stock / par / price), preserving whatever the body
  doesn't mention. This is the per-bar memory behind the mobile product book: par and price
  set once here, read back by `GET /locations/{id}/par-levels` on every later count.
  `par_quantity = 0` means "nobody has set a par yet" — the same convention price uses —
  so a PATCH without `par` never invents one (that also stops `generate_order_items`, which
  iterates par_levels rather than scans, from emitting a phantom order line for a bottle
  nobody has parred). `par_levels.par_set_at` is stamped only when a request actually
  carries a `par`, and only by THIS route — the older `POST /par-levels`, its bulk variant
  and the sync route set pars without stamping it, so treat the column as informational and
  keep using `par_quantity > 0` as the "this bar set a par" signal. Adding that column in
  `init_db()` is also the one-shot gate for the backfill that cleared the placeholder pars
  of 1 this endpoint used to create (see database.py)
- GET/POST /locations/{id}/product-distributors — the other half of that memory: which
  distributor a bottle is ordered from at this bar, set once and applied to every future scan
- POST /inventory/start, GET /inventory/{session_id}, POST /inventory/{session_id}/scan
- POST /inventory/{session_id}/scan/bulk
- POST /scans/analyze — the live AI vision route (OpenAI → Gemini fallback), see AI Vision Rules above
- POST /scans/warm — best-effort provider warm-up, fire-and-forget, never raises
- POST /inventory/{session_id}/voice — voice notes
- POST /inventory/{session_id}/complete
- GET/POST /distributors — distributor management
- POST /billing/create-checkout-session — Stripe hosted checkout (no IAP, checkout happens in system browser)
- GET /health, GET / (API info), GET /docs

(There is no `/scans/pen-capture` or `/scans/batch` route — those were removed along with pen-based level
capture. Don't reintroduce them or describe them as current.)

## CRM (internal sales tool)
- `GET /crm` — the UI. Unauthenticated on purpose (it's where the key gets entered), served
  `noindex, nofollow` + `no-store`. 404s if `static/crm.html` is missing
- All `/v1/crm/*` endpoints require an `X-CRM-Key` header matching the `CRM_API_KEY` env var,
  compared with `secrets.compare_digest`. **An unset `CRM_API_KEY` makes every CRM endpoint
  503, never open** — "no key configured" must never mean "no check"
- Routes: `GET/POST /v1/crm/leads`, `PATCH/DELETE /v1/crm/leads/{id}`,
  `POST /v1/crm/leads/{id}/email-sent`, `GET/PATCH /v1/crm/counters`
- `crm_counters` is a single row pinned to `id = 1` by a CHECK constraint — the counters are
  one global scoreboard and a second row would silently become a second truth
- Daily counters reset to their `*_quota` columns on the first request of a new calendar day,
  decided under `SELECT ... FOR UPDATE` so two morning requests can't both apply the reset.
  The day is measured in `CRM_TIMEZONE` (default UTC — on Render that rolls over at 7pm US
  Eastern, mid-shift, so set it to a real zone). The lifetime touch ticker and download count
  are NOT touched by a daily reset
- `PATCH /v1/crm/counters` takes both absolutes (`touch_ticker_remaining`) and deltas
  (`touch_ticker_delta`). Prefer deltas: they apply relative to the stored value so two tabs
  can't clobber each other, and they clamp at 0 rather than going negative
- Lead status is validated by a Pydantic `Literal`, not a DB CHECK — pipeline stages change,
  and a Literal is a deploy where a CHECK is a migration
- `init_crm_tables()` runs from main.py's lifespan in its own try, so a CRM schema failure can
  never stop the product API booting. **It logs `[crm] CRM_TABLES_READY tables=[...]` on
  success and `[crm] CRM_TABLES_FAILED` on failure** — grep Render's deploy logs for
  `CRM_TABLES` to confirm the schema landed
- NOTE: the global CORS config (main.py ~line 110) allows neither the `PATCH` method nor the
  `X-CRM-Key` header. That's fine while the page is served from `/crm` on this same origin
  (same-origin requests skip CORS entirely), but hosting the CRM page on another domain and
  calling this API cross-origin would fail on both counts

## Environment Variables Required
Source of truth: the `_config_checks` startup list in main.py (~line 52) — it logs what's missing on boot.
- DATABASE_URL — PostgreSQL connection string (required, app crashes without it)
- SECRET_KEY — JWT signing key (CRITICAL if left at the default — anyone can forge login tokens)
- OPENAI_API_KEY — primary bottle-scan provider; without it, scanning falls straight to Gemini
- GEMINI_API_KEY or GOOGLE_API_KEY — fallback bottle-scan provider; without it, no fallback if OpenAI fails
- RESEND_API_KEY — order emails and password resets cannot send without it
- STRIPE_SECRET_KEY — checkout/billing endpoints 503 without it
- STRIPE_PRICE_ID — checkout endpoint 503s without it, nobody can subscribe
- STRIPE_WEBHOOK_SECRET — without it, payments don't activate subscriptions (customers pay and stay locked out)
- CRM_API_KEY — shared key for `/v1/crm/*`; unset means every CRM endpoint 503s (the UI at
  `/crm` still loads, it just can't do anything). Not used by the mobile app at all
- CRM_TIMEZONE — optional, zone name the CRM's daily counters roll over in (default UTC)
- SENTRY_DSN — optional, error visibility only
- CONFIDENCE_THRESHOLD, LEVEL_DEADBAND — optional tuning, see AI Vision Rules above

## Deploy Rules
- Deployed via Render (see Procfile) — do NOT change without approval
- Requirements are pinned — check compatibility before upgrading
- Cannot push directly to main — always work on a feature branch and open a PR (branch name is assigned
  per session, not fixed — the old hardcoded `claude/build-ios-preview-ASNee` reference here no longer exists)
- NOTE: README.md is outdated (says SQLite) — ignore it, this app uses PostgreSQL
