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
- Google Gemini for AI bottle vision (primary)
- Anthropic Claude SDK imported and available as fallback

## Key Files
- main.py — all routes and app logic (~2523 lines, single-file monolith)
- database.py — PostgreSQL connection (DATABASE_URL required)
- auth.py — JWT access + refresh tokens
- helpers.py — level classification, ID generation, variance calc, order generation
- models.py — Pydantic request/response models
- seed_data.py — default product catalog
- test_level_classifier.py — unit tests for helpers.py level logic (run: pytest test_level_classifier.py -v)

## AI Vision Rules
- MUST use `gemini-2.0-flash` — **current code at main.py:2295 still says `gemini-1.5-flash`, this is a known bug to fix**
- Model constant: `GEMINI_MODEL = "gemini-2.0-flash"` (update this)
- Env vars: GEMINI_API_KEY or GOOGLE_API_KEY
- Claude models available: claude-sonnet-4-6, claude-3-5-sonnet-20241022, claude-3-haiku-20240307
- Confidence threshold: 0.35 (override via CONFIDENCE_THRESHOLD env var)
- Level deadband: ±0.03 (override via LEVEL_DEADBAND env var)

## Key API Routes (all under /v1)
- POST /auth/register, /auth/login, /auth/refresh
- GET/POST /products, GET /products/search, GET /products/barcode/{upc}
- GET/POST /locations, GET/POST /locations/{id}/par-levels
- POST /inventory/start, GET /inventory/{session_id}, POST /inventory/{session_id}/scan
- POST /inventory/{session_id}/scan/bulk
- POST /scans/pen-capture — primary pen-based level scan
- POST /scans/batch — bulk capture
- POST /inventory/{session_id}/voice — voice notes
- POST /inventory/{session_id}/complete
- GET/POST /distributors — distributor management
- GET /health, GET / (API info), GET /docs

## Environment Variables Required
- DATABASE_URL — PostgreSQL connection string (required)
- GEMINI_API_KEY or GOOGLE_API_KEY — for bottle scanning
- SECRET_KEY — JWT signing key
- CONFIDENCE_THRESHOLD, LEVEL_DEADBAND — optional tuning

## Deploy Rules
- Deployed via Render (see Procfile) — do NOT change without approval
- Requirements are pinned — check compatibility before upgrading
- Cannot push directly to main — push to claude/build-ios-preview-ASNee and PR
- NOTE: README.md is outdated (says SQLite) — ignore it, this app uses PostgreSQL
