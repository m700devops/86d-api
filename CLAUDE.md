# 86'd API

## Project
FastAPI backend for 86'd Mobile — handles auth, inventory, bottle scanning, and AI vision analysis.

## Repos
- Backend: https://github.com/m700devops/86d-api
- Mobile: https://github.com/m700devops/86d-mobile

## Stack
- Python / FastAPI
- PostgreSQL (via psycopg2, requires DATABASE_URL env var)
- Deployed on Render at https://eight6d-api.onrender.com
- Google Gemini for AI bottle vision analysis
- Anthropic Claude SDK also available

## Key Files
- main.py — all routes and app logic (single-file monolith)
- database.py — PostgreSQL connection setup
- auth.py — JWT auth (access + refresh tokens)
- helpers.py — utilities: level classification, ID generation, etc.
- models.py — Pydantic request/response models
- seed_data.py — default product catalog

## AI Vision Rules
- MUST use `gemini-2.0-flash` (NOT 1.5) — current code has `GEMINI_MODEL = "gemini-1.5-flash"` which is wrong
- Env vars: GEMINI_API_KEY or GOOGLE_API_KEY
- Anthropic SDK is imported but Gemini is primary for image analysis

## Environment Variables Required
- DATABASE_URL — PostgreSQL connection string (required, app will crash without it)
- GEMINI_API_KEY or GOOGLE_API_KEY — for bottle scanning
- SECRET_KEY — JWT signing key

## Deploy Rules
- Deployed via Render (see Procfile)
- Do NOT change the Procfile without explicit approval
- Requirements are pinned — check compatibility before upgrading packages

## API Base
- Version prefix: /v1
- Health check: GET /health
- Docs: /docs (FastAPI auto-generated)
