import sqlite3
import os
from contextlib import contextmanager
from seed_data import SEED_PRODUCTS
from helpers import generate_id, now_iso

DATABASE_PATH = os.getenv("DATABASE_PATH", "86d.db")

@contextmanager
def get_db():
    """Get database connection with WAL mode enabled"""
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    
    # Enable WAL mode for concurrent access
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    
    try:
        yield conn
    finally:
        conn.close()

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
        
        conn.commit()
        
        # Seed products if table is empty
        cursor.execute("SELECT COUNT(*) FROM products")
        if cursor.fetchone()[0] == 0:
            seed_products(conn)

def seed_products(conn):
    """Seed the products table with initial data"""
    cursor = conn.cursor()
    now = now_iso()
    
    for product in SEED_PRODUCTS:
        cursor.execute("""
            INSERT INTO products (id, name, brand, category, size, upc, image_url, scan_count, verified, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            generate_id(),
            product["name"],
            product.get("brand"),
            product["category"],
            product.get("size"),
            product.get("upc"),
            None,  # image_url
            0,  # scan_count
            1,  # verified
            now,
            now
        ))
    
    conn.commit()
    print(f"Seeded {len(SEED_PRODUCTS)} products")