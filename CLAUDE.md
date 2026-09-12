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
  it lives in their browser's localStorage. **Designed for an operator with ADHD**: one
  headline stating the single next action, a shrinking list as the progress bar, zones that
  can't be called folded away, and one primary button per row. Keep it that way — extra
  choices on this screen are a cost, not a feature
- static/icon.png, static/favicon.png — the app logo, copied from the mobile repo's assets and
  served via the allowlisted `/crm/{asset}` route (NOT a directory mount — that would be one
  traversal away from serving the repo). Re-copy from 86d-mobile/assets when rebranding
- leadgen.py — the daily lead generator: harvest (OpenStreetMap/Overpass) → enrich (crawl
  the venue's site for an email) → qualify (drop chains, score) → promote (top N into
  crm_leads each morning). See the LEAD GENERATOR section below
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

## LEAD GENERATOR (leadgen.py)
- Four stages with a persistent pool between qualify and promote. The pool is the point:
  harvesting runs AHEAD of consumption, so an Overpass outage or a slow crawl costs nothing
  that morning — promote draws from the bank. `/v1/crm/leadgen/health` reports
  `days_of_runway`
- Data source is **OpenStreetMap (ODbL)**, not Google Places or Yelp. Those two forbid
  storing place content beyond a short cache window, which is exactly what a persistent
  lead pool does; OSM is legal to keep. It also needs no API key and no billing account
- **Every Overpass mirror in `OVERPASS_MIRRORS` must carry the full planet.**
  `overpass.osm.ch` was removed because it is a European regional mirror: a US query gets
  HTTP 200 and an empty element list, a confident "there are no bars in Austin". That is
  worse than an outage because it looks like success. `_overpass()` therefore treats a
  zero-element response as a miss and tries the next mirror
- A total harvest failure RAISES rather than returning (0, 0) — a silent zero would mark the
  city harvested and look identical to a city with no bars
- Runs interrupted mid-flight (Render free tier spins down constantly) are reconciled to
  `phase='abandoned'` on the next run; the health endpoint warns when several are stuck
- Enrichment runs in a thread pool (`LEADGEN_ENRICH_WORKERS`, default 8) and gives up on a
  site the moment its homepage doesn't load — a dead domain used to cost one request per
  guessed path. It prefers links the homepage actually points at over guessed URLs
- Measured yield: roughly 280 bars per metro → ~17% have both phone and website → ~30-50% of
  those have a findable email ≈ **15-20 qualified leads per city**. Sustaining 25/day needs
  ~1.5 new cities per day; 58 US metros are seeded, more via `POST /v1/crm/leadgen/cities`
- A lead is NEVER promoted without both a phone and an email, and never if it's suppressed,
  already in the pipeline, or already a customer
- **The crawler will not fetch a non-public URL.** `_is_public_http_url()` requires http(s)
  and resolves the host, rejecting private/loopback/link-local/reserved addresses, and every
  venue-supplied fetch passes `verify_public=True`. This matters because the `website` tag
  comes from OpenStreetMap, which anyone can edit: without it, someone could point a bar's
  website at `http://169.254.169.254/` and have this server fetch its own cloud metadata by
  editing a map. DNS rebinding would still defeat it; pinning the resolved IP into the
  request is more machinery than an internal tool warrants
- `EMAIL_BLOCKLIST` rejects site-builder and platform domains (wix.com, squarespace, toasttab,
  resy, yelp…) as well as role addresses. A real harvest returned `wixofday@wix.com` as a
  Portland bar's contact — valid-looking, reaches Wix's marketing team, never the venue. A
  lead nobody can reply to is worse than no lead: it still costs a call slot. Free mailboxes
  (gmail etc.) are deliberately NOT blocked — for a small independent bar they're the norm
- Log lines to grep on Render: `LEADGEN_TABLES_READY`, `LEADGEN_RUN`, `LEADGEN_TABLES_FAILED`

## CRM SALES TOOLING
- `GET /v1/crm/funnel` — signups, trials by days-remaining, trial→paid, **activation**
  (signed up but never finished a first count), pipeline and per-channel win rates
- `GET /v1/crm/queue` — overdue follow-ups, due today, never called, each with a call-window
  hint. Bars are shut mornings and slammed evenings; the window is 2-5pm local, derived
  crudely from longitude (`us_tz_offset`), which is only ever used to label a phone number
- `POST /v1/crm/leads/{id}/touch` — one call that stamps the date, moves the status, sets the
  follow-up, appends a dated note and spends both counters IN ONE TRANSACTION. That
  atomicity is what stops the activity numbers drifting from the pipeline
- `POST /v1/crm/attribution/rematch` — joins crm_leads to users by email, then unique email
  domain, then unique normalized business_name, recording WHICH method matched. Conservative
  on purpose: a wrong attribution points the next 5,000 touches at the wrong city
- `GET/POST /v1/crm/suppressions` — do-not-call. Checked at promote time, so a suppressed
  venue can never re-enter the pipeline through the generator either
- `GET /v1/crm/leadgen/export.csv?scope=today|queue|all` — for an auto-dialer
- Today's call list and the CSV both exclude `won`/`dead`: calling someone who asked not to
  be contacted is the one mistake this list must never cause

## THE CALL LIST (the screen the operator actually lives in)
- `GET /v1/crm/calllist` — every unworked lead, GROUPED BY TIMEZONE, zones ordered so the one
  callable right now is first. Bars are shut mornings and in service from ~5pm local, so as
  the afternoon rolls west Eastern goes "rush" while Central is still good, then Mountain,
  then Pacific. Working west through the zones is the whole point of the grouping
- A lead leaves this list the moment it is touched, debriefed, status-changed or deleted —
  the filter is `status = 'new' AND last_touch_at IS NULL`. That is what makes it impossible
  to call the same restaurant twice, and the shrinking list doubles as the progress bar
- `phone_digits` is on every lead: bare digits, US country code stripped (`+1-615-742-9095`
  → `6157429095`), for pasting into CloudTalk. One click on the page copies it
- `DELETE /v1/crm/leads/{id}` and `POST /v1/crm/leads/bulk-delete` also RETIRE the
  `crm_lead_candidates` row that produced the lead. Without that the generator re-promotes
  the same restaurant on a later run and it reappears — the exact duplicate call that
  deleting it was meant to prevent
- `POST /v1/crm/leads/{id}/debrief` — free-text call notes in, structured fields out
  (status, contact, email, phone, follow-up date, a dated note), applied in one transaction
  along with the counters. Uses the SAME providers as the scan path (OpenAI then Gemini), so
  no new key. Everything the model decided is echoed back in `applied` so a misreading is
  visible immediately. With no provider key it 503s with "type the fields in by hand"
  rather than failing obscurely. Model output is treated as untrusted: `status` is checked
  against VALID_STATUSES, `followup_in_days` is range-checked, and the free-text fields are
  length-capped before they reach a column

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
- CRM_TIMEZONE — optional, zone name the CRM's daily counters roll over in (default UTC).
  Also decides when the daily lead run fires
- LEADGEN_DAILY_TARGET (default 25), LEADGEN_POOL_FLOOR (150), LEADGEN_ENRICH_WORKERS (8),
  LEADGEN_RUN_HOUR (18 = 6pm, local) — optional lead generator tuning. No API key needed: the
  generator uses OpenStreetMap, which has neither keys nor billing
- SENTRY_DSN — optional, error visibility only
- CONFIDENCE_THRESHOLD, LEVEL_DEADBAND — optional tuning, see AI Vision Rules above

## Deploy Rules
- Deployed via Render (see Procfile) — do NOT change without approval
- Requirements are pinned — check compatibility before upgrading
- Cannot push directly to main — always work on a feature branch and open a PR (branch name is assigned
  per session, not fixed — the old hardcoded `claude/build-ios-preview-ASNee` reference here no longer exists)
- NOTE: README.md is outdated (says SQLite) — ignore it, this app uses PostgreSQL
