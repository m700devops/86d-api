from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timedelta
import sqlite3
import os
import json

app = FastAPI(title="86'd API", description="Bar inventory management backend")

# CORS for mobile app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup
DB_PATH = "86d.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Locations table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    # Products table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT,
            category TEXT,
            upc TEXT UNIQUE,
            size_ml INTEGER,
            abv REAL,
            scan_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Par levels table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS par_levels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER,
            product_id INTEGER,
            par_quantity INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (location_id) REFERENCES locations (id),
            FOREIGN KEY (product_id) REFERENCES products (id),
            UNIQUE(location_id, product_id)
        )
    """)
    
    # Inventory sessions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER,
            status TEXT DEFAULT 'active',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            duration_seconds INTEGER,
            FOREIGN KEY (location_id) REFERENCES locations (id)
        )
    """)
    
    # Scans table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            product_id INTEGER,
            level TEXT,
            level_decimal REAL,
            detection_method TEXT,
            photo_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES inventory_sessions (id),
            FOREIGN KEY (product_id) REFERENCES products (id)
        )
    """)
    
    # Voice notes table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voice_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            transcript TEXT,
            linked_product_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES inventory_sessions (id),
            FOREIGN KEY (linked_product_id) REFERENCES products (id)
        )
    """)
    
    # Orders table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            location_id INTEGER,
            status TEXT DEFAULT 'draft',
            items_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES inventory_sessions (id),
            FOREIGN KEY (location_id) REFERENCES locations (id)
        )
    """)
    
    # Usage history table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER,
            product_id INTEGER,
            quantity_used REAL,
            period_start DATE,
            period_end DATE,
            FOREIGN KEY (location_id) REFERENCES locations (id),
            FOREIGN KEY (product_id) REFERENCES products (id)
        )
    """)
    
    conn.commit()
    conn.close()

def seed_products():
    """Seed common bar products with real UPC codes"""
    products = [
        ("Tito's Handmade Vodka", "Tito's", "Vodka", "619947000006", 750, 40.0),
        ("Jameson Irish Whiskey", "Jameson", "Whiskey", "080480000036", 750, 40.0),
        ("Grey Goose Vodka", "Grey Goose", "Vodka", "000501000035", 750, 40.0),
        ("Patron Silver Tequila", "Patron", "Tequila", "072410000042", 750, 40.0),
        ("Jack Daniel's Old No. 7", "Jack Daniel's", "Whiskey", "000822000045", 750, 40.0),
        ("Bacardi Superior Rum", "Bacardi", "Rum", "008040000024", 750, 40.0),
        ("Captain Morgan Spiced Rum", "Captain Morgan", "Rum", "008040000107", 750, 35.0),
        ("Hennessy VS Cognac", "Hennessy", "Cognac", "008800000165", 750, 40.0),
        ("Jose Cuervo Especial", "Jose Cuervo", "Tequila", "008100000220", 750, 40.0),
        ("Smirnoff No. 21 Vodka", "Smirnoff", "Vodka", "008200000035", 750, 40.0),
        ("Absolut Vodka", "Absolut", "Vodka", "008320000045", 750, 40.0),
        ("Johnnie Walker Black Label", "Johnnie Walker", "Whiskey", "008800000052", 750, 40.0),
        ("Crown Royal Canadian Whisky", "Crown Royal", "Whiskey", "008200000120", 750, 40.0),
        ("Fireball Cinnamon Whisky", "Fireball", "Whiskey", "008860000035", 750, 33.0),
        ("Baileys Irish Cream", "Baileys", "Liqueur", "008670000020", 750, 17.0),
        ("Kahlua Coffee Liqueur", "Kahlua", "Liqueur", "008040000500", 750, 20.0),
        ("Grand Marnier", "Grand Marnier", "Liqueur", "008880000020", 750, 40.0),
        ("Cointreau", "Cointreau", "Liqueur", "008870000010", 750, 40.0),
        ("St-Germain Elderflower", "St-Germain", "Liqueur", "008100000350", 750, 20.0),
        ("Disaronno Amaretto", "Disaronno", "Liqueur", "008160000010", 750, 28.0),
    ]
    
    conn = get_db()
    cursor = conn.cursor()
    
    for name, brand, category, upc, size_ml, abv in products:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO products (name, brand, category, upc, size_ml, abv)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (name, brand, category, upc, size_ml, abv))
        except:
            pass
    
    conn.commit()
    conn.close()

# Initialize on startup
@app.on_event("startup")
async def startup():
    init_db()
    seed_products()

# Helper functions
def level_to_decimal(level: str) -> float:
    """Convert level string to decimal value"""
    levels = {
        "full": 1.0,
        "3/4": 0.75,
        "three quarters": 0.75,
        "half": 0.5,
        "1/2": 0.5,
        "1/4": 0.25,
        "quarter": 0.25,
        "empty": 0.0,
    }
    return levels.get(level.lower(), 0.0)

def check_variance(current_usage: float, avg_usage: float) -> Optional[str]:
    """Check if usage varies significantly from average"""
    if avg_usage == 0:
        return None
    
    ratio = current_usage / avg_usage
    
    if ratio >= 2.0:
        return f"HIGH: Usage {ratio:.1f}x above average"
    elif ratio <= 0.5:
        return f"LOW: Usage {ratio:.1f}x below average"
    
    return None

# Pydantic models
class ProductCreate(BaseModel):
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    upc: Optional[str] = None
    size_ml: Optional[int] = None
    abv: Optional[float] = None

class LocationCreate(BaseModel):
    user_id: Optional[int] = None
    name: str
    address: Optional[str] = None

class ParLevelCreate(BaseModel):
    product_id: int
    par_quantity: int = Field(default=0, ge=0)

class ScanCreate(BaseModel):
    product_id: int
    level: str
    detection_method: str = "camera"
    photo_url: Optional[str] = None

class VoiceNoteCreate(BaseModel):
    transcript: str
    linked_product_id: Optional[int] = None

class SyncData(BaseModel):
    location_id: int
    scans: List[dict] = []
    voice_notes: List[dict] = []

# Endpoints
@app.get("/")
async def root():
    return {
        "name": "86'd API",
        "version": "1.0",
        "description": "Bar inventory management backend",
        "endpoints": {
            "root": "GET /",
            "health": "GET /health",
            "products": {
                "list": "GET /products?category=",
                "search": "GET /products/search?q=",
                "barcode": "GET /products/barcode/{upc}",
                "create": "POST /products",
                "increment": "POST /products/{id}/increment-scan",
            },
            "locations": {
                "list": "GET /locations",
                "create": "POST /locations",
                "par_levels": "GET /locations/{id}/par-levels",
                "set_par": "POST /locations/{id}/par-levels",
            },
            "inventory": {
                "start": "POST /inventory/start?location_id=",
                "get": "GET /inventory/{session_id}",
                "scan": "POST /inventory/{session_id}/scan",
                "voice": "POST /inventory/{session_id}/voice",
                "complete": "POST /inventory/{session_id}/complete",
                "generate_order": "POST /inventory/{session_id}/generate-order",
            },
            "orders": {
                "get": "GET /orders/{order_id}",
                "export": "POST /orders/{order_id}/export?format=",
            },
            "sync": {
                "post": "POST /sync",
                "get": "GET /sync/{location_id}",
            }
        }
    }

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# Products endpoints
@app.get("/products")
async def list_products(category: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor()
    
    if category:
        cursor.execute("SELECT * FROM products WHERE category = ? ORDER BY scan_count DESC", (category,))
    else:
        cursor.execute("SELECT * FROM products ORDER BY scan_count DESC")
    
    products = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"products": products, "count": len(products)}

@app.get("/products/search")
async def search_products(q: str):
    conn = get_db()
    cursor = conn.cursor()
    
    search = f"%{q}%"
    cursor.execute("""
        SELECT * FROM products 
        WHERE name LIKE ? OR brand LIKE ? OR upc LIKE ?
        ORDER BY scan_count DESC
    """, (search, search, search))
    
    products = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"products": products, "count": len(products), "query": q}

@app.get("/products/barcode/{upc}")
async def get_product_by_barcode(upc: str):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM products WHERE upc = ?", (upc,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    
    return dict(row)

@app.post("/products")
async def create_product(product: ProductCreate):
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO products (name, brand, category, upc, size_ml, abv)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (product.name, product.brand, product.category, product.upc, product.size_ml, product.abv))
        
        product_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return {"id": product_id, "message": "Product created", "product": product.dict()}
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Product with this UPC already exists")

@app.post("/products/{product_id}/increment-scan")
async def increment_scan_count(product_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("UPDATE products SET scan_count = scan_count + 1 WHERE id = ?", (product_id,))
    
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Product not found")
    
    conn.commit()
    conn.close()
    return {"message": "Scan count incremented", "product_id": product_id}

# Locations endpoints
@app.get("/locations")
async def list_locations(user_id: Optional[int] = None):
    conn = get_db()
    cursor = conn.cursor()
    
    if user_id:
        cursor.execute("SELECT * FROM locations WHERE user_id = ?", (user_id,))
    else:
        cursor.execute("SELECT * FROM locations")
    
    locations = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"locations": locations, "count": len(locations)}

@app.post("/locations")
async def create_location(location: LocationCreate):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO locations (user_id, name, address)
        VALUES (?, ?, ?)
    """, (location.user_id, location.name, location.address))
    
    location_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return {"id": location_id, "message": "Location created", "location": location.dict()}

@app.get("/locations/{location_id}/par-levels")
async def get_par_levels(location_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT pl.*, p.name as product_name, p.brand, p.category
        FROM par_levels pl
        JOIN products p ON pl.product_id = p.id
        WHERE pl.location_id = ?
    """, (location_id,))
    
    levels = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"par_levels": levels, "count": len(levels)}

@app.post("/locations/{location_id}/par-levels")
async def set_par_level(location_id: int, par: ParLevelCreate):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO par_levels (location_id, product_id, par_quantity)
        VALUES (?, ?, ?)
        ON CONFLICT(location_id, product_id) 
        DO UPDATE SET par_quantity = ?, updated_at = CURRENT_TIMESTAMP
    """, (location_id, par.product_id, par.par_quantity, par.par_quantity))
    
    conn.commit()
    conn.close()
    return {"message": "Par level set", "location_id": location_id, "product_id": par.product_id, "par_quantity": par.par_quantity}

# Inventory endpoints
@app.post("/inventory/start")
async def start_inventory(location_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO inventory_sessions (location_id, status, started_at)
        VALUES (?, 'active', CURRENT_TIMESTAMP)
    """, (location_id,))
    
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return {"session_id": session_id, "location_id": location_id, "status": "active"}

@app.get("/inventory/{session_id}")
async def get_inventory_session(session_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    # Get session info
    cursor.execute("SELECT * FROM inventory_sessions WHERE id = ?", (session_id,))
    session = cursor.fetchone()
    
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    
    session_dict = dict(session)
    
    # Get scans
    cursor.execute("""
        SELECT s.*, p.name as product_name, p.brand
        FROM scans s
        JOIN products p ON s.product_id = p.id
        WHERE s.session_id = ?
        ORDER BY s.created_at
    """, (session_id,))
    scans = [dict(row) for row in cursor.fetchall()]
    
    # Get voice notes
    cursor.execute("SELECT * FROM voice_notes WHERE session_id = ? ORDER BY created_at", (session_id,))
    voice_notes = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return {
        "session": session_dict,
        "scans": scans,
        "voice_notes": voice_notes,
        "scan_count": len(scans)
    }

@app.post("/inventory/{session_id}/scan")
async def add_scan(session_id: int, scan: ScanCreate):
    level_decimal = level_to_decimal(scan.level)
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO scans (session_id, product_id, level, level_decimal, detection_method, photo_url)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (session_id, scan.product_id, scan.level, level_decimal, scan.detection_method, scan.photo_url))
    
    scan_id = cursor.lastrowid
    
    # Increment product scan count
    cursor.execute("UPDATE products SET scan_count = scan_count + 1 WHERE id = ?", (scan.product_id,))
    
    conn.commit()
    conn.close()
    
    return {"scan_id": scan_id, "message": "Scan recorded", "level_decimal": level_decimal}

@app.post("/inventory/{session_id}/voice")
async def add_voice_note(session_id: int, note: VoiceNoteCreate):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO voice_notes (session_id, transcript, linked_product_id)
        VALUES (?, ?, ?)
    """, (session_id, note.transcript, note.linked_product_id))
    
    note_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return {"note_id": note_id, "message": "Voice note recorded"}

@app.post("/inventory/{session_id}/complete")
async def complete_inventory(session_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    # Calculate duration
    cursor.execute("SELECT started_at FROM inventory_sessions WHERE id = ?", (session_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    
    started = datetime.fromisoformat(row['started_at'])
    now = datetime.now()
    duration = int((now - started).total_seconds())
    
    cursor.execute("""
        UPDATE inventory_sessions 
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP, duration_seconds = ?
        WHERE id = ?
    """, (duration, session_id))
    
    conn.commit()
    conn.close()
    
    return {"message": "Session completed", "session_id": session_id, "duration_seconds": duration}

@app.post("/inventory/{session_id}/generate-order")
async def generate_order(session_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    # Get session info
    cursor.execute("SELECT location_id FROM inventory_sessions WHERE id = ?", (session_id,))
    session = cursor.fetchone()
    
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    
    location_id = session['location_id']
    
    # Get latest scan for each product in this session
    cursor.execute("""
        SELECT s.product_id, s.level_decimal, p.name, p.brand, p.category
        FROM scans s
        JOIN products p ON s.product_id = p.id
        WHERE s.session_id = ?
        AND s.created_at = (
            SELECT MAX(created_at) 
            FROM scans 
            WHERE product_id = s.product_id AND session_id = ?
        )
    """, (session_id, session_id))
    
    current_inventory = {row['product_id']: dict(row) for row in cursor.fetchall()}
    
    # Get par levels for location
    cursor.execute("SELECT * FROM par_levels WHERE location_id = ?", (location_id,))
    par_levels = {row['product_id']: row['par_quantity'] for row in cursor.fetchall()}
    
    # Generate order items
    order_items = []
    
    for product_id, par_qty in par_levels.items():
        current = current_inventory.get(product_id, {})
        current_level = current.get('level_decimal', 0)
        
        # Calculate bottles needed (assuming 1.0 = full bottle)
        needed = max(0, par_qty - current_level)
        
        if needed > 0:
            product_info = current if current else {"name": "Unknown", "brand": "", "category": ""}
            
            order_items.append({
                "product_id": product_id,
                "product_name": product_info.get('name'),
                "brand": product_info.get('brand'),
                "category": product_info.get('category'),
                "par_quantity": par_qty,
                "current_level": current_level,
                "needed": needed,
            })
    
    # Save order
    cursor.execute("""
        INSERT INTO orders (session_id, location_id, items_json)
        VALUES (?, ?, ?)
    """, (session_id, location_id, json.dumps(order_items)))
    
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return {
        "order_id": order_id,
        "location_id": location_id,
        "items": order_items,
        "item_count": len(order_items)
    }

# Orders endpoints
@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()
    
    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")
    
    order_dict = dict(order)
    order_dict['items'] = json.loads(order_dict['items_json'])
    del order_dict['items_json']
    
    conn.close()
    return order_dict

@app.post("/orders/{order_id}/export")
async def export_order(order_id: int, format: str = "json"):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()
    
    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")
    
    items = json.loads(order['items_json'])
    
    if format == "text":
        lines = ["86'D ORDER", "=" * 40, ""]
        for item in items:
            lines.append(f"{item['brand']} {item['product_name']}")
            lines.append(f"  Category: {item['category']}")
            lines.append(f"  Par: {item['par_quantity']} | Current: {item['current_level']:.2f} | Need: {item['needed']:.2f}")
            lines.append("")
        
        return {"format": "text", "content": "\n".join(lines)}
    
    return {"format": "json", "order_id": order_id, "items": items}

# Sync endpoints
@app.post("/sync")
async def sync_data(data: SyncData):
    conn = get_db()
    cursor = conn.cursor()
    
    # Process scans
    scan_ids = []
    for scan_data in data.scans:
        cursor.execute("""
            INSERT INTO scans (session_id, product_id, level, level_decimal, detection_method, photo_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            scan_data.get('session_id'),
            scan_data.get('product_id'),
            scan_data.get('level'),
            level_to_decimal(scan_data.get('level', '')),
            scan_data.get('detection_method', 'camera'),
            scan_data.get('photo_url')
        ))
        scan_ids.append(cursor.lastrowid)
    
    # Process voice notes
    note_ids = []
    for note_data in data.voice_notes:
        cursor.execute("""
            INSERT INTO voice_notes (session_id, transcript, linked_product_id)
            VALUES (?, ?, ?)
        """, (
            note_data.get('session_id'),
            note_data.get('transcript'),
            note_data.get('linked_product_id')
        ))
        note_ids.append(cursor.lastrowid)
    
    conn.commit()
    conn.close()
    
    return {
        "message": "Sync completed",
        "scans_synced": len(scan_ids),
        "voice_notes_synced": len(note_ids)
    }

@app.get("/sync/{location_id}")
async def get_sync_data(location_id: int):
    conn = get_db()
    cursor = conn.cursor()
    
    # Get latest session
    cursor.execute("""
        SELECT * FROM inventory_sessions 
        WHERE location_id = ? 
        ORDER BY started_at DESC 
        LIMIT 1
    """, (location_id,))
    
    session = cursor.fetchone()
    
    if not session:
        conn.close()
        return {"location_id": location_id, "latest_session": None}
    
    session_id = session['id']
    
    # Get all data for location
    cursor.execute("SELECT * FROM par_levels WHERE location_id = ?", (location_id,))
    par_levels = [dict(row) for row in cursor.fetchall()]
    
    cursor.execute("SELECT * FROM scans WHERE session_id = ?", (session_id,))
    scans = [dict(row) for row in cursor.fetchall()]
    
    cursor.execute("SELECT * FROM voice_notes WHERE session_id = ?", (session_id,))
    voice_notes = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return {
        "location_id": location_id,
        "latest_session": dict(session),
        "par_levels": par_levels,
        "recent_scans": scans,
        "recent_voice_notes": voice_notes
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
