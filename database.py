import psycopg2
import psycopg2.extras
import psycopg2.pool
import os
import time
import threading
from contextlib import contextmanager
from typing import Optional
from seed_data import SEED_PRODUCTS
from helpers import generate_id, now_iso, product_match_key

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

# ── Connection pool ───────────────────────────────────────────────────────────
# Max 10 keeps us well under Render free-tier's 22-connection cap.
# Pool is lazy-initialised on first get_db() call and rebuilt automatically
# after all retries are exhausted on a dead-connection burst.

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()

_CONN_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)
_BACKOFF = (0.3, 0.6)   # seconds between attempts 1→2 and 2→3


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=DATABASE_URL,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=5,
                keepalives_count=3,
            )
    return _pool


def _drop_conn(pool: psycopg2.pool.ThreadedConnectionPool, conn) -> None:
    """Return a single bad connection to the pool and close it immediately."""
    try:
        pool.putconn(conn, close=True)
    except Exception:
        pass


def _drain_pool(pool: psycopg2.pool.ThreadedConnectionPool) -> None:
    """Close every connection and null out the global pool reference."""
    global _pool
    with _pool_lock:
        try:
            pool.closeall()
        except Exception:
            pass
        if _pool is pool:
            _pool = None


@contextmanager
def get_db():
    """Borrow a validated connection from the pool.

    Acquire + validate phase (up to 3 attempts, 0.3s / 0.6s backoff):
      - getconn() from pool
      - SELECT 1 ping to confirm the connection is alive
      - If either fails with OperationalError/InterfaceError, drop only that
        conn (putconn close=True) and retry
      - After all retries exhausted, drain the whole pool and re-raise so the
        next caller gets a fresh pool

    User-code phase (inside the yield):
      - Any OperationalError/InterfaceError → rollback, drop the conn, re-raise
        (no retry — the caller's transaction is already broken)
      - Any other exception → rollback, return conn to pool normally, re-raise
    """
    conn = None
    pool = None
    last_error: Exception = Exception("unreachable")

    # TODO: handle psycopg2.pool.PoolError (all 10 conns checked out) at scale
    for attempt in range(3):
        try:
            pool = _get_pool()
            conn = pool.getconn()
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            # Validate — catches silently-dead idle connections
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            break  # conn is good
        except _CONN_ERRORS as exc:
            last_error = exc
            if conn is not None:
                _drop_conn(pool, conn)
                conn = None
            if attempt < 2:
                time.sleep(_BACKOFF[attempt])
            else:
                # All retries exhausted — drain so next caller starts fresh
                print(f"[db] pool drained after 3 failed attempts: {last_error}", flush=True)
                _drain_pool(pool)
                pool = None
                raise

    # ── User code ────────────────────────────────────────────────────────────
    try:
        yield conn
    except _CONN_ERRORS:
        # Broken mid-transaction — drop this conn, don't return it to pool
        try:
            conn.rollback()
        except Exception:
            pass
        if pool is not None:
            _drop_conn(pool, conn)
        conn = None
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if conn is not None and pool is not None:
            pool.putconn(conn)


def init_db():
    """Initialize database with tables and seed data"""
    with get_db() as conn:
        cursor = conn.cursor()

        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT,
                terms_accepted_at TEXT,
                privacy_accepted_at TEXT,
                trial_started_at TEXT,
                trial_ends_at TEXT,
                subscription_status TEXT DEFAULT 'trial',
                subscription_tier TEXT DEFAULT 'starter',
                password_reset_token TEXT,
                password_reset_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_email
            ON users(email) WHERE deleted_at IS NULL
        """)

        # Locations table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS locations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                address TEXT,
                timezone TEXT DEFAULT 'America/New_York',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_locations_user
            ON locations(user_id) WHERE deleted_at IS NULL
        """)

        # Products table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                brand TEXT,
                category TEXT NOT NULL,
                size TEXT,
                upc TEXT UNIQUE,
                image_url TEXT,
                price REAL,
                scan_count INTEGER DEFAULT 0,
                verified INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_upc
            ON products(upc) WHERE upc IS NOT NULL
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_search
            ON products(name, brand)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_category
            ON products(category)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_scan_count
            ON products(scan_count DESC)
        """)

        # Par levels table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS par_levels (
                id TEXT PRIMARY KEY,
                location_id TEXT NOT NULL REFERENCES locations(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                par_quantity REAL NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(location_id, product_id)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_par_location
            ON par_levels(location_id)
        """)

        # Inventory sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inventory_sessions (
                id TEXT PRIMARY KEY,
                location_id TEXT NOT NULL REFERENCES locations(id),
                user_id TEXT NOT NULL REFERENCES users(id),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                total_bottles INTEGER DEFAULT 0,
                duration_seconds INTEGER,
                status TEXT DEFAULT 'in_progress',
                device_id TEXT,
                app_version TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_location
            ON inventory_sessions(location_id, started_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_status
            ON inventory_sessions(status) WHERE status = 'in_progress'
        """)

        # Scans table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES inventory_sessions(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                level TEXT NOT NULL,
                level_decimal REAL NOT NULL,
                quantity INTEGER DEFAULT 1,
                detection_method TEXT NOT NULL,
                confidence REAL,
                pen_position_y REAL,
                capture_method TEXT DEFAULT 'manual',
                photo_url TEXT,
                shelf_location TEXT,
                notes TEXT,
                idempotency_key TEXT UNIQUE,
                synced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scans_session
            ON scans(session_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scans_product
            ON scans(product_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scans_idempotency
            ON scans(idempotency_key)
        """)

        # Voice notes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS voice_notes (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES inventory_sessions(id),
                audio_url TEXT,
                transcript TEXT,
                linked_product_id TEXT REFERENCES products(id),
                duration_seconds INTEGER,
                processed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_voice_session
            ON voice_notes(session_id)
        """)

        # Orders table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES inventory_sessions(id),
                location_id TEXT NOT NULL REFERENCES locations(id),
                order_data TEXT NOT NULL,
                total_items INTEGER NOT NULL,
                estimated_cost REAL,
                variance_alerts TEXT,
                exported_at TEXT,
                export_format TEXT,
                export_destination TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_session
            ON orders(session_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_location
            ON orders(location_id, created_at DESC)
        """)

        # Usage history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usage_history (
                id TEXT PRIMARY KEY,
                location_id TEXT NOT NULL REFERENCES locations(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                starting_amount REAL NOT NULL,
                ending_amount REAL NOT NULL,
                bottles_used REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_location_product
            ON usage_history(location_id, product_id, period_start DESC)
        """)

        # Sync queue table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_queue (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT,
                synced_at TEXT,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sync_pending
            ON sync_queue(user_id, synced_at) WHERE synced_at IS NULL
        """)

        # Distributors table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS distributors (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                rep_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_distributors_user
            ON distributors(user_id) WHERE deleted_at IS NULL
        """)

        # Location-Product-Distributor mapping table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS location_product_distributors (
                id TEXT PRIMARY KEY,
                location_id TEXT NOT NULL REFERENCES locations(id),
                product_id TEXT NOT NULL REFERENCES products(id),
                distributor_id TEXT NOT NULL REFERENCES distributors(id),
                created_at TEXT NOT NULL,
                UNIQUE(location_id, product_id)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_lpd_location
            ON location_product_distributors(location_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_lpd_distributor
            ON location_product_distributors(distributor_id)
        """)

        # Product aliases: name/brand phrasings that should resolve to an existing
        # product instead of creating a new one. Written when someone merges a
        # duplicate, so the phrasing that caused the split stops causing it.
        # Stored pre-normalized (see helpers.normalize_match_text) — the matcher
        # looks these up with an equality check, not a fuzzy one.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS product_aliases (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL REFERENCES products(id),
                norm_name TEXT NOT NULL,
                norm_brand TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(norm_name, norm_brand)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_product_aliases_product
            ON product_aliases(product_id)
        """)

        # Supports the normalized/swapped lookups in _match_or_create_product.
        # The expressions must match helpers.NORM_SQL exactly to be usable.
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_norm_name_brand
            ON products (
                regexp_replace(lower(coalesce(name, '')), '[^a-z0-9]+', '', 'g'),
                regexp_replace(lower(coalesce(brand, '')), '[^a-z0-9]+', '', 'g')
            )
        """)

        # Inventory drafts: one in-progress (unsent) scan session per user+location,
        # backing up the mobile app's local AsyncStorage copy against device loss.
        # Deliberately separate from the older scans/inventory_sessions tables,
        # which are shaped around the removed pen-detection flow (level/confidence/
        # pen_position_y) and don't match the current bottle-count data model.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inventory_drafts (
                user_id TEXT NOT NULL REFERENCES users(id),
                location_id TEXT NOT NULL REFERENCES locations(id),
                bottles_data TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, location_id)
            )
        """)

        conn.commit()

        # Backfill subscription fields for old accounts that pre-date those columns
        cursor.execute("""
            UPDATE users
            SET subscription_status = 'trial', updated_at = %s
            WHERE subscription_status IS NULL
        """, (now_iso(),))
        cursor.execute("""
            UPDATE users
            SET subscription_tier = 'starter', updated_at = %s
            WHERE subscription_tier IS NULL
        """, (now_iso(),))
        conn.commit()

        # Migrate users: add business_name, manager_name, stripe_customer_id, trial_reminder_sent_at, password_changed_at columns if absent
        # auth_provider/apple_subject carry Sign in with Apple: apple_subject is
        # the stable per-app user id Apple returns, and is what a returning
        # sign-in is matched on — not the email, which the user can rotate or
        # hide behind a relay alias at any time.
        for col, col_type in [("business_name", "TEXT"), ("manager_name", "TEXT"), ("stripe_customer_id", "TEXT"), ("trial_reminder_sent_at", "TEXT"), ("password_changed_at", "TEXT"), ("auth_provider", "TEXT DEFAULT 'password'"), ("apple_subject", "TEXT")]:
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")
                print(f"[db] migrated users: added {col} {col_type}", flush=True)
        conn.commit()

        # Migrate par_levels: add full_quantity, current_stock, price columns if absent
        for col, col_type in [("full_quantity", "NUMERIC(10,2)"), ("current_stock", "NUMERIC(10,2)"), ("price", "NUMERIC(10,2)")]:
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'par_levels' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE par_levels ADD COLUMN {col} {col_type} DEFAULT 0")
                print(f"[db] migrated par_levels: added {col} {col_type}", flush=True)
        conn.commit()

        # Migrate par_levels: par_set_at records when a human actually set a par, as
        # opposed to a row that exists only because something else was written to it.
        # Adding the column is also the one-shot gate for the backfill below, which
        # has to run exactly once.
        cursor.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'par_levels' AND column_name = 'par_set_at'
        """)
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE par_levels ADD COLUMN par_set_at TEXT")
            print("[db] migrated par_levels: added par_set_at TEXT", flush=True)

            # Every par_quantity of 1 in this table was almost certainly invented
            # by the API rather than chosen by anyone: the mobile app — the only
            # client — has never sent a par at all (it PATCHes current_stock and
            # price and GETs par levels, nothing more), and a row created by
            # either of those writes defaulted to a par of 1. The client now
            # reads par_quantity > 0 as "this bar set a par", so leaving those 1s
            # in place presents a par nobody picked as deliberate: it drops the
            # "Not set" warning and orders the bottle back up to 1.
            #
            # "Almost certainly" is the whole problem. POST /par-levels, its bulk
            # variant and the sync route all accept any positive par and none of
            # them stamp par_set_at, so a genuine par of 1 set through one of
            # them is indistinguishable from the placeholder — and clearing it
            # would silently drop that bottle out of every future order until
            # someone noticed by hand. So the old values are copied out first.
            # Nothing here reads this table; it exists so a wrongly-cleared par
            # can be put back with a single UPDATE ... FROM rather than being
            # gone for good.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS par_levels_backfill_log (
                    location_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    old_par INTEGER NOT NULL,
                    cleared_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                INSERT INTO par_levels_backfill_log (location_id, product_id, old_par, cleared_at)
                SELECT location_id, product_id, par_quantity, %s
                  FROM par_levels WHERE par_quantity = 1
            """, (now_iso(),))
            saved = cursor.rowcount

            cursor.execute("UPDATE par_levels SET par_quantity = 0 WHERE par_quantity = 1")
            print(
                f"[db] migrated par_levels: cleared {cursor.rowcount} placeholder par(s) of 1 "
                f"({saved} saved to par_levels_backfill_log)",
                flush=True,
            )
        conn.commit()

        # Migrate products: add source, created_by_user_id, deleted_at if absent
        products_migrations = [
            ("source", "TEXT DEFAULT 'manual'"),
            ("created_by_user_id", "TEXT"),
            ("deleted_at", "TEXT"),
            ("product_type", "TEXT"),
            # helpers.product_match_key(name, brand) — see there. Filled for
            # every row by reconcile_product_match_keys() below.
            ("match_key", "TEXT"),
        ]
        for col, col_def in products_migrations:
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'products' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE products ADD COLUMN {col} {col_def}")
                print(f"[db] migrated products: added {col} {col_def}", flush=True)
        conn.commit()

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_products_match_key
            ON products(match_key) WHERE deleted_at IS NULL
        """)
        conn.commit()

        # Backfill source: seed rows (verified=1) → 'seed', others keep 'manual'
        cursor.execute("""
            UPDATE products SET source = 'seed', updated_at = %s
            WHERE verified = 1 AND (source IS NULL OR source = 'manual')
        """, (now_iso(),))
        conn.commit()

        # One-time grandfather: accounts created during development got 14-day
        # trials that will have already lapsed by the time trial enforcement
        # deploys — without this, deploying locks out every existing account
        # (including demo/test accounts) the moment the paywall goes live.
        # Fixed literal dates keep this idempotent: it can never re-extend.
        cursor.execute("""
            UPDATE users
            SET trial_ends_at = '2026-08-18T00:00:00+00:00', updated_at = %s
            WHERE subscription_status = 'trial'
              AND created_at < '2026-07-19'
              AND (trial_ends_at IS NULL OR trial_ends_at < '2026-08-18')
        """, (now_iso(),))
        conn.commit()

        # Migrate locations: add order_rounding_mode and staff_names if absent
        for col, col_def in [("order_rounding_mode", "TEXT DEFAULT 'nearest'"), ("staff_names", "TEXT")]:
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'locations' AND column_name = %s
            """, (col,))
            if not cursor.fetchone():
                cursor.execute(f"ALTER TABLE locations ADD COLUMN {col} {col_def}")
                print(f"[db] migrated locations: added {col} {col_def}", flush=True)
        conn.commit()


        # An account created through Sign in with Apple has no password at all,
        # so password_hash can no longer be NOT NULL. Every path that compares
        # a password guards for the null (see login and delete_user in main.py)
        # — a null hash must read as "this account has no password", never as
        # "any password will do".
        cursor.execute("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'password_hash'
        """)
        row = cursor.fetchone()
        if row and row["is_nullable"] == "NO":
            cursor.execute("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL")
            print("[db] migrated users: password_hash is now nullable (social sign-in)", flush=True)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_apple_subject
            ON users(apple_subject) WHERE apple_subject IS NOT NULL AND deleted_at IS NULL
        """)
        conn.commit()

        # App funnel events. The product API can only see a user once the row
        # exists, so everything before that — opened the app, reached the
        # sign-up form, abandoned it — was invisible. These five events fill in
        # exactly that gap and nothing more.
        #
        # anon_id is a per-install random id, not a device identifier: it exists
        # so one install's open → view → submit can be joined into a funnel, and
        # it is regenerated if the app is reinstalled. There is deliberately no
        # free-form properties column — the write route is unauthenticated (a
        # pre-signup event has no user to authenticate), and a JSON blob on an
        # open endpoint is somebody else's storage.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_events (
                id TEXT PRIMARY KEY,
                anon_id TEXT NOT NULL,
                user_id TEXT,
                event TEXT NOT NULL,
                platform TEXT,
                app_version TEXT,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_app_events_event_created
            ON app_events(event, created_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_app_events_anon
            ON app_events(anon_id)
        """)

        # One row per /scans/analyze call: which provider answered, how fast,
        # what it said, what it matched. final_product_id is filled in later by
        # the draft sync with the product the bartender's row actually ended up
        # as — matched_product_id vs final_product_id is scan accuracy. No image
        # is stored. Written in the background (main._record_scan_event), so a
        # failure here never fails a scan.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scan_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                location_id TEXT,
                status TEXT,
                provider TEXT,
                model TEXT,
                fallback_from TEXT,
                name TEXT,
                brand TEXT,
                category TEXT,
                product_type TEXT,
                confidence REAL,
                match_method TEXT,
                matched_product_id TEXT,
                needs_rescan BOOLEAN,
                provider_ms INTEGER,
                total_ms INTEGER,
                input_tokens INTEGER,
                cached_tokens INTEGER,
                output_tokens INTEGER,
                image_kb INTEGER,
                label_text TEXT,
                label_supported BOOLEAN,
                final_product_id TEXT,
                final_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        # Added after the table first shipped: what the model read off the label
        # before naming the product, and whether that name was in it
        # (helpers.label_supports). ADD COLUMN IF NOT EXISTS is idempotent.
        cursor.execute("ALTER TABLE scan_events ADD COLUMN IF NOT EXISTS label_text TEXT")
        cursor.execute("ALTER TABLE scan_events ADD COLUMN IF NOT EXISTS label_supported BOOLEAN")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_events_created
            ON scan_events(created_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_events_user_created
            ON scan_events(user_id, created_at)
        """)
        conn.commit()

        # Seed products — always runs but is idempotent (checks name+brand before insert)
        seed_products(conn)
        reconcile_product_match_keys(conn)


def reconcile_product_match_keys(conn) -> int:
    """Make every product's stored match_key equal what product_match_key()
    computes today. Runs every boot, like the CRM's _reconcile_* passes, rather
    than once: the first run backfills the column, and any later change to the
    key rule re-keys the catalog on the next deploy instead of leaving old rows
    unmatchable. Only rows whose key differs are written, so a normal boot
    writes nothing. Returns the number of rows updated."""
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, brand, match_key FROM products")
    stale = []
    for row in cursor.fetchall():
        key = product_match_key(row["name"], row["brand"])
        if row["match_key"] != key:
            stale.append((row["id"], key))
    if stale:
        cursor.execute("""
            UPDATE products AS p SET match_key = v.key
            FROM unnest(%s::text[], %s::text[]) AS v(id, key)
            WHERE p.id = v.id
        """, ([s[0] for s in stale], [s[1] for s in stale]))
        conn.commit()
        print(f"[db] PRODUCT_MATCH_KEYS updated {len(stale)} product(s)", flush=True)
    return len(stale)


def seed_products(conn):
    """Seed the products table — idempotent, safe to run on every startup."""
    cursor = conn.cursor()
    now = now_iso()
    inserted = 0
    skipped = 0

    for product in SEED_PRODUCTS:
        name = product["name"]
        brand = product.get("brand")
        upc = product.get("upc")

        # Skip if this name+brand already exists (handles re-deploys and partial seeds)
        cursor.execute(
            "SELECT id FROM products WHERE name = %s AND (brand = %s OR (brand IS NULL AND %s IS NULL))",
            (name, brand, brand)
        )
        if cursor.fetchone():
            skipped += 1
            continue

        # Also skip if this UPC already exists (guards against data errors in seed list)
        if upc:
            cursor.execute("SELECT id FROM products WHERE upc = %s", (upc,))
            if cursor.fetchone():
                skipped += 1
                continue

        cursor.execute("""
            INSERT INTO products (id, name, brand, category, size, upc, image_url, scan_count, verified, source,
                                  match_key, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            generate_id(), name, brand, product["category"],
            product.get("size"), upc, None, 0, 1, 'seed',
            product_match_key(name, brand), now, now
        ))
        inserted += 1

    conn.commit()
    print(f"Products: {inserted} inserted, {skipped} already existed ({len(SEED_PRODUCTS)} total in catalog)")
