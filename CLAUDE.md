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
  Follow-ups — with Numbers and Lead engine behind a burger top right: those are looked at
  occasionally and thought about once, and in the tab row they competed with the three things
  a working day actually needs. The burger turns orange when the open page lives inside it. Single self-contained file, no build step;
  replacing this file replaces the UI. Holds no credentials — the operator types the key and
  it lives in their browser's localStorage. **Designed for an operator with ADHD**: one
  headline stating the single next action, a shrinking list as the progress bar, two rows of
  tabs (service, then timezone) with exactly ONE table on screen at a time, and one primary
  button per row. The zone accordion it replaced meant four headers and four open/shut states
  to hold in your head; tabs mean one list and a fixed place for every tab. Keep it that way —
  extra choices on this screen are a cost, not a feature
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
- seed_data.py — default product catalog
- test_level_classifier.py — unit tests for helpers.py level logic
- test_phones.py, test_callwindow.py, test_timezones.py, test_contacts.py — the phone
  validator, call-window/service-band logic, timezone assignment, and manager/email
  classification, all pure. Run them: `pytest test_level_classifier.py test_phones.py
  test_callwindow.py test_timezones.py test_contacts.py test_venue.py -q` (215 tests)

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
- Runs interrupted mid-flight (Render free tier spins down constantly) are reconciled to
  `phase='abandoned'` on the next run; the health endpoint warns when several are stuck
- Enrichment runs in a thread pool (`LEADGEN_ENRICH_WORKERS`, default 8) and gives up on a
  site the moment its homepage doesn't load — a dead domain used to cost one request per
  guessed path. It prefers links the homepage actually points at over guessed URLs
- **The harvest takes `restaurant` as well as `bar`/`pub`/`nightclub`.** It used to take the
  three drink-led types only, which is a small slice of the places that pour: an independent
  restaurant with a licence has a back bar to count exactly like a tavern does, and in OSM it
  is `amenity=restaurant`. The cost is that most restaurants have no bar worth calling, so a
  restaurant must SHOW a drinks programme on its own site (`LIQUOR_HINTS`, or a `bar=yes`
  tag) before it can qualify. Harvesting is cheap; promoting is what matters
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
  inventory. `UPSCALE_HINTS` (tasting menu, sommelier) is a gentler one.
  `NEIGHBOURHOOD_HINTS` (pool table, happy hour, dive, tavern) is the positive. None of them
  EXCLUDE anything — a fine-dining room can still be on a clipboard and stays on the list;
  they only decide order, which is what matters when fifty names are in front of you
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
- `GET/POST /v1/crm/suppressions` — do-not-call. Checked at promote time, so a suppressed
  venue can never re-enter the pipeline through the generator either
- `GET /v1/crm/leadgen/export.csv?scope=today|queue|all` — for an auto-dialer
- Today's call list and the CSV both exclude `won`/`dead`: calling someone who asked not to
  be contacted is the one mistake this list must never cause

## CALLING MODE — the "Ready to start calling" button
- `GET /v1/crm/now` — ONE flat queue across all eight tabs, ordered by who is in a calling
  window this minute, then by how far the call can get (a name to ask for, then a direct
  mailbox, then fit score). The tabs are the right way to UNDERSTAND the list and the wrong
  way to WORK it: sitting down to call, the only question is "who do I dial first", and
  answering it by clicking eight tabs reading local clocks is work the screen should do
- It crosses timezones freely on purpose — at any moment the Eastern bars setting up and the
  Pacific ones in their lull are both good calls, and the zone stops mattering once you know
  it's their quiet half hour
- The page refreshes this every 60s while it's on screen. Windows open and shut on the clock,
  so a list left sitting goes stale under you
- **`CRM_OPERATOR_TZ` (default `Asia/Manila`) is where the caller is, and every screen shows
  their clock.** The operator is in Iloilo, UTC+8, so the entire US calling day lands in the
  middle of their night — US Eastern afternoon is roughly 2-4am there. "Best at 2:00pm"
  means nothing to someone thirteen hours away deciding whether to stay up, so every
  upcoming window is also printed in their own time (`starts_at_yours`)
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
- `q` searches name, town, contact, email and phone. The phone match strips punctuation on
  both sides, so "6157429095" finds "+1-615-742-9095"
- Stage counts on the tabs are for the WHOLE pipeline, never for the current filter — a tab
  that renumbers itself when you click it is unreadable
- Eight columns, not ten: the contact's name sits under the bar's, and last-touch/next-due are
  one column. At ten the action buttons fell off the right-hand edge, and the buttons are the
  point of the screen
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
- **An email that comes due more than `CRM_EMAIL_STALE_MINUTES` (90) late is NOT sent.** On
  Render's free tier the process sleeps after ~15 minutes idle and only wakes on a request,
  so a 2pm send can surface at 6pm — landing "I know you're quiet right now" mail in the
  middle of service, which is the exact harm scheduling exists to prevent. It's marked failed
  with an explanation and shown to the operator to reschedule
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
  → `6157429095`), for pasting into CloudTalk. One click on the page copies it. A lead whose
  phone doesn't validate is dropped from the call list entirely rather than shown with a
  dead number — see phones.py
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
- SENTRY_DSN — optional, error visibility only
- CONFIDENCE_THRESHOLD, LEVEL_DEADBAND — optional tuning, see AI Vision Rules above

## Deploy Rules
- Deployed via Render (see Procfile) — do NOT change without approval
- Requirements are pinned — check compatibility before upgrading
- Cannot push directly to main — always work on a feature branch and open a PR (branch name is assigned
  per session, not fixed — the old hardcoded `claude/build-ios-preview-ASNee` reference here no longer exists)
- NOTE: README.md is outdated (says SQLite) — ignore it, this app uses PostgreSQL
