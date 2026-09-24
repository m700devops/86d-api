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
- static/crm.html — the CRM UI, served at `/crm`. **Three tabs only** — Call list, CRM,
  Follow-ups — with School, Apple Analytics and Customers behind a burger top right:
  those are looked at occasionally and thought about once, and in the tab row they competed
  with the three things a working day actually needs. The burger turns orange when the open
  page lives inside it. Single self-contained file, no build step;
  replacing this file replaces the UI. Holds no credentials — the operator types the key and
  it lives in their browser's localStorage. **The Call list tab is one button and one table,
  nothing else.** It used to carry a focus card, a clock, a queued-email banner, an undo
  banner, and service/timezone tabs above the table — all of that was decision-making the
  operator had already done by pressing the button. Pressing "Ready to start calling" now
  does exactly what it says: fetches whoever is in a calling window this minute
  (`GET /v1/crm/now`) and lists only that, in one excel-style table. Nobody in a window right
  now gets a one-line status instead of a table, not a wall of leads that aren't callable yet.
  Undo still works — the 10-second Undo on the toast after every logged call — it just isn't
  a permanent banner anymore
- **The burger holds School, Apple Analytics and Customers.** Numbers (funnel, connect rate by
  hour, attribution re-match) and Lead engine (run now, bank health, restaurant recheck) were
  removed from the PAGE at the operator's request; every endpoint behind them is still live
  (`/funnel`, `/dialstats`, `/attribution/rematch`, `/leadgen/health`, `/leadgen/run`,
  `/leadgen/recheck-restaurants`), and the daily 6pm run and the Call list's empty-list
  auto-fill still keep leads coming without anyone opening a panel
- apple.py — **Apple Analytics**: App Store Connect's App Analytics (impressions, product
  page views, conversion, downloads, proceeds, sessions, installs, deletions, crashes) via
  Apple's **Analytics Reports API**. There is no "give me the dashboard" call: the app gets
  ONE ongoing report request (`ensure_report_request`, reused if it exists — Apple allows one
  per app), Apple then produces a daily INSTANCE per report as gzipped TSVs behind pre-signed
  URLs (downloaded WITHOUT the bearer token), and `sync()` imports each instance once
  (`crm_apple_instances`) into `crm_apple_metrics (report, day, dim, metric, value)`. Only
  "Standard" reports — the "Detailed" variants hold the same numbers split finer and would
  double every total. Report columns vary, so `aggregate()` never assumes them: a column is
  a metric if its name says it counts something, and the numbers split by the first present
  of `DIM_PREFERENCE` (Event, Download Type, …). **The first reports take Apple about 1–2
  days after connecting**; the tab says so instead of looking broken. `summarize()` ends its
  windows at the LATEST day Apple has reported, not today — the newest day or two are never
  in yet and would read as a collapse. A tile whose rows Apple didn't report shows "—", never
  0. Auth is an ES256 JWT (python-jose, already a dependency) from the team key's Issuer ID,
  Key ID and .p8. Routes in crm.py: `GET /v1/crm/apple` (status + tiles + tables; starts a
  background import when data is older than `APPLE_STALE_HOURS`), `POST /apple/connect`
  (checks the key against Apple BEFORE saving, so a typo fails with Apple's reason),
  `/apple/sync`, `/apple/disconnect`. The .p8 is stored Fernet-encrypted with a key derived
  from `SECRET_KEY` and never returned to the page; rotating `SECRET_KEY` makes it unreadable
  and the tab asks to reconnect. Env vars `APPLE_ISSUER_ID` / `APPLE_KEY_ID` /
  `APPLE_PRIVATE_KEY` (+ optional `APPLE_APP_ID`) override the saved key. The key needs the
  **Admin** role, because creating the report request does. Covered by test_apple.py; grep
  Render logs for `APPLE_SYNC`
- static/icon.png, static/favicon.png — the app logo, copied from the mobile repo's assets and
  served via the allowlisted `/crm/{asset}` route (NOT a directory mount — that would be one
  traversal away from serving the repo). Re-copy from 86d-mobile/assets when rebranding
- callwindow.py — parses OSM `opening_hours` and decides when to ring THIS venue. Pure
  functions of (hours string, local now), so it's testable without a DB, network or clock.
  Also owns the BUCKET definition — `service_of()`, `bucket_of()`, `all_buckets()` — the
  (service × timezone) cells the page, the API and the generator all have to agree on. One
  definition on purpose: three copies would drift and the tabs would stop matching what gets
  generated. See THE CALL LIST below for the heuristic
- mailer.py — sending from the real Spacemail mailbox over SMTP. **Spacemail has no API and
  doesn't need one: it speaks SMTP**, which is what every mail client uses, so this is stdlib
  `smtplib` only — no new dependency. `mail.spacemail.com:465` SSL (587 STARTTLS also works),
  username is the FULL email address, password is the mailbox password. Deliberately NOT the
  path order confirmations use — those stay on Resend in main.py, because mixing
  transactional mail with cold outreach on one reputation means a few spam complaints from
  strangers start bouncing customers' receipts
- venue.py — what's true about a bar, for the thirty seconds before you dial: cuisine, size,
  hours (a volume proxy), how long it's been open, address. Every fact is EXTRACTED from
  either the harvested OSM tags or the venue's OWN site text, and **carries its source** —
  `facts_to_lines()` reads the source off the fact rather than assuming it. A year from the
  map is phrased "map lists it opening 2025", a year from their site "open since 1974",
  because OSM's `start_date` is often a survey date and asserting it on a call is the moment
  they decide you're reading a script. Covered by test_venue.py
- contacts.py — what makes a contact worth having. `find_manager()` (a name to ask for),
  `email_kind()` (personal / owner / role / unknown), and `EMAIL_BLOCKLIST`. The blocklist
  lives here rather than in leadgen.py so it can be tested without a database — leadgen
  imports `database`, which raises at import time without `DATABASE_URL`. A manager name is NEVER verified and cannot be — managers turn over constantly,
  so a name is only taken when a ROLE WORD sits next to it, the page and date are stored
  with it, and the UI says "Ask if X is still the GM" rather than "ask for X". Measured: 1
  usable name in 22 reachable bar sites. Missing one costs nothing; inventing one costs the
  call. See test_contacts.py
- phones.py — strict NANP validation, fails closed. OSM phone tags are volunteer free text
  carrying extensions, two numbers in one field, international numbers and vanity spellings;
  anything this can't prove dialable returns None and is never promoted. It can promise the
  digits are a structurally valid US number, NOT that the line still belongs to that venue —
  nothing short of dialling proves that
- leadgen.py — the daily lead generator: harvest (OpenStreetMap/Overpass) → enrich (crawl
  the venue's site for an email) → qualify (drop chains, score) → promote (top N into
  crm_leads each morning). See the LEAD GENERATOR section below
- coach.py — cold-call PRACTICE, opened via **School** in the burger menu (`data-panel`
  section, same as Apple Analytics/Customers — it used to live inline in the Call list
  tab behind a "Warm up first" button, which put practice above the actual dial list; moving
  it behind the menu is what let the Call list tab shrink to one button and one table).
  Prompts for the drills and for "The Holdout", a game where
  Claude plays a tough bar owner with hidden problems. Pure: `apply_turn()` is the referee
  that clamps the meters and only lets the owner say yes once trust >= 70 and two
  problems have been found. Left to itself the model agrees far too easily. Routes are
  `/v1/crm/coach/*` in crm.py, using the same `_ask_claude()` helper. Practice scores
  live in the operator's browser (localStorage `crmPractice`), not the database. See
  test_coach.py. Content ROTATES every 3 days, seeded from the local date in the page
  (guests from `coach.GUESTS`, a house rule from `coach.CHALLENGES`, 10 of 30 test
  questions, research, videos, Gauntlet rounds). Every AI path has an offline fallback:
  the Gauntlet game and the drill's offline deck need no server at all. Tape Doctor (`/coach/tape`) plants exactly 3
  rep mistakes in a generated call; `validate_tape()` rejects any tape that can't be scored
  fairly and `tape_score()` charges −25 per false accusation vs +40 per find. Three built-in
  tapes in the page cover the no-AI case
- school.py — the practice school's REFRESH. Every `SCHOOL_EVERY_DAYS` (3) days at
  `SCHOOL_RUN_HOUR` (10) in `SCHOOL_TZ` (Asia/Manila — the operator is in Iloilo and asleep
  then), `_school_refresh_loop()` in main.py runs `refresh_if_due()`: search YouTube with
  use-case queries (rotated per run), keep 3–21 min embeddable videos not shown in the last
  4 packs, have Claude vet them against the bar-owner use case from title/channel/
  description (it can't watch them — the UI says so), and write fresh Gauntlet rounds and
  test questions. Saved to `crm_school_packs`; `GET /v1/crm/coach/school` serves the latest
  good one, `POST /coach/school/refresh` forces a run. Optional `YOUTUBE_API_KEY` switches
  search from the public results page to the Data API (exact durations + embeddable flag).
  A pack must fill 4+ call steps to replace the previous one; otherwise the last good pack
  (or the page's built-in library) stays. Grep Render logs for `SCHOOL_REFRESH`.
  On a free-tier service that's spun down at 10am, the refresh runs when it next wakes.
  See test_school.py
- seed_data.py — default product catalog
- test_level_classifier.py — unit tests for helpers.py level logic
- test_phones.py, test_callwindow.py, test_timezones.py, test_contacts.py — the phone
  validator, call-window/service-band logic, timezone assignment, and manager/email
  classification, all pure. Run them: `pytest test_level_classifier.py test_phones.py
  test_callwindow.py test_timezones.py test_contacts.py test_venue.py test_callnow.py
  test_leadgen.py test_quick_add.py test_coach.py test_school.py test_ask.py
  test_apple.py -q`
- test_leadgen.py — `_restaurant_pours()`, the restaurant liquor gate, pure (crawled text +
  OSM tags in, a yes/no and a reason out). Stubs `database` in `sys.modules` the same way
  test_callnow.py stubs it for crm
- test_callnow.py — calling mode's bucketing, ordering and headlines. Stubs `database` in
  `sys.modules` (crm imports it, and it raises without `DATABASE_URL`) and stubs
  `_call_window` per row, so the tests don't depend on the real clock — callwindow's own
  logic is test_callwindow.py's job
- test_quick_add.py — `_apply_call_notes()` (the write path `/debrief` and `/leads/quick-add`
  share) and `quick_add_lead()` itself. Stubs `database` the same way test_callnow.py does,
  and also fakes the cursor: real Postgres tolerates a `sets`/`params` list falling out of
  lockstep by raising loudly, but nothing here would have caught a silent mismatch without
  something enforcing "every `%s` has exactly one param" and applying each write onto a row
  dict so assertions can check what actually got saved, not just that nothing raised

## AI Vision Rules
- `POST /v1/scans/analyze` (main.py:3590) tries OpenAI first, falls through to Gemini on timeout/error —
  see `_run_providers()` at main.py:3528
- Model constants are ENV-OVERRIDABLE: `OPENAI_MODEL` (default `gpt-4o`), `GEMINI_MODEL`
  (default `gemini-3.6-flash`). They are env vars because a provider can retire a model out from
  under the app and it fails SILENTLY — `gemini-2.0-flash` was retired and every scan ran with no
  fallback until a boot log was read. `_warm_providers()` now prints a loud
  `[warm] WARNING: configured provider(s) NOT available` line for exactly this case; grep the
  Render boot log for `[warm]` after any deploy. Swapping a retired model is a dashboard edit,
  not a deploy
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
- **The cap is PER TAB, not global: `LEADGEN_BUCKET_TARGET` (50) unworked leads in each of
  the 8 (service × timezone) cells.** It used to be a single `LEADGEN_MAX_ACTIVE` of 100, and
  that number cannot survive the tabs: 100 spread over 8 cells averages 12, so opening
  "lunch → Eastern" showed a nearly empty screen. `MAX_ACTIVE` still exists but is DERIVED
  (`BUCKET_TARGET × 2 services × 4 zones` = 400) and is not an independent knob — a global
  number disagreeing with the per-cell one would starve some tabs to fill others
- **Promotion is per-cell, emptiest first** (`promote_leads` → `bucket_deficits`). Score still
  decides WHO gets promoted within a cell; it no longer decides which cells get filled. Taking
  the global top-N by score was measured filling Pacific to 131 while Eastern sat at 12
- **City selection follows the same deficit** (`_next_cities`). The seed list is ordered
  roughly by population, which put most Eastern metros at the back; sorting by the zone's
  shortfall first is what actually feeds a thin tab. Never-harvested breaks the tie, then
  oldest, so a short zone rotates through its own cities instead of re-harvesting one forever
- `DAILY_TARGET` (25) is now a PACE, not a ceiling. A run promotes up to the total shortfall:
  metering a cold start of 400 out at 25/day would leave the tabs unusable for a fortnight. In
  steady state the two coincide anyway, because the per-cell cap means only as many leads can
  land as were called off the list
- When every cell is full AND the bank is at floor, the run returns immediately having made
  ZERO network calls. It still harvests when the bank is thin OR when what's banked can't
  reach the cells that are actually short — a deep bank of Pacific candidates is still an
  empty Eastern tab
- NOTE: none of this spends API credits — OpenStreetMap and Nominatim are free and keyless.
  The cap exists because an ever-growing list is one nobody opens, and to stop pointless
  crawling. The only paid call in the CRM is `/debrief`, once per call the operator logs
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
- Runs interrupted mid-flight are reconciled to `phase='abandoned'` on the next run; the
  health endpoint warns when several are stuck. Deploys and restarts are the cause, NOT a
  sleeping instance — the web service is on Render's **Starter** plan, which does not spin
  down. (Three runs did sit at `phase='running'` forever, but that was the harvest gate
  opening every run with a dozen Overpass sweeps, not the host. See the harvest note above.)
- Enrichment runs in a thread pool (`LEADGEN_ENRICH_WORKERS`, default 8) and gives up on a
  site the moment its homepage doesn't load — a dead domain used to cost one request per
  guessed path. It prefers links the homepage actually points at over guessed URLs
- **The harvest takes `restaurant` as well as `bar`/`pub`/`nightclub`.** It used to take the
  three drink-led types only, which is a small slice of the places that pour: an independent
  restaurant with a licence has a back bar to count exactly like a tavern does, and in OSM it
  is `amenity=restaurant`. The cost is that most restaurants have no bar worth calling, so a
  restaurant must SHOW a drinks programme on its own site (`LIQUOR_HINTS`, or a `bar=yes`
  tag) before it can qualify — `_restaurant_pours()`, covered by test_leadgen.py. Harvesting
  is cheap; promoting is what matters
- **`LIQUOR_HINTS` is anchored, not bare words, after a real harvested pizzeria with zero
  alcohol reached the call list through it.** Bare `cocktail` matched "shrimp cocktail" and
  "fruit cocktail" on a kitchen menu, `bar menu` matched "salad bar menu", `spirits` (no word
  boundary) matched "spirited", `shots?\b` (no LEADING boundary) matched "screenshot", and
  bare `draft`/`happy hour` matched an NFL-watch-party page or a lunch special — none of which
  mean the venue pours. Every phrase now requires something a kitchen-only site has no reason
  to say (`full bar`, `craft cocktail menu`, `wine list`, a named liquor, `draft beer` rather
  than bare `draft`, …). `NO_LIQUOR_HINTS` (byob, "we do not serve alcohol", "no liquor
  license") is checked FIRST and overrides everything else, including an OSM `bar=yes` tag —
  a mapper's edit can be stale, a venue is not wrong about its own liquor license
- **`recheck_restaurant_leads()` is the one-time correction for rows the OLD gate let
  through.** Tightening `LIQUOR_HINTS` only changes what NEW candidates do from here on —
  restaurants already banked (`status='qualified'`) or already promoted-but-never-called sit
  on data collected under the old rule. Unlike `_reconcile_bad_emails()`/
  `_reconcile_timezones()` this can't be recomputed from stored columns alone (the crawled
  HTML isn't kept), so it re-crawls each restaurant row's site and re-applies
  `_restaurant_pours()`. Deliberately NOT run on every boot — re-fetching every restaurant
  candidate's site is exactly the kind of network-heavy work the harvest cap exists to avoid
  doing needlessly — so it's a manual action: `POST /v1/crm/leadgen/recheck-restaurants`
  (poll the same path with GET), background-threaded like `/leadgen/fill` but under its own
  lock (the Lead engine panel that had a button for it was removed from the page — call the
  endpoint directly). It never touches a lead someone has already
  called or logged — only banked candidates and promoted-but-`last_touch_at IS NULL` leads,
  which it deletes the same way the operator's own Delete button does (retiring the
  candidate too, so the generator can't re-promote the same venue tomorrow)
- **`_seed_cities()` runs on EVERY boot**, not only into an empty table. It was gated on "no
  cities yet", which meant adding metros to `SEED_CITIES_EXTRA` did nothing at all to a
  database that already had the first batch — forty cities that would have silently never
  arrived. The insert is `ON CONFLICT DO NOTHING`, so re-running is free
- Territory is sized against consumption: at ~12 qualified leads per metro from bars alone,
  58 cities was about a month of calling before the well ran dry, and running dry is silent.
  87 cities plus restaurants is roughly four months
- Measured yield: roughly 280 bars per metro → ~17% have both phone and website → ~30-50% of
  those have a findable email ≈ **15-20 qualified leads per city**. Sustaining 25/day needs
  ~1.5 new cities per day; 58 US metros are seeded, more via `POST /v1/crm/leadgen/cities`
- A lead is NEVER promoted without both a phone and an email, and never if it's suppressed,
  already in the pipeline, or already a customer
- **The name+city duplicate check compares against `loc` ("City, ST"), not `city`.** It used
  to pass the bare city, so `'portland' = 'portland, or'` never matched and the name half of
  the check never fired once — only the email half did any work, and a venue whose published
  address changed between harvests came straight back onto the list. That is the exact
  duplicate call the check exists to prevent
- **Scoring favours venues LIKELY TO STILL COUNT BY HAND.** `POS_STACK_HINTS` (Resy,
  OpenTable, Tock, SevenRooms, Toast) is the strongest negative: a venue taking bookings
  through a platform is running a stack that probably came with something claiming to do
  inventory. `UPSCALE_HINTS` (tasting menu, sommelier) is a gentler one, same as
  `ASIAN_CUISINE_HINTS` (read straight off the OSM `cuisine` tag, no crawl needed) — per
  Stephan's own sales experience, an Asian restaurant runs a materially higher rate of
  already having some system in place. `_on_tourist_strip()` is the same idea again, from a
  fourth signal: an address on a curated list of tourist strips (Las Vegas Blvd, Lower
  Broadway, Bourbon St, ...) keyed by `(city, street)` so "Broadway" only counts against
  Nashville, not the dozen other seeded metros with an ordinary street by that name.
  `NEIGHBOURHOOD_HINTS` (pool table, happy hour, dive, tavern) is the positive. None of them
  EXCLUDE anything — a fine-dining room, a sushi bar, or a Broadway honky-tonk can still be
  on a clipboard and stays on the list; they only decide order, which is what matters when
  fifty names are in front of you
- A personal mailbox (`dave@divebar.com`) scores +4 and a named manager +5: both mean the
  call has somewhere to land, and both are rare enough to be worth putting first
- **An email with no recorded `email_source` is never promoted, whatever it looks like.**
  Every address this pipeline produces records the page it was read off; one that doesn't
  was never crawled, so nothing can vouch for it. Fourteen rows in a test database carried a
  `bank<uuid>@test.com` with a NULL source and a NULL `enriched_at` and still reached the top
  of the call list, because "has an email" was the only test in the way. The blocklist
  catches shapes we've seen; this catches the ones we haven't
- **`extract_emails()` ignores `<script>`, `<style>` and HTML comments** (`_NON_CONTENT_RE`).
  Addresses in there were written by a developer or a library, never the venue — a real bar's
  homepage carries a jQuery message reading "Please use the format email@example.com", and
  widget config is where machine-shaped addresses come from. `mailto:` hrefs are still read
  from the raw HTML: that's an address a human deliberately published
- `TEMPLATE_EMAILS` / `MACHINE_LOCAL` (in contacts.py) block stock website placeholders
  (`your@email.com`, `mymail@mailservice.com`) and machine-generated addresses. These matter
  more than they look: a placeholder reads as a PERSONAL mailbox, so it sorted to the TOP of
  the call list and reached nobody
- `_reconcile_bad_emails()` runs every boot beside `_reconcile_timezones()`. It strips
  unsourced/blocklisted addresses from candidates (back to `status='new'` so the next run
  re-crawls them) and from leads, and re-runs `email_kind()` on every lead so a row
  classified by an older rule isn't sorted by a rule that no longer applies. **The lead is
  kept, never deleted** — the venue and its validated phone number are real; only the address
  goes. Leads with `source` other than `'leadgen'` are left alone entirely: a manually
  entered address is the operator's own data
- **The phone is validated twice: at harvest and again at promote** (`phones.normalize_us_phone`).
  The second check is not redundant — rows banked by an earlier build predate the validator,
  and promote is the last gate before a number reaches a dialer. `phone_digits` in crm.py
  returns `""` rather than a guess, so an unvalidated number can't reach the copy button
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
- The OSM `email`/`contact:email` tag is used before crawling — it's free, saves requests, and
  candidates were being rejected for "no email" when the map had one all along
- `opener` is one true thing about the venue pulled from its own site (a craft cocktail
  programme, a big tap list, happy hour) so the first sentence of a call isn't a cold open.
  Only ever taken from the venue's own pages, so it can't be wrong about them
- Log lines to grep on Render: `LEADGEN_TABLES_READY`, `LEADGEN_RUN`, `LEADGEN_TABLES_FAILED`

## CRM SALES TOOLING
- `GET /v1/crm/funnel` — signups, trials by days-remaining, trial→paid, **activation**
  (signed up but never finished a first count), pipeline and per-channel win rates
- `GET /v1/crm/queue` — overdue follow-ups, due today, never called, each with a call-window
  hint, per venue (see THE CALL LIST)
- **Timezone is decided by STATE, not longitude** (`us_tz_offset(lon, state, lat)`). The
  Central/Mountain line runs through west Texas, Kansas, Nebraska and the Dakotas, so a
  meridian cutoff cannot get Texas right: the old -97.5 rule put Austin, San Antonio and
  Oklahoma City in Mountain, and Atlanta in Central — 52 real Atlanta bars filed an hour
  early. `_SPLIT_STATES` handles the states the line genuinely crosses by longitude;
  Idaho is in `_LAT_SPLIT_STATES` because its line runs east-west (Boise is Mountain,
  the panhandle is Pacific). Covered by test_timezones.py
- `_reconcile_timezones()` runs on EVERY boot from `init_leadgen_tables()` and re-files any
  row whose stored zone disagrees with the current rule. Deliberately not a one-shot
  migration: that would fix today's rows and be wrong again the next time a boundary is
  corrected, and a lead in the wrong tab is called during its dinner service. Idempotent
  and cheap; nothing sets these by hand so there is no operator edit to clobber
- `POST /v1/crm/leads/{id}/touch` — one call that stamps the date, moves the status, sets the
  follow-up, appends a dated note and spends both counters IN ONE TRANSACTION. That
  atomicity is what stops the activity numbers drifting from the pipeline
- `POST /v1/crm/attribution/rematch` — joins crm_leads to users by email, then unique email
  domain, then unique normalized business_name, recording WHICH method matched. Conservative
  on purpose: a wrong attribution points the next 5,000 touches at the wrong city
- `GET /v1/crm/users?status=&q=&limit=&offset=` — the **Customers** page (behind the burger,
  beside Apple Analytics): everyone who actually downloaded the app and made an account, which is the
  other side of the pipeline tab (everyone who HASN'T). Reads straight from `users`, not
  `crm_leads` — most rows never touched the pipeline at all, since an organic download signs
  up with no call or email behind it. No touch/log/email actions — the calling workflow lives
  on the pipeline tab — but it does carry a delete button (see below), because this list
  needs its own housekeeping. Each row carries `location_count`, `sessions_completed` and
  `last_active_at` (from `locations`/`inventory_sessions`) and, where attribution has matched
  one, the originating `lead` — the same `matched_user_id` join `rematch_attribution` writes,
  done as a Python lookup here rather than a SQL join so an unmatched user (most of them)
  needs no special-casing. `status` is one of `trial` / `active` / `canceled` (the only values
  `billing_webhook` in main.py ever writes) and, like the pipeline's stage tabs, the `counts`
  in the response are always whole-table, never the current filter. Trial-days-left is
  computed client-side from `trial_ends_at` — plain date arithmetic on a field already in the
  row, not worth a server round trip
- **`TEST_EMAIL_PATTERN` (crm.py, beside `USER_STATUSES`) excludes App Store review and our
  own QA accounts from the Customers list, unconditionally** — every row, every count, every
  search. A build submitted for review gets a fresh `appreview…@icloud.com` /
  `applereview@my86d.com` account every time, and our own test accounts follow
  `test[-+.]…@86d.com` or land on `example.com`; left in, they silently padded "signups" and
  made trial-conversion numbers look worse than reality. This is a VIEW filter only — it never
  touches the row, so it costs nothing to widen or narrow later. It only matches on shape, so
  an oddly-named but real signup (no `test`/`appreview`/`-verify` in the address, not on
  `86d.com`/`example.com`) still shows up and has to be judged by hand
- `DELETE /v1/crm/users/{id}` — the Customers list's own delete button, for exactly that: a
  signup the pattern filter above doesn't catch (an ad hoc test account, a mistaken signup)
  that still needs to go. **Soft delete**, setting the same `deleted_at` the product API
  already checks everywhere a user matters — login, registration's email-exists check, the
  funnel, this list itself — so deleting here can't orphan anything and needs no special
  handling elsewhere: the account simply can't log in again and its email frees up. A hard
  `DELETE FROM users` was never an option here anyway — `locations.user_id` is a real foreign
  key, so it would fail outright the moment the account has any
- `GET/POST /v1/crm/suppressions` — do-not-call. Checked at promote time, so a suppressed
  venue can never re-enter the pipeline through the generator either
- `GET /v1/crm/leadgen/export.csv?scope=today|queue|all` — for an auto-dialer
- Today's call list and the CSV both exclude `won`/`dead`: calling someone who asked not to
  be contacted is the one mistake this list must never cause

## CALLING MODE — the "Ready to start calling" button
- `GET /v1/crm/now` — ONE flat queue, ordered by who is in a calling window this minute,
  then by how far the call can get (a name to ask for, then a direct mailbox, then fit
  score). The response still has three buckets — `ready` (in a window now), `soon` (opens
  shortly), `rest` (past the window, shut today, permanently closed) — plus counts and a
  `headline`, but **the page only ever renders `ready` as a table.** It used to also render
  `soon`/`rest` as tables (and, before that, an eight-tab service×timezone browser via a
  separate `/calllist` endpoint) — all removed as over-complication: the button's only job
  now is "fetch whoever I can call right now," so that's the only thing on screen. When
  `ready` is empty the page shows one line of status (list truly empty → offer to fill it;
  leads exist but none dialable → say so; leads exist but none in a window → say how many
  are coming up) instead of a second table, and keeps polling every 60s until one opens
- `WINDOW_RANK` (in crm.py, beside `_call_window`) is the ONE definition of how ringable each
  window state is. `late` ranks above `shut_today` because it does not mean closed — it
  means the quiet half hour has passed, not that the doors have. Still used to order `rest`
  server-side even though the page no longer renders that bucket as a table, since a future
  screen (or `/calllist`, still live server-side for anything that wants the old tabbed view)
  can rely on the same ranking
- `dialable_total` is distinct from `unworked_total`: a list of rows whose phones all failed
  validation is empty for calling purposes but must NOT trigger a lead fill, because filling
  won't fix it. Only `list_empty` (no unworked leads at all) auto-starts a fill
- It crosses timezones freely on purpose — at any moment the Eastern bars setting up and the
  Pacific ones in their lull are both good calls, and the zone stops mattering once you know
  it's their quiet half hour
- The page refreshes this every 60s while it's on screen. Windows open and shut on the clock,
  so a list left sitting goes stale under you
- **`CRM_OPERATOR_TZ` (default `Asia/Manila`) is where the caller is.** The operator is in
  Iloilo, UTC+8, so the entire US calling day lands in the middle of their night — US Eastern
  afternoon is roughly 2-4am there. The page dropped its persistent "your clock" readout when
  the calling-mode UI was stripped down to one button and one table; `starts_at_yours` is
  still in the API for anything that wants to show it
- **Venue-local time comes from an IANA zone (`tz_name`), not the raw offset.** The offset is
  standard time, so from March to November it is an hour behind the real local clock
  everywhere except Arizona — and an hour is the entire width of the pre-open window, enough
  to ring a bar that is still locked. `us_tz_name()` in leadgen.py; `_reconcile_timezones()`
  backfills it

## THE PIPELINE TAB (every lead, at every stage)
- `GET /v1/crm/leads?status=&q=&limit=&offset=` — the whole book, searchable and paged, plus
  `GET /v1/crm/leads/{id}` for the edit form. **This is the only screen that shows a lead
  AFTER it has been worked.** The call list deliberately hides anything touched — that's what
  stops the same bar being rung twice — and Follow-ups only shows what's due, so before this
  tab existed a bar you spoke to on Tuesday and forgot to book a callback for was invisible.
  That is how warm leads quietly die
- **Ask AI** — the box ABOVE the search bar. `POST /v1/crm/ask {question}` hands Claude
  (`_ask_claude`, Haiku) a text snapshot of the book — every lead (status, last outcome,
  calls, last touched, follow-up, contact, email, latest note) and the full touch log with
  undone dials excluded — and returns `{answer, leads}`. Every timestamp in the snapshot is
  the OPERATOR's local time (`CRM_OPERATOR_TZ`), with TODAY stated, so "who did we email last
  Thursday" resolves against their calendar. Leads are aliased L1, L2… to save tokens; the
  server maps them back and drops any alias the model invented. **The model never writes
  SQL**: the same database holds customer accounts and password hashes, and a snapshot of
  CRM rows is all a sales question needs. Read-only — nothing on this path writes. Covered
  by test_ask.py
- `q` searches name, town, contact, email and phone. The phone match strips punctuation on
  both sides, so "6157429095" finds "+1-615-742-9095"
- **Two buttons only: Open and Dead** (`status=open` on `/leads` means `status <> 'dead'`).
  Every lead is Open until they said no — not interested, already have a system or an app,
  don't call again — and the debrief/quick-add prompts send exactly those to `dead`. The five
  stage chips this replaced (In play / Not called yet / Won / Dead / Everything) made the
  operator remember what each held. Typing a search looks across both
- Counts on the two buttons are for the WHOLE pipeline, never for the current filter — a
  button that renumbers itself when you click it is unreadable
- Eight columns, not ten: the contact's name sits under the bar's, and last-touch/next-due are
  one column. At ten the action buttons fell off the right-hand edge, and the buttons are the
  point of the screen
- **WHERE THINGS STAND shows `last_outcome`, not just a bare date.** A STAGE badge of
  CONTACTED covers a voicemail, a gatekeeper, and an actual conversation alike (`log_touch`
  in crm.py lands all three on "contacted") — the badge alone can't answer "did I actually
  reach anyone?", and "last touched 2026-09-22" didn't either. `last_outcome` has always
  recorded the real answer (`OUTCOME_LABEL` in crm.html: Answered / Voicemail / Manager out /
  Not interested / Asked for a callback / Logged); it just wasn't shown anywhere on this
  screen. Follow-ups' mini table shows it too, under the bar's name, for the same reason
- Edit, Log, Email and Delete all work inline here, sharing the same endpoints (and the same
  undo) as the call list

## THE CALL LIST (the screen the operator actually lives in)
- `GET /v1/crm/calllist` — every unworked lead, split BY SERVICE then BY TIMEZONE. Two levels
  because they answer two questions: the service tab answers "it's 11am, who is even open?"
  (a bar that doesn't unlock until four is unreachable now and belongs behind another tab),
  the zone sub-tab answers "who's in their window right now?"
- **Zones are returned in fixed east-to-west order and every zone is always present, empty or
  not.** Never re-sorted by how good each looks right now: the tabs stay where the hand
  expects them, and east-to-west IS the order the afternoon moves. The `recommended` flag
  moves instead. Venues with no hours in OSM sit under DINNER — filing them under lunch would
  send late-morning calls to bars that don't open until four
- Clicking an empty zone tab SHOWS that it's empty rather than bouncing you to a full one;
  only an auto-selected zone gets skipped past. Landing somewhere else after a deliberate
  click is the more disorienting of the two
- **Call timing is PER VENUE, from its own `opening_hours`, not a blanket window.** The old
  fixed 2-5pm was wrong for much of the list: real harvested data has bars opening at 4pm and
  nightclubs at 9pm, and a 2pm dial to either reaches an empty room. The heuristic in
  callwindow.py. Every window now STARTS 30 MINUTES BEFORE the doors open (`PRE_OPEN_MINUTES`)
  — staff are in, taking deliveries, not yet serving anyone; it's the quietest half hour of a
  venue's day. A LUNCH venue (opens at/before 11:30) gets TWO windows: open-30min to
  open+45min, and the 2:00-4:00pm post-lunch lull. The single
  2-4:30 window it started with made the lunch tab useless for its own purpose — from 11am
  to 2pm every row read "too early", three hours in which the doors are open and nobody has
  ordered yet. Between the two windows the headline says "In the rush", not "too early",
  which at 12:30pm reads like a bug. A LATER-opening venue gets one window: open to two
  hours after (staff setting up, manager on, nobody ordering drinks yet). Unparseable or
  missing hours fall back to the generic afternoon — never worse than before
- **`_split_rules()` splits on `;` AND on a comma that introduces a new day.** The OSM spec
  separates rules with `;` and uses `,` to join time spans inside one rule, but contributors
  use commas for both. `Mo-Th 11:00-24:00, Fr 11:00-26:00, Sa 10:00-26:00, Su 10:00-24:00`
  parsed as a single Mon-Thu rule carrying four spans, leaving a real harvested bar with NO
  hours for Friday, Saturday or Sunday — read as shut on its three best nights and dropped
  off the call list all weekend
- Venues `opening_hours` marks `closed` are dropped at harvest; venues shut TODAY are sorted
  to the bottom and labelled with the next day they open. Zone headlines are derived from how
  many rows are actually ringable, so a header can't say "nobody's there" above an open bar
- **Attempt cadence (`CADENCE_DAYS`, `MAX_ATTEMPTS`).** A call that reaches nobody
  auto-schedules the next try at +1, +2, +4, +7, +14 days, then retires the lead as dead with
  a note. Persistence is the biggest lever in cold calling and the easiest to lose: before
  this, a voicemail only came back if the caller remembered to set a follow-up by hand, so
  most leads died at attempt one. A connect always overrides the ladder — if a human says
  "call me Tuesday", that wins
- The call list orders by fewest attempts first: an untried lead beats a fourth swing at one
  that never answers
- `GET /v1/crm/dialstats` — connect rate by hour, weekday and attempt number, from the
  `crm_touches` log. The windows above are a REASONED HEURISTIC; this is how it gets checked
  against reality. Once a few hundred dials are logged, move the window to match the data
  rather than trusting the heuristic. It reports thin data honestly rather than dressing up
  noise
- **`GET /leads/{id}/brief` is the pre-call sheet.** Facts from venue.py first, each labelled
  with where it came from; then two or three talking points Claude writes FROM THOSE FACTS
  ONLY, cached in `call_brief` so nobody waits on a model with a phone in their hand. A model
  outage returns the facts alone rather than nothing — the facts are the part that had to be
  true anyway
- **Claude drafts the email on request.** `POST /v1/crm/leads/{id}/draft-email` takes a
  sentence of intent ("Ed wants more info, include a link to the app and my website") and
  returns a subject and body into the compose box. Send the CURRENT draft back with the next
  brief and it edits that draft instead of writing a new one — a tweak like "shorter" must
  not lose the part that was already right. The operator can still type over any of it, and
  nothing sends until Send is pressed
- **The drafting prompt is facts-only** (`_draft_system`). It is handed the product
  description, the venue, the contact and the links from `COMPANY_WEBSITE` / `COMPANY_APP_URL`,
  and told in the first rule never to invent a URL, price, percentage, customer count or
  feature — with an explicit "NO LINKS ARE AVAILABLE, do not include any URL" when neither
  env var is set. A cold email carrying a made-up link is worse than no email
- `_ask_claude()` is the one place that knows the Anthropic headers, the `{` prefill trick and
  what each failure should say; both the drafter and the call-notes reader go through it.
  `ANTHROPIC_BASE_URL` overrides the host, for a gateway or a local stand-in
- **An approved email can be held for the venue's quiet hour.** `send_at` on
  `POST /leads/{id}/send-email` queues it in `crm_scheduled_emails` instead of sending;
  `_scheduled_email_loop` in main.py wakes every 60s and `run_due_emails()` sends what's due.
  `GET /leads/{id}/send-slots` proposes times from the venue's OWN opening hours (the rush
  that ruins a badly-timed email is theirs) and returns every one in BOTH clocks — the
  operator is half a day away and "2pm Tuesday" tells them nothing about whether they'll be
  awake for it
- **Queueing does NOT stamp the lead.** The touch happens when the mail actually goes, so the
  bar stays on the call list and stays callable — scheduling a note for Tuesday is no reason
  to stop ringing them today. Only `queued_email_at` is set, for the badge
- **An email that comes due more than `CRM_EMAIL_STALE_MINUTES` (90) late is NOT sent.** A
  2pm send surfacing at 6pm lands "I know you're quiet right now" mail in the middle of
  service, which is the exact harm scheduling exists to prevent. It's marked failed with an
  explanation and shown to the operator to reschedule. The guard was originally written for
  a sleeping free-tier instance; the service is on **Starter** and does not sleep, so the
  remaining causes are deploys, restarts and a loop that fell behind — rarer, but the harm
  is identical and the guard still earns its place
- Each due row is claimed with a conditional `UPDATE ... FOR UPDATE SKIP LOCKED` before the
  send, so two workers, or one worker and a Render restart mid-flight, cannot send the same
  email twice. Sending twice is the failure that matters: the recipient sees it, and nothing
  afterwards unsends it. One pending email per lead — queueing a second marks the first
  `replaced`
- A send that fails is left `failed` with the error, never retried in a loop: a bad address or
  a rejected login will not fix itself. `GET /v1/crm/scheduled` surfaces pending AND failed,
  and the page shows failures in red above the list — an email you believe went out and
  didn't is a follow-up you wait on forever
- **The Email button sends from the server, it is not a `mailto:` link.** `POST
  /v1/crm/leads/{id}/send-email` opens a compose box prefilled with the pitch, sends via
  mailer.py, then stamps `email_date`, moves the status off `new`, appends a dated note,
  records the touch and spends the email counter — in one transaction, with undo. The old
  mailto: handed the job to whatever client the browser had registered and heard nothing
  back, so the pipeline couldn't count it. **The send happens BEFORE the database write**: a
  message that went out unrecorded is recoverable by looking in the sent folder, a row
  claiming "sent" for mail that never left is not
- `GET /v1/crm/mail/status` tells the page whether a mailbox is configured. Without one the
  button falls back to the old mailto: hand-off rather than breaking
- Outgoing mail is PLAIN TEXT. A one-to-one note to a bar manager should look like a person
  wrote it; an HTML template reads as a blast and filters accordingly. `Date` and
  `Message-ID` are set explicitly — a message missing them is one of the cheapest spam
  signals there is
- **Everything on this screen is 12-hour.** Venue clocks, the operator's clock, call windows,
  and the connect-rate-by-hour table (`hour_label`). "13:45 there" is a small tax on every
  glance and this screen is glanced at constantly
- **Every touch is reversible.** `_snapshot()` stores the whole row before a touch changes
  it, `GET /v1/crm/undo` lists what was just worked, `POST /v1/crm/undo/{id}` puts it back
  exactly — status, attempts, notes, follow-up date — and refunds the counters, because a
  call that didn't happen shouldn't show in the day's numbers. Stored as a whole-row snapshot
  rather than a list of fields to unwind: a touch changes six things and each has a different
  "undo" depending on what the row already held. The page shows both a 10-second Undo in the
  toast and a persistent "just worked" bar, because noticing a misclick usually happens
  several calls later
- The undone dial stays in `crm_touches` marked `outcome='undone'` rather than being deleted —
  it was dialled, and /dialstats should stay honest about that
- A lead leaves this list the moment it is touched, debriefed, status-changed or deleted —
  the filter is `status = 'new' AND last_touch_at IS NULL`. That is what makes it impossible
  to call the same restaurant twice, and the shrinking list doubles as the progress bar
- `phone_digits` is on every lead: bare digits, US country code stripped (`+1-615-742-9095`
  → `6157429095`). A lead whose phone doesn't validate is dropped from the call list entirely
  rather than shown with a dead number — see phones.py
- **What actually gets copied is `phone_dial` (`format_us_phone_dashed()`), not
  `phone_digits`.** CloudTalk's paste box silently refuses a bare 10-digit string with no
  separators, so a bare-digits COPY button was producing a number that wouldn't paste into
  the one place it's for. `615-742-9095`, not `format_us_phone()`'s `(615) 742-9095` (that
  one's for reading a number aloud, not pasting it, and CloudTalk doesn't take parens
  either). One click on the page copies it
- `DELETE /v1/crm/leads/{id}` and `POST /v1/crm/leads/bulk-delete` also RETIRE the
  `crm_lead_candidates` row that produced the lead. Without that the generator re-promotes
  the same restaurant on a later run and it reappears — the exact duplicate call that
  deleting it was meant to prevent
- **AI is Claude only, via the raw REST API through httpx** (`ANTHROPIC_API_KEY`,
  `ANTHROPIC_MODEL` default `claude-haiku-4-5-20251001`). It used to share the scan path's
  OpenAI→Gemini pair on the grounds that it needed no new key; this is one short text
  extraction per logged call and Haiku is materially cheaper. No SDK, matching how main.py
  talks to Resend. **The scan path in main.py is unchanged and still OpenAI→Gemini** — that
  is the product's core feature, not the CRM's
- The drawer's quick-outcome buttons (Voicemail / Manager out / Not interested) go straight
  to `/touch` with a known outcome. They used to post a canned sentence through the model —
  a paid round trip to be told what the button already said, which also meant the quick path
  stopped working with no API key set. Only free text a human typed is worth a model
- `POST /v1/crm/leads/{id}/debrief` — free-text call notes in, structured fields out
  (status, outcome, contact, email, phone, follow-up date, a dated note), applied in one
  transaction along with the counters. Uses Claude (`_ask_claude()`, `ANTHROPIC_API_KEY`) —
  see AI is Claude only, below; this bullet used to say it shared the scan path's
  OpenAI/Gemini pair, which stopped being true when debrief moved to Claude and was never
  corrected here. With no provider key it 503s with "type the fields in by hand" rather than
  failing obscurely. Model output is treated as untrusted: `status` is checked against
  VALID_STATUSES, `outcome` against `TOUCH_OUTCOMES`, `followup_in_days` is range-checked,
  and the free-text fields are length-capped before they reach a column. Everything the
  model decided is echoed back in `applied` so a misreading is visible immediately
- **`outcome` is asked for and used DIRECTLY, not re-derived from `status`.** It used to be:
  `status` is a coarse pipeline stage on purpose ("voicemail or gatekeeper with nobody
  reached -> status 'contacted'", same as an actual conversation), and `_apply_call_notes`
  then inferred `last_outcome` from THAT alone — anything landing on warm/won/contacted
  became "answered". A debrief reading "left a voicemail, no answer" landed `status`
  correctly and `last_outcome='answered'` wrong, indistinguishable on screen from a real
  conversation — the exact thing WHERE THINGS STAND (above) was built to show. Worse: `answered`
  is in the set `_cadence()` treats as "reached, stop scheduling", so a voicemail silently
  fell out of the retry ladder too. `DEBRIEF_SYSTEM`/`QUICK_ADD_SYSTEM` now ask for `outcome`
  (`TOUCH_OUTCOMES`: answered/voicemail/gatekeeper/not_interested/callback) alongside
  `status` explicitly, and `_apply_call_notes` uses it when the model supplies a valid one,
  falling back to the old status-based guess only when it doesn't (an older extraction, or a
  model that skips the field)
- **`no_answer` is its own outcome, and nobody-picked-up is never "answered".** There was no
  outcome for a call that rang out with no way to leave a message, so it had nowhere to land:
  Pig & the Sprout, noted "no one picked up the phone, and you can't leave a message", was
  logged Answered — which also stopped its retry ladder. `_no_answer_outcome()` reads the
  operator's RAW notes and overrides the model when it says "answered" or nothing (never a
  callback/not-interested, since those mean a person spoke). The fallback no longer guesses
  "answered" from status=contacted. `_reconcile_no_answer()` runs every boot and re-files
  rows logged before this, reading only the latest note line
- **`POST /v1/crm/leads/quick-add` is the same idea for a call to a bar that was never in
  the pipeline at all** — cold-found on the operator's own initiative, a referral, a walk-in.
  `/debrief` only ever updates a lead that already exists; this describes the call in plain
  words and creates the lead AND logs that first call in one step, sharing `_apply_call_notes()`
  (extracted from `/debrief`'s body) so a brand-new lead gets the exact same undo/counter/
  cadence handling an old one's touch gets, not a thinner copy of it. On the CRM tab, "Add a
  lead" opens this (a bare `prompt()` for a name used to be the whole flow, leaving every
  real field for later "Edit")
- **Quick-add takes ONE paste box — no separate name field.** The name comes from the model
  (`QUICK_ADD_SYSTEM` tells it the venue is almost always the first thing in pasted notes and
  to always return it), then `_name_from_text()` (the text before the first phone number,
  " at ", or punctuation) if the model drops it, and only 422s if both are empty. A required
  "Bar name" box was tried for a day and rejected by the operator: the point is to paste
  a listing plus a sentence and walk away. The real input that drove this — "Olde Town
  Tavern & Grill at (720) 242-9667 ... Website: Olde Town Tavern & Grill, Called this
  place..." — is a test in test_quick_add.py
- **Quick-add finds the email itself.** When the notes carry no address (or say "it's on
  their website"), `leadgen.find_venue_website()` looks the venue up on Nominatim by name +
  town for its OSM `website` tag (unless the notes gave a URL), and
  `leadgen.find_email_on_site()` reads it the same way `enrich_candidate` does — homepage,
  the site's own contact links, then the guessed paths, capped at 4 pages so the click
  stays a few seconds. Where it was found is echoed in `applied.email_found_on` and noted
- **Everything the model finds is kept, labelled, in the notes** — decision makers, who
  was spoken to, next step, address, other phones, website, where the email came from.
  crm_leads has no columns for most of these, and "CONTACTED" alone tells the operator
  nothing when they come back to it
- **Pipeline rows are clickable.** Anywhere on a row that isn't a button opens a read-only
  drawer with every field plus the full notes/history — the row itself only has room for a
  badge and one line

## Environment Variables Required
Source of truth: the `_config_checks` startup list in main.py (~line 52) — it logs what's missing on boot.
- DATABASE_URL — PostgreSQL connection string (required, app crashes without it)
- SECRET_KEY — JWT signing key (CRITICAL if left at the default — anyone can forge login tokens)
- OPENAI_API_KEY — primary bottle-scan provider; without it, scanning falls straight to Gemini
- GEMINI_API_KEY or GOOGLE_API_KEY — fallback bottle-scan provider; without it, no fallback if OpenAI fails
- OPENAI_MODEL / GEMINI_MODEL — optional, override the scan models when a provider retires one
- RESEND_API_KEY — order emails and password resets cannot send without it
- STRIPE_SECRET_KEY — checkout/billing endpoints 503 without it
- STRIPE_PRICE_ID — checkout endpoint 503s without it, nobody can subscribe
- STRIPE_WEBHOOK_SECRET — without it, payments don't activate subscriptions (customers pay and stay locked out)
- CRM_API_KEY — shared key for `/v1/crm/*`; unset means every CRM endpoint 503s (the UI at
  `/crm` still loads, it just can't do anything). Not used by the mobile app at all
- ANTHROPIC_API_KEY — the CRM's only AI call (`/debrief`, reading call notes into fields).
  Without it that endpoint 503s with "type the fields in by hand" and everything else,
  including the quick-outcome buttons, works normally. Not used by the mobile app
- ANTHROPIC_MODEL — optional, default `claude-haiku-4-5-20251001`
- COMPANY_WEBSITE (default `https://my86d.com`), COMPANY_APP_URL (default empty),
  COMPANY_NAME, COMPANY_BLURB — the only facts the email drafter may state. An unset
  COMPANY_APP_URL means no App Store link appears, never an invented one
- SPACEMAIL_USER / SPACEMAIL_PASSWORD — the mailbox the Email button sends from
  (`Stephan@my86d.com`). Unset means the button falls back to a `mailto:` link and nothing is
  recorded. SPACEMAIL_HOST (default `mail.spacemail.com`), SPACEMAIL_PORT (465),
  SPACEMAIL_FROM_NAME and SPACEMAIL_TIMEOUT are optional
- CRM_OPERATOR_TZ — where the person making the calls is (default `Asia/Manila`). Decides the
  "your time" clock and every upcoming-window time on the call screen
- CRM_TIMEZONE — optional, zone name the CRM's daily counters roll over in (default UTC).
  Also decides when the daily lead run fires
- LEADGEN_BUCKET_TARGET (default 50 — leads per service×timezone tab; total capacity is
  8× this), LEADGEN_DAILY_TARGET (25, a pace not a ceiling), LEADGEN_POOL_FLOOR (50),
  LEADGEN_ENRICH_WORKERS (8),
  LEADGEN_RUN_HOUR (18 = 6pm, local) — optional lead generator tuning. No API key needed: the
  generator uses OpenStreetMap, which has neither keys nor billing
- APPLE_ISSUER_ID / APPLE_KEY_ID / APPLE_PRIVATE_KEY (+ optional APPLE_APP_ID) — optional; the
  Apple Analytics tab's App Store Connect team key. Unset is fine: the tab's Connect form
  saves the key instead (encrypted). `\n` in APPLE_PRIVATE_KEY is accepted
- SENTRY_DSN — optional, error visibility only
- CONFIDENCE_THRESHOLD, LEVEL_DEADBAND — optional tuning, see AI Vision Rules above

## Deploy Rules
- Deployed via Render (see Procfile) — do NOT change without approval
- **The web service is on the Starter plan ($7/mo, 0.5 CPU, 512MB), not Free.** It does not
  spin down, so there is no cold start to design around. Confirmed from the Render dashboard
  on 2026-09-15; earlier notes in both repos assumed Free and were wrong. Postgres is on a
  paid tier separately. 512MB has been enough to crawl 200 venue sites in one run
- Requirements are pinned — check compatibility before upgrading
- Cannot push directly to main — always work on a feature branch and open a PR (branch name is assigned
  per session, not fixed — the old hardcoded `claude/build-ios-preview-ASNee` reference here no longer exists)
- NOTE: README.md is outdated (says SQLite) — ignore it, this app uses PostgreSQL
