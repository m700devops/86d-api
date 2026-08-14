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
- SENTRY_DSN — optional, error visibility only
- CONFIDENCE_THRESHOLD, LEVEL_DEADBAND — optional tuning, see AI Vision Rules above

## Deploy Rules
- Deployed via Render (see Procfile) — do NOT change without approval
- Requirements are pinned — check compatibility before upgrading
- Cannot push directly to main — always work on a feature branch and open a PR (branch name is assigned
  per session, not fixed — the old hardcoded `claude/build-ios-preview-ASNee` reference here no longer exists)
- NOTE: README.md is outdated (says SQLite) — ignore it, this app uses PostgreSQL
