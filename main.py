from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import time
import uuid
import json

from database import init_db, get_db
from auth import (
    get_password_hash, verify_password, create_access_token, 
    create_refresh_token, verify_token, create_password_reset_token,
    verify_password_reset_token
)
from helpers import (
    generate_id, now_iso, level_to_decimal, decimal_to_level,
    calculate_variance, generate_order_items
)
from models import *
from seed_data import SEED_PRODUCTS

# Startup time for uptime calculation
START_TIME = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup"""
    init_db()
    yield

app = FastAPI(
    title="86'd API",
    description="Bar inventory management API for iOS app",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware - allow all origins for mobile app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["X-Request-Id", "X-RateLimit-Remaining", "X-RateLimit-Limit", "X-RateLimit-Reset", "Retry-After"]
)

# Gzip compression
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ============== MIDDLEWARE ==============

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Add request ID and rate limit headers"""
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    
    # Add rate limit headers (simplified)
    response.headers["X-RateLimit-Limit"] = "100"
    response.headers["X-RateLimit-Remaining"] = "95"
    response.headers["X-RateLimit-Reset"] = str(int(time.time() + 60))
    
    return response

# ============== V1 ROUTER ==============

v1_router = APIRouter(prefix="/v1")

# ============== DEPENDENCIES ==============

def get_current_user(authorization: str = Header(None)) -> str:
    """Extract and verify JWT token from Authorization header"""
    if not authorization:
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "message": "Authorization header required"
        })
    
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail={
            "error": "unauthorized",
            "message": "Invalid authorization scheme"
        })
    
    user_id = verify_token(token, "access")
    if not user_id:
        raise HTTPException(status_code=401, detail={
            "error": "token_expired",
            "message": "Token expired or invalid"
        })
    
    return user_id

# ============== HEALTH & INFO ==============

@app.get("/", response_model=APIInfoResponse)
def root():
    """API info and endpoints list"""
    return {
        "name": "86'd API",
        "version": "1.0.0",
        "status": "healthy",
        "docs": "/docs",
        "endpoints": {
            "auth": ["/v1/auth/register", "/v1/auth/login", "/v1/auth/refresh", "/v1/auth/forgot-password", "/v1/auth/reset-password"],
            "products": ["/v1/products", "/v1/products/search", "/v1/products/barcode/{upc}"],
            "locations": ["/v1/locations", "/v1/locations/{id}/par-levels"],
            "inventory": ["/v1/inventory/start", "/v1/inventory/{id}", "/v1/inventory/{id}/scan"],
            "orders": ["/v1/orders", "/v1/orders/{id}", "/v1/orders/{id}/prepare-emails", "/v1/orders/{id}/export"],
            "sync": ["/v1/sync"],
            "distributors": ["/v1/distributors"],
            "users": ["/v1/users/me"]
        }
    }

@app.get("/health", response_model=HealthResponse)
def health_check():
    """Health check with database connectivity test"""
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
        return {
            "status": "healthy",
            "database": "connected",
            "timestamp": now_iso(),
            "uptime_seconds": int(time.time() - START_TIME)
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "error": str(e)
            }
        )

# ============== AUTHENTICATION ==============

@v1_router.post("/auth/register", response_model=TokenResponse, status_code=201)
def register(user_data: UserCreate):
    """Create new user account"""
    import sys
    import traceback
    
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Check if terms were accepted
            if not getattr(user_data, 'terms_accepted', False):
                raise HTTPException(status_code=400, detail={
                    "error": "terms_not_accepted",
                    "message": "You must accept the terms of service to register"
                })
            
            # Check if email exists
            cursor.execute(
                "SELECT id FROM users WHERE email = ? AND deleted_at IS NULL",
                (user_data.email.lower().strip(),)
            )
            if cursor.fetchone():
                raise HTTPException(status_code=400, detail={
                    "error": "email_exists",
                    "message": "An account with this email already exists"
                })
            
            # Create user with trial
            user_id = generate_id()
            now = now_iso()
            password_hash = get_password_hash(user_data.password)
            trial_ends = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
            
            cursor.execute("""
                INSERT INTO users (id, email, password_hash, name, terms_accepted_at, privacy_accepted_at,
                                   trial_started_at, trial_ends_at, subscription_status, subscription_tier,
                                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                user_data.email.lower().strip(),
                password_hash,
                user_data.name,
                now,  # terms_accepted_at
                now,  # privacy_accepted_at
                now,  # trial_started_at
                trial_ends,
                'trial',
                'starter',
                now,
                now
            ))
            conn.commit()
            
            # Generate tokens
            access_token = create_access_token(user_id)
            refresh_token = create_refresh_token(user_id)
            
            return {
                "user": {
                    "id": user_id,
                    "email": user_data.email.lower().strip(),
                    "name": user_data.name,
                    "subscription_status": "trial",
                    "subscription_tier": "starter",
                    "trial_ends_at": trial_ends,
                    "terms_accepted_at": now,
                    "privacy_accepted_at": now,
                    "created_at": now
                },
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": 3600
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"REGISTER ERROR: {e}", file=sys.stderr)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail={
            "error": "server_error",
            "message": "An unexpected error occurred",
            "debug": str(e)
        })

@v1_router.post("/auth/login", response_model=TokenResponse)
def login(credentials: UserLogin):
    """Authenticate and get tokens"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Find user
        cursor.execute(
            """SELECT id, email, password_hash, name, subscription_status, subscription_tier,
                      trial_ends_at, terms_accepted_at, privacy_accepted_at, created_at 
               FROM users WHERE email = ? AND deleted_at IS NULL""",
            (credentials.email.lower().strip(),)
        )
        row = cursor.fetchone()
        
        if not row or not verify_password(credentials.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail={
                "error": "invalid_credentials",
                "message": "Invalid email or password"
            })
        
        # Generate tokens
        access_token = create_access_token(row["id"])
        refresh_token = create_refresh_token(row["id"])
        
        return {
            "user": {
                "id": row["id"],
                "email": row["email"],
                "name": row["name"],
                "subscription_status": row["subscription_status"],
                "subscription_tier": row["subscription_tier"],
                "trial_ends_at": row["trial_ends_at"],
                "terms_accepted_at": row["terms_accepted_at"],
                "privacy_accepted_at": row["privacy_accepted_at"],
                "created_at": row["created_at"]
            },
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 3600
        }

@v1_router.post("/auth/refresh", response_model=RefreshResponse)
def refresh_token(refresh_data: RefreshRequest):
    """Get new access token using refresh token"""
    user_id = verify_token(refresh_data.refresh_token, "refresh")
    if not user_id:
        raise HTTPException(status_code=401, detail={
            "error": "token_expired",
            "message": "Refresh token expired or invalid"
        })
    
    access_token = create_access_token(user_id)
    return {
        "access_token": access_token,
        "expires_in": 3600
    }

# ============== PRODUCTS ==============

@v1_router.get("/products", response_model=ProductListResponse)
def list_products(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    category: Optional[str] = Query(None, pattern="^(spirits|beer|wine|other)$"),
    sort: str = Query("name", pattern="^(name|scan_count|created_at)$")
):
    """List products with pagination and filters"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Build query
        where_clause = "WHERE 1=1"
        params = []
        if category:
            where_clause += " AND category = ?"
            params.append(category)
        
        # Get total count
        cursor.execute(f"SELECT COUNT(*) FROM products {where_clause}", params)
        total = cursor.fetchone()[0]
        
        # Get products
        order_by = {
            "name": "name ASC",
            "scan_count": "scan_count DESC",
            "created_at": "created_at DESC"
        }.get(sort, "name ASC")
        
        cursor.execute(f"""
            SELECT * FROM products 
            {where_clause}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
        """, params + [limit, offset])
        
        products = [dict(row) for row in cursor.fetchall()]
        for p in products:
            p["verified"] = bool(p["verified"])
        
        return {
            "products": products,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total
        }

@v1_router.get("/products/search", response_model=ProductSearchResponse)
def search_products(
    q: str = Query(..., min_length=2),
    limit: int = Query(20, ge=1, le=50)
):
    """Search products by name, brand, or UPC"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        search_term = f"%{q}%"
        cursor.execute("""
            SELECT * FROM products 
            WHERE name LIKE ? OR brand LIKE ? OR upc LIKE ?
            ORDER BY 
                CASE WHEN name LIKE ? THEN 0 ELSE 1 END,
                scan_count DESC
            LIMIT ?
        """, (search_term, search_term, search_term, f"%{q}%", limit))
        
        products = [dict(row) for row in cursor.fetchall()]
        for p in products:
            p["verified"] = bool(p["verified"])
        
        return {
            "products": products,
            "query": q,
            "total": len(products)
        }

@v1_router.get("/products/barcode/{upc}", response_model=dict)
def get_product_by_barcode(upc: str):
    """Lookup product by UPC barcode"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM products WHERE upc = ?", (upc,))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "product_not_found",
                "message": f"No product found with UPC {upc}",
                "upc": upc
            })
        
        product = dict(row)
        product["verified"] = bool(product["verified"])
        return {"product": product}

@v1_router.post("/products", response_model=dict, status_code=201)
def create_product(product_data: ProductCreate, user_id: str = Depends(get_current_user)):
    """Add new product"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check UPC if provided
        if product_data.upc:
            cursor.execute("SELECT * FROM products WHERE upc = ?", (product_data.upc,))
            existing = cursor.fetchone()
            if existing:
                raise HTTPException(status_code=409, detail={
                    "error": "upc_exists",
                    "existing_product": dict(existing)
                })
        
        # Create product
        product_id = generate_id()
        now = now_iso()
        
        cursor.execute("""
            INSERT INTO products (id, name, brand, category, size, upc, image_url, scan_count, verified, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            product_id,
            product_data.name,
            product_data.brand,
            product_data.category,
            product_data.size,
            product_data.upc,
            None,  # image_url
            0,  # scan_count
            0,  # verified
            now,
            now
        ))
        conn.commit()
        
        return {
            "product": {
                "id": product_id,
                "name": product_data.name,
                "brand": product_data.brand,
                "category": product_data.category,
                "size": product_data.size,
                "upc": product_data.upc,
                "image_url": None,
                "scan_count": 0,
                "verified": False,
                "created_at": now,
                "updated_at": now
            }
        }

@v1_router.post("/products/{product_id}/increment-scan", response_model=ScanCountResponse)
def increment_scan_count(product_id: str):
    """Increment scan count (call when product is scanned)"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE products 
            SET scan_count = scan_count + 1, updated_at = ?
            WHERE id = ?
        """, (now_iso(), product_id))
        conn.commit()
        
        cursor.execute("SELECT scan_count FROM products WHERE id = ?", (product_id,))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Product not found"
            })
        
        return {"scan_count": row["scan_count"]}

# ============== LOCATIONS ==============

@v1_router.get("/locations", response_model=LocationListResponse)
def list_locations(user_id: str = Depends(get_current_user)):
    """List user's locations"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM locations 
            WHERE user_id = ? AND deleted_at IS NULL
            ORDER BY created_at DESC
        """, (user_id,))
        
        locations = [dict(row) for row in cursor.fetchall()]
        return {"locations": locations}

@v1_router.post("/locations", response_model=dict, status_code=201)
def create_location(location_data: LocationCreate, user_id: str = Depends(get_current_user)):
    """Create new location"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        location_id = generate_id()
        now = now_iso()
        
        cursor.execute("""
            INSERT INTO locations (id, user_id, name, address, timezone, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            location_id,
            user_id,
            location_data.name,
            location_data.address,
            location_data.timezone,
            now,
            now
        ))
        conn.commit()
        
        return {
            "location": {
                "id": location_id,
                "user_id": user_id,
                "name": location_data.name,
                "address": location_data.address,
                "timezone": location_data.timezone,
                "created_at": now,
                "updated_at": now
            }
        }

@v1_router.get("/locations/{location_id}/par-levels", response_model=ParLevelListResponse)
def get_par_levels(location_id: str, user_id: str = Depends(get_current_user)):
    """Get all par levels for a location"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify location belongs to user
        cursor.execute(
            "SELECT id FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={
                "error": "forbidden",
                "message": "Access denied to this location"
            })
        
        cursor.execute("""
            SELECT pl.*, p.id as product_id, p.name, p.brand, p.category, p.size, p.upc, 
                   p.image_url, p.scan_count, p.verified, p.created_at as product_created_at, 
                   p.updated_at as product_updated_at
            FROM par_levels pl
            JOIN products p ON pl.product_id = p.id
            WHERE pl.location_id = ?
        """, (location_id,))
        
        rows = cursor.fetchall()
        par_levels = []
        for row in rows:
            pl = {
                "id": row["id"],
                "location_id": row["location_id"],
                "product_id": row["product_id"],
                "par_quantity": row["par_quantity"],
                "updated_at": row["updated_at"],
                "product": {
                    "id": row["product_id"],
                    "name": row["name"],
                    "brand": row["brand"],
                    "category": row["category"],
                    "size": row["size"],
                    "upc": row["upc"],
                    "image_url": row["image_url"],
                    "scan_count": row["scan_count"],
                    "verified": bool(row["verified"]),
                    "created_at": row["product_created_at"],
                    "updated_at": row["product_updated_at"]
                }
            }
            par_levels.append(pl)
        
        return {"par_levels": par_levels}

@v1_router.post("/locations/{location_id}/par-levels", response_model=dict)
def set_par_level(location_id: str, par_data: ParLevelCreate, user_id: str = Depends(get_current_user)):
    """Set or update par level for a product"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify location belongs to user
        cursor.execute(
            "SELECT id FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={
                "error": "forbidden",
                "message": "Access denied to this location"
            })
        
        # Verify product exists
        cursor.execute("SELECT id FROM products WHERE id = ?", (par_data.product_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Product not found"
            })
        
        now = now_iso()
        par_id = generate_id()
        
        cursor.execute("""
            INSERT INTO par_levels (id, location_id, product_id, par_quantity, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(location_id, product_id) DO UPDATE SET
                par_quantity = excluded.par_quantity,
                updated_at = excluded.updated_at
        """, (par_id, location_id, par_data.product_id, par_data.par_quantity, now))
        conn.commit()
        
        # Get the actual ID (either inserted or existing)
        cursor.execute(
            "SELECT id FROM par_levels WHERE location_id = ? AND product_id = ?",
            (location_id, par_data.product_id)
        )
        par_id = cursor.fetchone()["id"]
        
        return {
            "par_level": {
                "id": par_id,
                "location_id": location_id,
                "product_id": par_data.product_id,
                "par_quantity": par_data.par_quantity,
                "updated_at": now
            }
        }

@v1_router.post("/locations/{location_id}/par-levels/bulk", response_model=ParLevelBulkResponse)
def set_par_levels_bulk(location_id: str, bulk_data: ParLevelBulkRequest, user_id: str = Depends(get_current_user)):
    """Set multiple par levels at once"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify location belongs to user
        cursor.execute(
            "SELECT id FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={
                "error": "forbidden",
                "message": "Access denied to this location"
            })
        
        now = now_iso()
        updated = 0
        par_levels = []
        
        for par_data in bulk_data.par_levels:
            par_id = generate_id()
            cursor.execute("""
                INSERT INTO par_levels (id, location_id, product_id, par_quantity, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(location_id, product_id) DO UPDATE SET
                    par_quantity = excluded.par_quantity,
                    updated_at = excluded.updated_at
            """, (par_id, location_id, par_data.product_id, par_data.par_quantity, now))
            updated += 1
            
            cursor.execute(
                "SELECT id FROM par_levels WHERE location_id = ? AND product_id = ?",
                (location_id, par_data.product_id)
            )
            actual_id = cursor.fetchone()["id"]
            
            par_levels.append({
                "id": actual_id,
                "location_id": location_id,
                "product_id": par_data.product_id,
                "par_quantity": par_data.par_quantity,
                "updated_at": now
            })
        
        conn.commit()
        
        return {
            "updated": updated,
            "par_levels": par_levels
        }

# ============== INVENTORY SESSIONS ==============

@v1_router.post("/inventory/start", response_model=dict, status_code=201)
def start_inventory(session_data: InventorySessionCreate, user_id: str = Depends(get_current_user)):
    """Start new inventory session"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify location belongs to user
        cursor.execute(
            "SELECT id FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (session_data.location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={
                "error": "forbidden",
                "message": "Access denied to this location"
            })
        
        # Check for existing active session
        cursor.execute("""
            SELECT id, started_at, user_id FROM inventory_sessions
            WHERE location_id = ? AND status = 'in_progress'
        """, (session_data.location_id,))
        existing = cursor.fetchone()
        
        if existing:
            raise HTTPException(status_code=409, detail={
                "error": "session_exists",
                "message": "An inventory session is already in progress for this location",
                "existing_session": {
                    "id": existing["id"],
                    "started_at": existing["started_at"],
                    "user_id": existing["user_id"]
                },
                "options": ["resume", "cancel_and_start_new"]
            })
        
        # Create session
        session_id = generate_id()
        now = now_iso()
        
        cursor.execute("""
            INSERT INTO inventory_sessions (id, location_id, user_id, started_at, status, device_id, app_version, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'in_progress', ?, ?, ?, ?)
        """, (session_id, session_data.location_id, user_id, now, session_data.device_id, session_data.app_version, now, now))
        conn.commit()
        
        return {
            "session": {
                "id": session_id,
                "location_id": session_data.location_id,
                "user_id": user_id,
                "started_at": now,
                "status": "in_progress"
            }
        }

@v1_router.get("/inventory/{session_id}", response_model=InventorySessionDetailResponse)
def get_inventory_session(session_id: str, user_id: str = Depends(get_current_user)):
    """Get session with all scans and voice notes"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Get session
        cursor.execute("""
            SELECT s.*, l.name as location_name
            FROM inventory_sessions s
            JOIN locations l ON s.location_id = l.id
            WHERE s.id = ? AND s.user_id = ?
        """, (session_id, user_id))
        session_row = cursor.fetchone()
        
        if not session_row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Session not found"
            })
        
        session = dict(session_row)
        session.pop("location_name", None)
        
        # Get scans with product info
        cursor.execute("""
            SELECT sc.*, p.id as product_id, p.name, p.brand, p.category, p.size, p.upc,
                   p.image_url, p.scan_count, p.verified, p.created_at as product_created_at,
                   p.updated_at as product_updated_at
            FROM scans sc
            JOIN products p ON sc.product_id = p.id
            WHERE sc.session_id = ?
            ORDER BY sc.created_at DESC
        """, (session_id,))
        
        scans = []
        for row in cursor.fetchall():
            scan = {
                "id": row["id"],
                "session_id": row["session_id"],
                "product_id": row["product_id"],
                "level": row["level"],
                "level_decimal": row["level_decimal"],
                "quantity": row["quantity"],
                "detection_method": row["detection_method"],
                "confidence": row["confidence"],
                "photo_url": row["photo_url"],
                "shelf_location": row["shelf_location"],
                "notes": row["notes"],
                "idempotency_key": row["idempotency_key"],
                "synced_at": row["synced_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "product": {
                    "id": row["product_id"],
                    "name": row["name"],
                    "brand": row["brand"],
                    "category": row["category"],
                    "size": row["size"],
                    "upc": row["upc"],
                    "image_url": row["image_url"],
                    "scan_count": row["scan_count"],
                    "verified": bool(row["verified"]),
                    "created_at": row["product_created_at"],
                    "updated_at": row["product_updated_at"]
                }
            }
            scans.append(scan)
        
        # Get voice notes
        cursor.execute("SELECT * FROM voice_notes WHERE session_id = ? ORDER BY created_at DESC", (session_id,))
        voice_notes = [dict(row) for row in cursor.fetchall()]
        for vn in voice_notes:
            vn["processed"] = bool(vn["processed"])
        
        return {
            "session": session,
            "scans": scans,
            "voice_notes": voice_notes
        }

@v1_router.post("/inventory/{session_id}/scan", response_model=dict, status_code=201)
def add_scan(session_id: str, scan_data: ScanCreate, user_id: str = Depends(get_current_user)):
    """Add bottle scan to session"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify session belongs to user and is in progress
        cursor.execute(
            "SELECT id, location_id FROM inventory_sessions WHERE id = ? AND user_id = ? AND status = 'in_progress'",
            (session_id, user_id)
        )
        session = cursor.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Active session not found"
            })
        
        # Check idempotency key
        if scan_data.idempotency_key:
            cursor.execute(
                "SELECT * FROM scans WHERE idempotency_key = ?",
                (scan_data.idempotency_key,)
            )
            existing = cursor.fetchone()
            if existing:
                raise HTTPException(status_code=409, detail={
                    "error": "duplicate_scan",
                    "message": "Scan already recorded",
                    "existing_scan": dict(existing)
                })
        
        # Create scan
        scan_id = generate_id()
        now = now_iso()
        level_decimal = level_to_decimal(scan_data.level)
        created_at = scan_data.created_at.isoformat() if scan_data.created_at else now
        
        cursor.execute("""
            INSERT INTO scans (id, session_id, product_id, level, level_decimal, quantity, 
                              detection_method, confidence, photo_url, shelf_location, notes, 
                              idempotency_key, synced_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_id, session_id, scan_data.product_id, scan_data.level, level_decimal,
            scan_data.quantity, scan_data.detection_method, scan_data.confidence,
            scan_data.photo_url, scan_data.shelf_location, scan_data.notes,
            scan_data.idempotency_key, now, created_at, now
        ))
        
        # Increment product scan count
        cursor.execute(
            "UPDATE products SET scan_count = scan_count + 1, updated_at = ? WHERE id = ?",
            (now, scan_data.product_id)
        )
        
        conn.commit()
        
        # Get total scans for session
        cursor.execute("SELECT COUNT(*) FROM scans WHERE session_id = ?", (session_id,))
        total = cursor.fetchone()[0]
        
        return {
            "scan": {
                "id": scan_id,
                "session_id": session_id,
                "product_id": scan_data.product_id,
                "level": scan_data.level,
                "level_decimal": level_decimal,
                "quantity": scan_data.quantity,
                "detection_method": scan_data.detection_method,
                "confidence": scan_data.confidence,
                "photo_url": scan_data.photo_url,
                "shelf_location": scan_data.shelf_location,
                "notes": scan_data.notes,
                "idempotency_key": scan_data.idempotency_key,
                "synced_at": now,
                "created_at": created_at,
                "updated_at": now
            },
            "session_total": total
        }

@v1_router.post("/inventory/{session_id}/scan/bulk", response_model=ScanBulkResponse, status_code=201)
def add_scans_bulk(session_id: str, bulk_data: ScanBulkRequest, user_id: str = Depends(get_current_user)):
    """Add multiple scans at once"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify session belongs to user and is in progress
        cursor.execute(
            "SELECT id FROM inventory_sessions WHERE id = ? AND user_id = ? AND status = 'in_progress'",
            (session_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Active session not found"
            })
        
        created = 0
        duplicates = 0
        scans = []
        now = now_iso()
        
        for scan_data in bulk_data.scans:
            # Check idempotency key
            if scan_data.idempotency_key:
                cursor.execute(
                    "SELECT * FROM scans WHERE idempotency_key = ?",
                    (scan_data.idempotency_key,)
                )
                if cursor.fetchone():
                    duplicates += 1
                    continue
            
            # Create scan
            scan_id = generate_id()
            level_decimal = level_to_decimal(scan_data.level)
            created_at = scan_data.created_at.isoformat() if scan_data.created_at else now
            
            cursor.execute("""
                INSERT INTO scans (id, session_id, product_id, level, level_decimal, quantity, 
                                  detection_method, confidence, photo_url, shelf_location, notes, 
                                  idempotency_key, synced_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                scan_id, session_id, scan_data.product_id, scan_data.level, level_decimal,
                scan_data.quantity, scan_data.detection_method, scan_data.confidence,
                scan_data.photo_url, scan_data.shelf_location, scan_data.notes,
                scan_data.idempotency_key, now, created_at, now
            ))
            
            # Increment product scan count
            cursor.execute(
                "UPDATE products SET scan_count = scan_count + 1, updated_at = ? WHERE id = ?",
                (now, scan_data.product_id)
            )
            
            created += 1
            scans.append({
                "id": scan_id,
                "session_id": session_id,
                "product_id": scan_data.product_id,
                "level": scan_data.level,
                "level_decimal": level_decimal,
                "quantity": scan_data.quantity,
                "detection_method": scan_data.detection_method,
                "confidence": scan_data.confidence,
                "photo_url": scan_data.photo_url,
                "shelf_location": scan_data.shelf_location,
                "notes": scan_data.notes,
                "idempotency_key": scan_data.idempotency_key,
                "synced_at": now,
                "created_at": created_at,
                "updated_at": now
            })
        
        conn.commit()
        
        return {
            "created": created,
            "duplicates": duplicates,
            "scans": scans
        }

# ============== PEN CAPTURE MODE ==============

from pydantic import BaseModel
from typing import Optional, List

class PenCaptureRequest(BaseModel):
    session_id: str
    bottle_image_base64: Optional[str] = None
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    level: float
    pen_position_y: float
    captured_at: Optional[str] = None
    confidence: float

class PenCaptureResponse(BaseModel):
    scan_id: str
    status: str
    bottle_number: int
    product: Optional[dict] = None

class BatchCaptureRequest(BaseModel):
    session_id: str
    captures: List[PenCaptureRequest]

class BatchCaptureResponse(BaseModel):
    processed: int
    failed: int
    bottles: List[dict]

@v1_router.post("/scans/pen-capture", response_model=PenCaptureResponse, status_code=201)
def pen_capture(capture_data: PenCaptureRequest, user_id: str = Depends(get_current_user)):
    """Handle rapid-fire bottle captures from continuous pen scanning mode"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify session belongs to user and is in progress
        cursor.execute(
            "SELECT id, location_id FROM inventory_sessions WHERE id = ? AND user_id = ? AND status = 'in_progress'",
            (capture_data.session_id, user_id)
        )
        session = cursor.fetchone()
        if not session:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Active session not found"
            })
        
        now = now_iso()
        created_at = capture_data.captured_at if capture_data.captured_at else now
        
        # Determine product
        product = None
        product_id = capture_data.product_id
        
        if product_id:
            # Get existing product
            cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
            product = cursor.fetchone()
        elif capture_data.product_name:
            # Try to find product by name
            cursor.execute(
                "SELECT * FROM products WHERE LOWER(name) = LOWER(?) OR LOWER(brand || ' ' || name) = LOWER(?)",
                (capture_data.product_name, capture_data.product_name)
            )
            product = cursor.fetchone()
            if product:
                product_id = product["id"]
        
        # If no product found, create a placeholder
        if not product_id:
            product_id = generate_id()
            cursor.execute("""
                INSERT INTO products (id, name, brand, category, size, upc, image_url, scan_count, verified, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                product_id,
                capture_data.product_name or "Unknown Product",
                None,
                "uncategorized",
                None,
                None,
                None,
                0,
                0,
                now,
                now
            ))
            cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
            product = cursor.fetchone()
        
        # Convert level to level string
        level_decimal = max(0.0, min(1.0, capture_data.level))
        if level_decimal >= 0.875:
            level = "full"
        elif level_decimal >= 0.625:
            level = "3/4"
        elif level_decimal >= 0.375:
            level = "half"
        elif level_decimal >= 0.125:
            level = "1/4"
        else:
            level = "empty"
        
        # Create scan
        scan_id = generate_id()
        cursor.execute("""
            INSERT INTO scans (id, session_id, product_id, level, level_decimal, quantity, 
                              detection_method, confidence, pen_position_y, capture_method,
                              synced_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_id, capture_data.session_id, product_id, level, level_decimal,
            1, "camera", capture_data.confidence, capture_data.pen_position_y, "pen_mode",
            now, created_at, now
        ))
        
        # Increment product scan count
        cursor.execute(
            "UPDATE products SET scan_count = scan_count + 1, updated_at = ? WHERE id = ?",
            (now, product_id)
        )
        
        conn.commit()
        
        # Get bottle number (count of scans in this session)
        cursor.execute("SELECT COUNT(*) FROM scans WHERE session_id = ?", (capture_data.session_id,))
        bottle_number = cursor.fetchone()[0]
        
        return {
            "scan_id": scan_id,
            "status": "captured",
            "bottle_number": bottle_number,
            "product": dict(product) if product else None
        }

@v1_router.post("/scans/batch", response_model=BatchCaptureResponse, status_code=201)
def batch_capture(batch_data: BatchCaptureRequest, user_id: str = Depends(get_current_user)):
    """Sync multiple captures at once (offline support)"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify session belongs to user and is in progress
        cursor.execute(
            "SELECT id FROM inventory_sessions WHERE id = ? AND user_id = ? AND status = 'in_progress'",
            (batch_data.session_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Active session not found"
            })
        
        processed = 0
        failed = 0
        bottles = []
        now = now_iso()
        
        for capture in batch_data.captures:
            try:
                created_at = capture.captured_at if capture.captured_at else now
                
                # Determine product
                product_id = capture.product_id
                if not product_id and capture.product_name:
                    cursor.execute(
                        "SELECT id FROM products WHERE LOWER(name) = LOWER(?) OR LOWER(brand || ' ' || name) = LOWER(?)",
                        (capture.product_name, capture.product_name)
                    )
                    row = cursor.fetchone()
                    if row:
                        product_id = row["id"]
                
                # If no product found, create placeholder
                if not product_id:
                    product_id = generate_id()
                    cursor.execute("""
                        INSERT INTO products (id, name, brand, category, size, upc, image_url, scan_count, verified, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        product_id,
                        capture.product_name or "Unknown Product",
                        None,
                        "uncategorized",
                        None,
                        None,
                        None,
                        0,
                        0,
                        now,
                        now
                    ))
                
                # Convert level
                level_decimal = max(0.0, min(1.0, capture.level))
                if level_decimal >= 0.875:
                    level = "full"
                elif level_decimal >= 0.625:
                    level = "3/4"
                elif level_decimal >= 0.375:
                    level = "half"
                elif level_decimal >= 0.125:
                    level = "1/4"
                else:
                    level = "empty"
                
                # Create scan
                scan_id = generate_id()
                cursor.execute("""
                    INSERT INTO scans (id, session_id, product_id, level, level_decimal, quantity, 
                                      detection_method, confidence, pen_position_y, capture_method,
                                      synced_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    scan_id, batch_data.session_id, product_id, level, level_decimal,
                    1, "camera", capture.confidence, capture.pen_position_y, "pen_mode",
                    now, created_at, now
                ))
                
                # Increment product scan count
                cursor.execute(
                    "UPDATE products SET scan_count = scan_count + 1, updated_at = ? WHERE id = ?",
                    (now, product_id)
                )
                
                processed += 1
                bottles.append({
                    "scan_id": scan_id,
                    "product_id": product_id,
                    "level": level,
                    "level_decimal": level_decimal,
                    "confidence": capture.confidence
                })
                
            except Exception as e:
                failed += 1
                continue
        
        conn.commit()
        
        return {
            "processed": processed,
            "failed": failed,
            "bottles": bottles
        }

@v1_router.post("/inventory/{session_id}/voice", response_model=dict, status_code=201)
def add_voice_note(session_id: str, voice_data: VoiceNoteCreate, user_id: str = Depends(get_current_user)):
    """Add voice note to session"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify session belongs to user
        cursor.execute(
            "SELECT id FROM inventory_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Session not found"
            })
        
        note_id = generate_id()
        now = now_iso()
        
        cursor.execute("""
            INSERT INTO voice_notes (id, session_id, audio_url, transcript, linked_product_id, duration_seconds, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (note_id, session_id, voice_data.audio_url, voice_data.transcript,
              voice_data.linked_product_id, voice_data.duration_seconds, now))
        conn.commit()
        
        return {
            "voice_note": {
                "id": note_id,
                "session_id": session_id,
                "audio_url": voice_data.audio_url,
                "transcript": voice_data.transcript,
                "linked_product_id": voice_data.linked_product_id,
                "duration_seconds": voice_data.duration_seconds,
                "processed": False,
                "created_at": now
            }
        }

@v1_router.post("/inventory/{session_id}/complete", response_model=InventoryCompleteResponse)
def complete_inventory(session_id: str, user_id: str = Depends(get_current_user)):
    """Mark session as complete and generate order"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Get session with location
        cursor.execute("""
            SELECT s.*, l.name as location_name
            FROM inventory_sessions s
            JOIN locations l ON s.location_id = l.id
            WHERE s.id = ? AND s.user_id = ? AND s.status = 'in_progress'
        """, (session_id, user_id))
        session = cursor.fetchone()
        
        if not session:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Active session not found"
            })
        
        location_id = session["location_id"]
        location_name = session["location_name"]
        started_at = datetime.fromisoformat(session["started_at"])
        
        # Get all scans for this session
        cursor.execute("""
            SELECT s.*, p.name as product_name, p.category
            FROM scans s
            JOIN products p ON s.product_id = p.id
            WHERE s.session_id = ?
        """, (session_id,))
        scans = [dict(row) for row in cursor.fetchall()]
        
        # Get par levels for location
        cursor.execute("SELECT product_id, par_quantity FROM par_levels WHERE location_id = ?", (location_id,))
        par_levels = {row["product_id"]: row["par_quantity"] for row in cursor.fetchall()}
        
        # Generate order items
        order_items = generate_order_items(scans, par_levels)
        
        # Get usage history for variance alerts
        variance_alerts = []
        for scan in scans:
            product_id = scan["product_id"]
            cursor.execute("""
                SELECT bottles_used FROM usage_history
                WHERE location_id = ? AND product_id = ?
                ORDER BY period_start DESC
                LIMIT 4
            """, (location_id, product_id))
            history = [row["bottles_used"] for row in cursor.fetchall()]
            
            # Calculate current usage (this is simplified - would need previous session data)
            # For now, use scan quantity as proxy
            current_usage = scan["level_decimal"] + (scan.get("quantity", 1) - 1)
            
            alert = calculate_variance(current_usage, history)
            if alert:
                alert["product_id"] = product_id
                alert["product_name"] = scan["product_name"]
                variance_alerts.append(alert)
        
        # Complete session
        now = now_iso()
        completed_at = datetime.now(timezone.utc)
        duration_seconds = int((completed_at - started_at).total_seconds())
        total_bottles = len(scans)
        
        cursor.execute("""
            UPDATE inventory_sessions
            SET status = 'completed', completed_at = ?, total_bottles = ?, duration_seconds = ?, updated_at = ?
            WHERE id = ?
        """, (now, total_bottles, duration_seconds, now, session_id))
        
        # Create order
        order_id = generate_id()
        order_data = {
            "items": order_items,
            "total_items": len(order_items),
            "variance_alerts": variance_alerts
        }
        
        import json
        cursor.execute("""
            INSERT INTO orders (id, session_id, location_id, order_data, total_items, variance_alerts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id, session_id, location_id,
            json.dumps({"items": order_items}),
            len(order_items),
            json.dumps(variance_alerts),
            now
        ))
        conn.commit()
        
        # Build order items with product names
        order_items_response = []
        for item in order_items:
            cursor.execute("SELECT name, category FROM products WHERE id = ?", (item["product_id"],))
            product = cursor.fetchone()
            order_items_response.append({
                "product_id": item["product_id"],
                "product_name": product["name"] if product else "Unknown",
                "category": product["category"] if product else "other",
                "current_amount": item["current_amount"],
                "par_level": item["par_level"],
                "order_quantity": item["order_quantity"],
                "urgency": item["urgency"]
            })
        
        return {
            "session": {
                "id": session_id,
                "location_id": location_id,
                "user_id": user_id,
                "started_at": session["started_at"],
                "completed_at": now,
                "status": "completed",
                "total_bottles": total_bottles,
                "duration_seconds": duration_seconds
            },
            "order": {
                "id": order_id,
                "items": order_items_response,
                "total_items": len(order_items_response),
                "variance_alerts": variance_alerts
            }
        }

@v1_router.post("/inventory/{session_id}/cancel", response_model=dict)
def cancel_inventory(session_id: str, user_id: str = Depends(get_current_user)):
    """Cancel an in-progress session"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify session belongs to user and is in progress
        cursor.execute(
            "SELECT id FROM inventory_sessions WHERE id = ? AND user_id = ? AND status = 'in_progress'",
            (session_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Active session not found"
            })
        
        now = now_iso()
        cursor.execute("""
            UPDATE inventory_sessions
            SET status = 'cancelled', completed_at = ?, updated_at = ?
            WHERE id = ?
        """, (now, now, session_id))
        conn.commit()
        
        return {
            "session": {
                "id": session_id,
                "status": "cancelled",
                "completed_at": now
            }
        }

# ============== ORDERS ==============

@v1_router.get("/orders", response_model=OrderListResponse)
def list_orders(
    location_id: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user)
):
    """List orders for user's locations"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Build query
        where_clause = "WHERE l.user_id = ?"
        params = [user_id]
        if location_id:
            where_clause += " AND o.location_id = ?"
            params.append(location_id)
        
        # Get total count
        cursor.execute(f"""
            SELECT COUNT(*) FROM orders o
            JOIN locations l ON o.location_id = l.id
            {where_clause}
        """, params)
        total = cursor.fetchone()[0]
        
        # Get orders
        cursor.execute(f"""
            SELECT o.*, l.name as location_name
            FROM orders o
            JOIN locations l ON o.location_id = l.id
            {where_clause}
            ORDER BY o.created_at DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset])
        
        import json
        orders = []
        for row in cursor.fetchall():
            order = dict(row)
            try:
                order_data = json.loads(order.get("order_data", "{}"))
                items = order_data.get("items", [])
            except:
                items = []
            
            orders.append({
                "id": order["id"],
                "session_id": order["session_id"],
                "location_id": order["location_id"],
                "location_name": order["location_name"],
                "items": items,
                "variance_alerts": [],
                "total_items": order["total_items"],
                "estimated_cost": order["estimated_cost"],
                "created_at": order["created_at"],
                "exported_at": order["exported_at"],
                "export_format": order["export_format"],
                "export_destination": order["export_destination"]
            })
        
        return {
            "orders": orders,
            "total": total
        }

@v1_router.get("/orders/{order_id}", response_model=dict)
def get_order(order_id: str, user_id: str = Depends(get_current_user)):
    """Get full order details"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT o.*, l.name as location_name, l.address, l.timezone
            FROM orders o
            JOIN locations l ON o.location_id = l.id
            WHERE o.id = ? AND l.user_id = ?
        """, (order_id, user_id))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Order not found"
            })
        
        import json
        order = dict(row)
        try:
            order_data = json.loads(order.get("order_data", "{}"))
            items = order_data.get("items", [])
        except:
            items = []
        
        try:
            variance_alerts = json.loads(order.get("variance_alerts", "[]"))
        except:
            variance_alerts = []
        
        return {
            "order": {
                "id": order["id"],
                "session_id": order["session_id"],
                "location": {
                    "id": order["location_id"],
                    "name": order["location_name"],
                    "address": order["address"],
                    "timezone": order["timezone"]
                },
                "items": items,
                "variance_alerts": variance_alerts,
                "total_items": order["total_items"],
                "estimated_cost": order["estimated_cost"],
                "created_at": order["created_at"],
                "exported_at": order["exported_at"]
            }
        }

@v1_router.post("/orders/{order_id}/export", response_model=OrderExportResponse)
def export_order(order_id: str, export_data: OrderExportRequest, user_id: str = Depends(get_current_user)):
    """Generate export and mark as exported"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT o.*, l.name as location_name
            FROM orders o
            JOIN locations l ON o.location_id = l.id
            WHERE o.id = ? AND l.user_id = ?
        """, (order_id, user_id))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Order not found"
            })
        
        import json
        order = dict(row)
        try:
            order_data = json.loads(order.get("order_data", "{}"))
            items = order_data.get("items", [])
        except:
            items = []
        
        # Generate text export
        location_name = order["location_name"]
        created_at = order["created_at"][:10]  # Just the date
        
        content_lines = [f"ORDER - {location_name}", created_at, "─" * 40, ""]
        
        # Group by urgency
        critical = [i for i in items if i.get("urgency") == "critical"]
        moderate = [i for i in items if i.get("urgency") == "moderate"]
        normal = [i for i in items if i.get("urgency") == "normal"]
        
        if critical:
            content_lines.append("🔴 CRITICAL (Out of Stock)")
            for item in critical:
                content_lines.append(f"  {item.get('product_name', 'Unknown')} ... {int(item.get('order_quantity', 0))} bottles")
            content_lines.append("")
        
        if moderate:
            content_lines.append("🟡 MODERATE (Below 50%)")
            for item in moderate:
                content_lines.append(f"  {item.get('product_name', 'Unknown')} ... {int(item.get('order_quantity', 0))} bottles")
            content_lines.append("")
        
        if normal:
            content_lines.append("🟢 NORMAL")
            for item in normal:
                content_lines.append(f"  {item.get('product_name', 'Unknown')} ... {int(item.get('order_quantity', 0))} bottles")
        
        content_lines.append("")
        content_lines.append(f"Total items: {len(items)}")
        
        content = "\n".join(content_lines)
        
        # Mark as exported
        now = now_iso()
        cursor.execute("""
            UPDATE orders
            SET exported_at = ?, export_format = ?, export_destination = ?
            WHERE id = ?
        """, (now, export_data.format, export_data.destination, order_id))
        conn.commit()
        
        return {
            "export": {
                "format": export_data.format,
                "content": content,
                "exported_at": now
            }
        }

# ============== SYNC ==============

@v1_router.post("/sync", response_model=SyncResponse)
def sync_data(sync_data: SyncRequest, user_id: str = Depends(get_current_user)):
    """Bulk sync endpoint for offline data"""
    with get_db() as conn:
        cursor = conn.cursor()
        import json
        
        now = now_iso()
        sessions_created = 0
        sessions_updated = 0
        scans_created = 0
        scans_duplicates = 0
        par_levels_updated = 0
        conflicts = []
        
        # Process sessions
        for session_data in sync_data.sessions:
            # Check if session exists
            cursor.execute("SELECT id, status FROM inventory_sessions WHERE id = ?", (session_data.id,))
            existing = cursor.fetchone()
            
            if existing:
                # Update if needed
                if existing["status"] == "in_progress" and session_data.status == "completed":
                    cursor.execute("""
                        UPDATE inventory_sessions
                        SET status = ?, completed_at = ?, updated_at = ?
                        WHERE id = ?
                    """, (session_data.status, session_data.completed_at, now, session_data.id))
                    sessions_updated += 1
            else:
                # Verify location belongs to user
                cursor.execute(
                    "SELECT id FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                    (session_data.location_id, user_id)
                )
                if not cursor.fetchone():
                    conflicts.append({
                        "type": "session",
                        "id": session_data.id,
                        "reason": "Location not found or access denied"
                    })
                    continue
                
                # Create session
                cursor.execute("""
                    INSERT INTO inventory_sessions (id, location_id, user_id, started_at, completed_at, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (session_data.id, session_data.location_id, user_id, session_data.started_at,
                      session_data.completed_at, session_data.status, now, now))
                sessions_created += 1
            
            # Process scans
            for scan_data in session_data.scans:
                # Check idempotency
                if scan_data.idempotency_key:
                    cursor.execute(
                        "SELECT id FROM scans WHERE idempotency_key = ?",
                        (scan_data.idempotency_key,)
                    )
                    if cursor.fetchone():
                        scans_duplicates += 1
                        continue
                
                scan_id = generate_id()
                level_decimal = level_to_decimal(scan_data.level)
                
                cursor.execute("""
                    INSERT INTO scans (id, session_id, product_id, level, level_decimal, quantity,
                                      detection_method, idempotency_key, synced_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """, (scan_id, session_data.id, scan_data.product_id, scan_data.level, level_decimal,
                      scan_data.detection_method, scan_data.idempotency_key, now,
                      scan_data.created_at, now))
                scans_created += 1
            
            # Process voice notes
            for vn_data in session_data.voice_notes:
                note_id = generate_id()
                cursor.execute("""
                    INSERT INTO voice_notes (id, session_id, audio_url, transcript, linked_product_id, duration_seconds, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (note_id, session_data.id, vn_data.audio_url, vn_data.transcript,
                      vn_data.linked_product_id, vn_data.duration_seconds, now))
        
        # Process par level updates
        for pl_update in sync_data.par_level_updates:
            # Verify location belongs to user
            cursor.execute(
                "SELECT id FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                (pl_update.location_id, user_id)
            )
            if not cursor.fetchone():
                continue
            
            pl_id = generate_id()
            cursor.execute("""
                INSERT INTO par_levels (id, location_id, product_id, par_quantity, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(location_id, product_id) DO UPDATE SET
                    par_quantity = excluded.par_quantity,
                    updated_at = excluded.updated_at
            """, (pl_id, pl_update.location_id, pl_update.product_id, pl_update.par_quantity, now))
            par_levels_updated += 1
        
        conn.commit()
        
        # Get server updates since last_sync_at
        server_updates = {
            "products": [],
            "locations": []
        }
        
        if sync_data.last_sync_at:
            # Get new products
            cursor.execute("""
                SELECT * FROM products
                WHERE created_at > ?
                ORDER BY created_at DESC
                LIMIT 50
            """, (sync_data.last_sync_at.isoformat(),))
            server_updates["products"] = [dict(row) for row in cursor.fetchall()]
            
            # Get user's locations
            cursor.execute("""
                SELECT * FROM locations
                WHERE user_id = ? AND (created_at > ? OR updated_at > ?)
                AND deleted_at IS NULL
            """, (user_id, sync_data.last_sync_at.isoformat(), sync_data.last_sync_at.isoformat()))
            server_updates["locations"] = [dict(row) for row in cursor.fetchall()]
        
        return {
            "synced_at": now,
            "sessions": {
                "created": sessions_created,
                "updated": sessions_updated,
                "scans_created": scans_created,
                "scans_duplicates": scans_duplicates
            },
            "par_levels": {
                "updated": par_levels_updated
            },
            "conflicts": conflicts,
            "server_updates": server_updates
        }

@v1_router.get("/sync/{location_id}", response_model=SyncLocationResponse)
def get_location_sync_data(location_id: str, since: Optional[str] = None, user_id: str = Depends(get_current_user)):
    """Get latest data for a location (delta sync)"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify location belongs to user
        cursor.execute(
            "SELECT * FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (location_id, user_id)
        )
        location = cursor.fetchone()
        if not location:
            raise HTTPException(status_code=403, detail={
                "error": "forbidden",
                "message": "Access denied to this location"
            })
        
        now = now_iso()
        
        # Get par levels
        cursor.execute("""
            SELECT pl.*, p.id as product_id, p.name, p.brand, p.category, p.size, p.upc,
                   p.image_url, p.scan_count, p.verified, p.created_at as product_created_at,
                   p.updated_at as product_updated_at
            FROM par_levels pl
            JOIN products p ON pl.product_id = p.id
            WHERE pl.location_id = ?
        """, (location_id,))
        
        par_levels = []
        for row in cursor.fetchall():
            pl = {
                "id": row["id"],
                "location_id": row["location_id"],
                "product_id": row["product_id"],
                "par_quantity": row["par_quantity"],
                "updated_at": row["updated_at"],
                "product": {
                    "id": row["product_id"],
                    "name": row["name"],
                    "brand": row["brand"],
                    "category": row["category"],
                    "size": row["size"],
                    "upc": row["upc"],
                    "image_url": row["image_url"],
                    "scan_count": row["scan_count"],
                    "verified": bool(row["verified"]),
                    "created_at": row["product_created_at"],
                    "updated_at": row["product_updated_at"]
                }
            }
            par_levels.append(pl)
        
        # Get recent sessions
        cursor.execute("""
            SELECT * FROM inventory_sessions
            WHERE location_id = ?
            ORDER BY started_at DESC
            LIMIT 5
        """, (location_id,))
        recent_sessions = [dict(row) for row in cursor.fetchall()]
        
        # Get products used at this location
        cursor.execute("""
            SELECT DISTINCT p.* FROM products p
            JOIN scans s ON p.id = s.product_id
            JOIN inventory_sessions ses ON s.session_id = ses.id
            WHERE ses.location_id = ?
            ORDER BY p.name
        """, (location_id,))
        products = [dict(row) for row in cursor.fetchall()]
        for p in products:
            p["verified"] = bool(p["verified"])
        
        return {
            "location": dict(location),
            "par_levels": par_levels,
            "recent_sessions": recent_sessions,
            "products": products,
            "synced_at": now
        }


# ============== V1 DISTRIBUTORS ==============

@v1_router.get("/distributors", response_model=DistributorListResponse)
def list_distributors(user_id: str = Depends(get_current_user)):
    """List user's distributors"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM distributors 
            WHERE user_id = ? AND deleted_at IS NULL
            ORDER BY name ASC
        """, (user_id,))
        distributors = [dict(row) for row in cursor.fetchall()]
        return {"distributors": distributors}

@v1_router.post("/distributors", response_model=dict, status_code=201)
def create_distributor(distributor_data: DistributorCreate, user_id: str = Depends(get_current_user)):
    """Create new distributor"""
    with get_db() as conn:
        cursor = conn.cursor()
        distributor_id = generate_id()
        now = now_iso()
        cursor.execute("""
            INSERT INTO distributors (id, user_id, name, email, phone, rep_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (distributor_id, user_id, distributor_data.name, distributor_data.email,
              distributor_data.phone, distributor_data.rep_name, now, now))
        conn.commit()
        return {"distributor": {"id": distributor_id, "user_id": user_id, "name": distributor_data.name,
                                "email": distributor_data.email, "phone": distributor_data.phone,
                                "rep_name": distributor_data.rep_name, "created_at": now, "updated_at": now}}

@v1_router.put("/distributors/{distributor_id}", response_model=dict)
def update_distributor(distributor_id: str, distributor_data: DistributorUpdate, user_id: str = Depends(get_current_user)):
    """Update distributor"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM distributors WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                       (distributor_id, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Distributor not found"})
        now = now_iso()
        updates = []
        params = []
        if distributor_data.name is not None:
            updates.append("name = ?")
            params.append(distributor_data.name)
        if distributor_data.email is not None:
            updates.append("email = ?")
            params.append(distributor_data.email)
        if distributor_data.phone is not None:
            updates.append("phone = ?")
            params.append(distributor_data.phone)
        if distributor_data.rep_name is not None:
            updates.append("rep_name = ?")
            params.append(distributor_data.rep_name)
        updates.append("updated_at = ?")
        params.append(now)
        params.append(distributor_id)
        cursor.execute(f"UPDATE distributors SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        return {"success": True, "message": "Distributor updated"}

@v1_router.delete("/distributors/{distributor_id}")
def delete_distributor(distributor_id: str, user_id: str = Depends(get_current_user)):
    """Soft delete distributor"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM distributors WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                       (distributor_id, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Distributor not found"})
        cursor.execute("UPDATE distributors SET deleted_at = ?, updated_at = ? WHERE id = ?",
                       (now_iso(), now_iso(), distributor_id))
        conn.commit()
        return {"success": True, "message": "Distributor deleted"}

@v1_router.post("/locations/{location_id}/product-distributors", response_model=dict)
def assign_product_distributor(location_id: str, assignment: LocationProductDistributorCreate,
                                user_id: str = Depends(get_current_user)):
    """Assign a product to a distributor for a location"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                       (location_id, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Access denied"})
        assignment_id = generate_id()
        now = now_iso()
        cursor.execute("""
            INSERT INTO location_product_distributors (id, location_id, product_id, distributor_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(location_id, product_id) DO UPDATE SET
                distributor_id = excluded.distributor_id
        """, (assignment_id, location_id, assignment.product_id, assignment.distributor_id, now))
        conn.commit()
        return {"success": True, "assignment_id": assignment_id}

@v1_router.get("/locations/{location_id}/product-distributors", response_model=LocationProductDistributorListResponse)
def list_product_distributors(location_id: str, user_id: str = Depends(get_current_user)):
    """List product-distributor assignments for a location"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                       (location_id, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Access denied"})
        cursor.execute("""
            SELECT lpd.*, d.name as distributor_name, d.email as distributor_email,
                   p.name as product_name, p.brand as product_brand, p.size as product_size
            FROM location_product_distributors lpd
            JOIN distributors d ON lpd.distributor_id = d.id
            JOIN products p ON lpd.product_id = p.id
            WHERE lpd.location_id = ? AND d.deleted_at IS NULL
        """, (location_id,))
        assignments = []
        for row in cursor.fetchall():
            assignments.append({
                "id": row["id"],
                "location_id": row["location_id"],
                "product_id": row["product_id"],
                "distributor_id": row["distributor_id"],
                "distributor": {"id": row["distributor_id"], "name": row["distributor_name"], "email": row["distributor_email"]},
                "product": {"id": row["product_id"], "name": row["product_name"], "brand": row["product_brand"], "size": row["product_size"]},
                "created_at": row["created_at"]
            })
        return {"assignments": assignments}

# ============== V1 EMAIL PREPARATION ==============

@v1_router.post("/orders/{order_id}/prepare-emails", response_model=OrderPrepareEmailsResponse)
def prepare_order_emails(order_id: str, user_id: str = Depends(get_current_user)):
    """Prepare emails grouped by distributor for an order"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT o.*, l.name as location_name
            FROM orders o
            JOIN locations l ON o.location_id = l.id
            WHERE o.id = ? AND l.user_id = ?
        """, (order_id, user_id))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Order not found"})
        
        order_data = json.loads(row.get("order_data", "{}"))
        items = order_data.get("items", [])
        location_id = row["location_id"]
        location_name = row["location_name"]
        
        # Group items by distributor
        distributor_items = {}
        for item in items:
            product_id = item.get("product_id")
            cursor.execute("""
                SELECT d.id, d.name, d.email
                FROM location_product_distributors lpd
                JOIN distributors d ON lpd.distributor_id = d.id
                WHERE lpd.location_id = ? AND lpd.product_id = ? AND d.deleted_at IS NULL
            """, (location_id, product_id))
            dist_row = cursor.fetchone()
            if dist_row:
                dist_id = dist_row["id"]
                if dist_id not in distributor_items:
                    distributor_items[dist_id] = {
                        "distributor_id": dist_id,
                        "distributor_name": dist_row["name"],
                        "email": dist_row["email"] or "orders@example.com",
                        "items": []
                    }
                distributor_items[dist_id]["items"].append(item)
        
        # If no distributors assigned, put all items in a default group
        if not distributor_items:
            distributor_items["default"] = {
                "distributor_id": "default",
                "distributor_name": "Default Distributor",
                "email": "orders@example.com",
                "items": items
            }
        
        # Generate emails
        today = datetime.now(timezone.utc).strftime("%B %d, %Y")
        emails = []
        total_items = 0
        
        for dist_data in distributor_items.values():
            dist_items = dist_data["items"]
            items_text = []
            email_items = []
            
            for item in dist_items:
                qty = int(item.get("order_quantity", 0))
                name = item.get("product_name", "Unknown")
                size = item.get("size", "")
                items_text.append(f"- {name} {size} x {qty}")
                email_items.append({
                    "product_id": item.get("product_id"),
                    "product_name": name,
                    "quantity": qty,
                    "size": size
                })
                total_items += qty
            
            body_text = f"""Hi,

Please deliver:

{chr(10).join(items_text)}

Total: {sum(int(i.get('order_quantity', 0)) for i in dist_items)} bottles

Thank you,
{location_name}"""
            
            emails.append({
                "distributor_id": dist_data["distributor_id"],
                "distributor_name": dist_data["distributor_name"],
                "to": dist_data["email"],
                "subject": f"Order from {location_name} - {today}",
                "body_text": body_text,
                "items": email_items,
                "total_items": sum(int(i.get('order_quantity', 0)) for i in dist_items)
            })
        
        return {
            "emails": emails,
            "summary": {
                "total_distributors": len(emails),
                "total_items": total_items
            }
        }

# ============== V1 USERS ==============

@v1_router.get("/users/me", response_model=UserProfileResponse)
def get_user_profile(user_id: str = Depends(get_current_user)):
    """Get full user profile including subscription status"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, email, name, subscription_status, subscription_tier, trial_ends_at,
                   terms_accepted_at, privacy_accepted_at, created_at
            FROM users WHERE id = ? AND deleted_at IS NULL
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        return dict(row)

@v1_router.delete("/users/me")
def delete_user(password: str, user_id: str = Depends(get_current_user)):
    """Soft delete user account (GDPR compliance)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        if not verify_password(password, row["password_hash"]):
            raise HTTPException(status_code=401, detail={"error": "invalid_password", "message": "Password is incorrect"})
        cursor.execute("""
            UPDATE users SET deleted_at = ?, email = CONCAT(email, '.deleted.', ?), updated_at = ?
            WHERE id = ?
        """, (now_iso(), generate_id()[:8], now_iso(), user_id))
        conn.commit()
        return {"success": True, "message": "Account deleted successfully"}

@v1_router.post("/users/me/accept-terms")
def accept_terms(request: AcceptTermsRequest, user_id: str = Depends(get_current_user)):
    """Accept terms and privacy policy"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = now_iso()
        cursor.execute("""
            UPDATE users SET terms_accepted_at = ?, privacy_accepted_at = ?, updated_at = ?
            WHERE id = ?
        """, (now, now, now, user_id))
        conn.commit()
        return {"success": True, "message": "Terms accepted successfully"}

# ============== V1 ADDITIONAL AUTH ==============

@v1_router.post("/auth/forgot-password")
def forgot_password(request: ForgotPasswordRequest):
    """Request password reset"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ? AND deleted_at IS NULL",
                       (request.email.lower().strip(),))
        row = cursor.fetchone()
        if row:
            reset_token = create_password_reset_token(row["id"])
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            cursor.execute("""
                UPDATE users SET password_reset_token = ?, password_reset_expires_at = ?
                WHERE id = ?
            """, (reset_token, expires_at, row["id"]))
            conn.commit()
            return {"success": True, "message": "If an account exists, a reset link has been sent",
                    "debug_token": reset_token}
        return {"success": True, "message": "If an account exists, a reset link has been sent"}

@v1_router.post("/auth/reset-password")
def reset_password(request: ResetPasswordRequest):
    """Reset password using token"""
    user_id = verify_password_reset_token(request.token)
    if not user_id:
        raise HTTPException(status_code=400, detail={"error": "invalid_token", "message": "Invalid or expired reset token"})
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM users WHERE id = ? AND password_reset_token = ? AND password_reset_expires_at > ?
        """, (user_id, request.token, now_iso()))
        if not cursor.fetchone():
            raise HTTPException(status_code=400, detail={"error": "invalid_token", "message": "Invalid or expired reset token"})
        password_hash = get_password_hash(request.new_password)
        cursor.execute("""
            UPDATE users SET password_hash = ?, password_reset_token = NULL, password_reset_expires_at = NULL, updated_at = ?
            WHERE id = ?
        """, (password_hash, now_iso(), user_id))
        conn.commit()
        return {"success": True, "message": "Password reset successfully"}

@v1_router.put("/auth/change-password")
def change_password(request: ChangePasswordRequest, user_id: str = Depends(get_current_user)):
    """Change password (requires current password)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        if not verify_password(request.current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail={"error": "invalid_password", "message": "Current password is incorrect"})
        password_hash = get_password_hash(request.new_password)
        cursor.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                       (password_hash, now_iso(), user_id))
        conn.commit()
        return {"success": True, "message": "Password changed successfully"}

# ============== INCLUDE V1 ROUTER ==============

app.include_router(v1_router)

# ============== ERROR HANDLERS ==============

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions with consistent format"""
    if isinstance(exc.detail, dict):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": "error",
            "message": exc.detail
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions"""
    return JSONResponse(
        status_code=500,
        content={
            "error": "server_error",
            "message": "An unexpected error occurred"
        }
    )
