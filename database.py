import psycopg2
import psycopg2.extras
import psycopg2.pool
import os
import time
import threading
from contextlib import contextmanager
from typing import Optional
from seed_data import SEED_PRODUCTS
from helpers import generate_id, now_iso

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

        # Migrate users: add business_name, manager_name, stripe_customer_id, trial_reminder_sent_at columns if absent
        for col, col_type in [("business_name", "TEXT"), ("manager_name", "TEXT"), ("stripe_customer_id", "TEXT"), ("trial_reminder_sent_at", "TEXT")]:
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

        # Migrate products: add source, created_by_user_id, deleted_at if absent
        products_migrations = [
            ("source", "TEXT DEFAULT 'manual'"),
            ("created_by_user_id", "TEXT"),
            ("deleted_at", "TEXT"),
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

        # Backfill source: seed rows (verified=1) → 'seed', others keep 'manual'
        cursor.execute("""
            UPDATE products SET source = 'seed', updated_at = %s
            WHERE verified = 1 AND (source IS NULL OR source = 'manual')
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


        # Seed products — always runs but is idempotent (checks name+brand before insert)
        seed_products(conn)


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
            INSERT INTO products (id, name, brand, category, size, upc, image_url, scan_count, verified, source, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            generate_id(), name, brand, product["category"],
            product.get("size"), upc, None, 0, 1, 'seed', now, now
        ))
        inserted += 1

    conn.commit()
    print(f"Products: {inserted} inserted, {skipped} already existed ({len(SEED_PRODUCTS)} total in catalog)")
