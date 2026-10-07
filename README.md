# 86'd API

FastAPI backend for **86'd** — AI bar inventory scanning. Point a phone at a bottle, the AI
identifies name, brand and category, the bartender confirms the count, and 86'd matches prices
and builds the distributor order.

**Live on the App Store:** https://apps.apple.com/app/id6798359825 (iPhone only, iOS 15+)
**Website:** https://my86d.com
**Live API:** https://eight6d-api.onrender.com · [`/health`](https://eight6d-api.onrender.com/health)
**Mobile app:** https://github.com/m700devops/86d-mobile

> **`CLAUDE.md` is the authoritative document for this repo.** It carries the architecture, the
> AI vision rules, the CRM design and a long "failure points fixed" log. This README is a map,
> not a spec — when the two disagree, `CLAUDE.md` wins.

## Tech Stack

- **Framework:** FastAPI, Python 3.11.4 (pinned in `.python-version`)
- **Database:** PostgreSQL via `psycopg2` with a threaded connection pool.
  `DATABASE_URL` is **required** — the app raises on startup without it.
- **Auth:** JWT access + refresh tokens (`auth.py`), plus Sign in with Apple (`apple_auth.py`)
- **AI bottle vision:** OpenAI (`OPENAI_MODEL`, default `gpt-4o`, primary) and Google Gemini
  (`GEMINI_MODEL`, default `gemini-3.6-flash`) asked **side by side**, each a second opinion
  on the other. See the AI Vision Rules in `CLAUDE.md`.
- **Billing:** Stripe (`billing.py`), with a webhook at `POST /billing/webhook`
- **Email:** SpaceMail over SMTP/IMAP (`mailer.py`, `inbox.py`); Resend as an alternate sender
- **Deploy:** Render — **Starter plan ($7/mo, 0.5 CPU, 512MB)**, not Free. It does not spin
  down, so there is no cold start to design around. Postgres is on a paid tier separately.

## Pricing (as the code actually behaves)

- **15-day free trial, no credit card.** No card is collected at signup; checkout only opens
  once the trial lapses (`pitch.TRIAL_DAYS = 15`, `main.py` ~3154).
- **$49.99/month** regular (`COMPANY_PRICE`).
- **$29.99/month launch price for the first `LAUNCH_PRICE_SLOTS` (10) real accounts**, via
  `STRIPE_LAUNCH_PRICE_ID`. The 11th account pays the regular price. `GET /v1/billing/price`
  tells the app which one applies. Covered by `test_launch_price.py`.

> ⚠️ **my86d.com currently advertises "30 days free" and "$29.99 / month" to everyone.**
> That does not match this backend (15 days; $29.99 only for the first 10). One of the two
> needs to change — see the note at the bottom of this file.

## Quick Start

The system Python has none of the pinned deps, and this box has no `python3-venv` and no
passwordless `sudo`. Use `uv`:

```bash
uv venv ~/.venvs/86d-api
uv pip install --python ~/.venvs/86d-api/bin/python -r requirements.txt pytest

# DATABASE_URL is mandatory — point it at a local or branch Postgres
export DATABASE_URL="postgresql://user:pass@localhost:5432/86d"
export SECRET_KEY="dev-only-change-me"

~/.venvs/86d-api/bin/python -m uvicorn main:app --reload
# API docs: http://localhost:8000/docs
```

### Tests

```bash
~/.venvs/86d-api/bin/python -m pytest -q
```

57 test files, **1167 tests**, ~2 minutes. Green at `327cf1e` (2026-10-07).

## Layout

`main.py` is a large single-file monolith holding the product API, the AI scan path and the
legal/billing/support pages. The sales side lives in its own modules.

| File | What |
|---|---|
| `main.py` | Product routes, AI scan pipeline, legal pages, Stripe webhook, `/crm` static mount |
| `database.py` | PostgreSQL pool and schema creation |
| `auth.py` / `apple_auth.py` | JWT tokens; Sign in with Apple |
| `models.py` | Pydantic request/response models |
| `helpers.py` | Level classification, ID generation, variance, order generation |
| `billing.py` | Stripe checkout, portal, subscription state |
| `seed_data.py` | 457 seeded master products |
| `scanstats.py` | Scanner report card |

### Internal sales CRM — not part of the product

`crm.py` (~8.1k lines) exposes a `/v1/crm` router with **its own shared-key auth
(`CRM_API_KEY`), its own models and its own `crm_*` tables**. It shares a process and a
database with the product API but is deliberately self-contained — nothing in the
inventory, scan or order paths reads from it.

| File | What |
|---|---|
| `crm.py` | `/v1/crm` router, leads, call list, follow-ups, Apple Analytics routes |
| `static/crm.html` | The whole CRM UI, served at `/crm`. Single self-contained file, no build step. Holds no credentials — the operator types the key and it lives in their browser's localStorage |
| `leadgen.py` | Lead discovery, enrichment, the owner's qualification rules |
| `contacts.py` | Email validation — does an address actually belong to the venue |
| `mailer.py` / `inbox.py` | Sending, bounces, reply handling, opt-outs |
| `apple.py` | Apple Analytics via App Store Connect's Analytics Reports API (connected 2026-09-26) |
| `cloudtalk.py` / `callcoach.py` / `coach.py` | Call import, scoring and per-call coaching |
| `pitch.py` / `playbook.py` / `school.py` | Pitch facts, call playbook, training packs |
| `research.py` / `competitors.py` / `venue.py` / `phones.py` | Prospect research and data quality |

## API

95 routes across two routers: `v1_router` (the product API, `/v1/*`) and `crm_router`
(internal sales). Full interactive list at **`/docs`** — that is generated from the code and
will not drift the way a hand-written list here would.

**Unprefixed:** `GET /` · `GET /health` · `GET /legal/privacy` · `GET /legal/terms` ·
`GET /support` · `GET /billing/success` · `GET /billing/cancel` · `POST /billing/webhook` ·
`POST /admin/activate-account` · `GET /crm`

**Product API** (`/v1/`), grouped:

- **Auth** — `register`, `login`, `refresh`, `forgot-password`, `reset-password`,
  `change-password`, `auth/apple`
- **Users** — `GET`/`PATCH`/`DELETE /users/me`, `users/me/accept-terms`
- **Products** — list, `search`, `barcode/{upc}`, create, `increment-scan`, `merge`
- **Locations** — list, create, patch, `duplicates`, `par-levels` (get/set/bulk),
  `product-distributors`, per-location product overrides
- **Distributors** — list, create, update, delete
- **Inventory** — `start`, `draft` (get/put/delete), get session, `scan`, `scan/bulk`,
  `voice`, `complete`, `cancel`
- **Scans** — `scans/analyze` (the AI vision call), `scans/warm`, `scans/{id}/outcome`
- **Orders** — list, get, `export`, `prepare-emails`, `email`
- **Billing** — `billing/price`, `create-checkout-session`, `create-portal-session`
- **Sync** — `POST /sync` (bulk, offline support), `GET /sync/{location_id}`
- **Events** — `POST /events`

## Environment Variables

`DATABASE_URL` and `SECRET_KEY` are the only two the app cannot run without. ~95 others are
optional and gate individual features; grep `os.getenv` for the full set.

| Variable | Description |
|---|---|
| `DATABASE_URL` | **Required.** PostgreSQL connection string; app raises without it |
| `SECRET_KEY` | **Required.** JWT signing, and derives the Fernet key for Apple's stored `.p8` — rotating it makes that key unreadable and the Apple tab asks to reconnect |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | Primary bottle vision (default `gpt-4o`) |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Second-opinion vision (default `gemini-3.6-flash`) |
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` / `STRIPE_LAUNCH_PRICE_ID` / `STRIPE_WEBHOOK_SECRET` | Billing |
| `LAUNCH_PRICE_SLOTS` | How many accounts get the launch price (10) |
| `COMPANY_PRICE` / `COMPANY_APP_URL` / `COMPANY_WEBSITE` / `COMPANY_PHONE` | Pitch and email facts |
| `CRM_API_KEY` | Shared key for the whole `/v1/crm` surface |
| `SPACEMAIL_*` | Outbound SMTP and inbound IMAP for sales email |
| `CLOUDTALK_KEY_ID` / `CLOUDTALK_KEY_SECRET` | Call import and scoring |
| `APPLE_ISSUER_ID` / `APPLE_KEY_ID` / `APPLE_PRIVATE_KEY` / `APPLE_APP_ID` | Override the saved App Store Connect key. Needs the **Admin** role |
| `SENTRY_DSN` | Error reporting |

## Database Schema

Created on boot by `database.py`. ~43 tables. Product side:

`users` · `locations` · `products` · `product_aliases` · `product_merges` · `distributors` ·
`location_product_distributors` · `par_levels` · `par_levels_backfill_log` ·
`inventory_sessions` · `inventory_drafts` · `scans` · `scan_events` · `scan_outcomes` ·
`voice_notes` · `orders` · `order_sends` · `sync_queue` · `usage_history` · `stripe_events` ·
`app_events`

Internal CRM side — all prefixed `crm_`: `crm_leads` · `crm_counters` · `crm_calls` ·
`crm_touches` · `crm_inbox` · `crm_sent_emails` · `crm_scheduled_emails` · `crm_suppressions` ·
`crm_lead_candidates` · `crm_leadgen_runs` · `crm_apple_metrics` · `crm_coach_hub` ·
`crm_ai_brain` and others.

## Deploy Rules

- Deployed on Render via `Procfile` → `render-start.sh` → `uvicorn main:app`, 1 worker.
  **Do not change without approval.**
- **Cannot push directly to `main`** — work on a feature branch and open a PR. Branch names
  are assigned per session, not fixed.
- **Requirements are fully pinned.** Check compatibility before upgrading anything. `openai`,
  `stripe` and `sentry-sdk` were `>=` until 2026-09-25, and stripe 15 silently broke the
  Stripe webhook. See the failure-points log in `CLAUDE.md`.

## Support

southportai@hotmail.com

## License

Proprietary — © Southport AI Solutions. All rights reserved. No license is granted.

---

### Open question for the owner

The website's pricing does not match the backend's:

| | my86d.com | This backend |
|---|---|---|
| Trial | 30 days free, no card | **15 days** free, no card |
| Price | $29.99/month, "one plan" | **$49.99/month**, $29.99 only for the first 10 accounts |

A bar that signs up from the website expecting 30 days at $29.99 will hit a 15-day trial and,
once the 10 launch slots are gone, a $49.99 charge. Decide which is correct and change the
other — then delete this section.
