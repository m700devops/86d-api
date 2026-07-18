from fastapi import FastAPI, Depends, HTTPException, Header, Request, Query, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import asyncio
import time
import uuid
import json
import traceback

from database import init_db, get_db
from auth import (
    get_password_hash, verify_password, create_access_token,
    create_refresh_token, verify_token,
    PASSWORD_RESET_EXPIRE_MINUTES, PASSWORD_RESET_RESEND_COOLDOWN_SECONDS
)
from helpers import (
    generate_id, now_iso, level_to_decimal, decimal_to_level,
    classify_level, smooth_level, calculate_variance, generate_order_items
)
from models import *
from seed_data import SEED_PRODUCTS
import google.generativeai as genai
import openai
import os
import httpx
import random
from pydantic import BaseModel, Field

# Startup time for uptime calculation
START_TIME = time.time()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup - non-blocking"""
    # auth.py falls back to a hardcoded SECRET_KEY when the env var is unset —
    # with the default, anyone can forge login tokens for any account. Scream
    # about it at startup so it can't go unnoticed on a real deployment.
    from auth import SECRET_KEY as _sk
    if _sk == "your-secret-key-change-in-production":
        print("=" * 70, flush=True)
        print("[SECURITY] SECRET_KEY env var is NOT set — using the default!", flush=True)
        print("[SECURITY] Anyone can forge auth tokens. Set SECRET_KEY on Render NOW.", flush=True)
        print("=" * 70, flush=True)
    try:
        # Run init_db in thread pool to avoid blocking startup
        await asyncio.to_thread(init_db)
        print("[lifespan] Database initialized successfully", flush=True)
    except Exception as e:
        print(f"[lifespan] Database init warning (may already exist): {e}", flush=True)
    # Pre-warm AI provider connections so the first scan is fast (best-effort)
    asyncio.create_task(_warm_providers())
    # Periodic trial-ending reminder emails (best-effort, runs for the life of the process)
    asyncio.create_task(_trial_reminder_loop())
    yield

app = FastAPI(
    title="86'd API",
    description="Bar inventory management API for iOS app",
    version="1.0.0",
    lifespan=lifespan
)

# ============== HEALTH CHECK (must be BEFORE router includes) ==============
# Render health check - must respond quickly on root path
@app.get("/")
async def health_check():
    """Health check for Render and load balancers"""
    return {
        "status": "ok",
        "service": "86d-api",
        "uptime": time.time() - START_TIME
    }

@app.get("/health")
async def health_check_alt():
    """Alternative health check endpoint"""
    return {
        "status": "ok", 
        "service": "86d-api",
        "uptime": time.time() - START_TIME
    }

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
                "SELECT id FROM users WHERE email = %s AND deleted_at IS NULL",
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
            trial_ends = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            
            cursor.execute("""
                INSERT INTO users (id, email, password_hash, name, terms_accepted_at, privacy_accepted_at,
                                   trial_started_at, trial_ends_at, subscription_status, subscription_tier,
                                   created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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

# Login throttling — in-memory, per-email. Enough to make credential
# stuffing impractical on a single-instance deployment without adding a
# dependency; resets on process restart, which is fine for this purpose.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60
_login_failures: dict[str, list[float]] = {}


def _login_locked(email: str) -> bool:
    cutoff = time.time() - LOGIN_LOCKOUT_SECONDS
    attempts = [t for t in _login_failures.get(email, []) if t > cutoff]
    _login_failures[email] = attempts
    return len(attempts) >= LOGIN_MAX_FAILURES


def _record_login_failure(email: str) -> None:
    _login_failures.setdefault(email, []).append(time.time())


@v1_router.post("/auth/login", response_model=TokenResponse)
def login(credentials: UserLogin):
    """Authenticate and get tokens"""
    email = credentials.email.lower().strip()
    if _login_locked(email):
        raise HTTPException(status_code=429, detail={
            "error": "too_many_attempts",
            "message": "Too many failed attempts — try again in 15 minutes, or reset your password."
        })

    with get_db() as conn:
        cursor = conn.cursor()

        # Find user
        cursor.execute(
            """SELECT id, email, password_hash, name, subscription_status, subscription_tier,
                      trial_ends_at, terms_accepted_at, privacy_accepted_at, created_at
               FROM users WHERE email = %s AND deleted_at IS NULL""",
            (email,)
        )
        row = cursor.fetchone()

        if not row or not verify_password(credentials.password, row["password_hash"]):
            _record_login_failure(email)
            raise HTTPException(status_code=401, detail={
                "error": "invalid_credentials",
                "message": "Invalid email or password"
            })

        _login_failures.pop(email, None)

        # Generate tokens
        access_token = create_access_token(row["id"])
        refresh_token = create_refresh_token(row["id"])
        
        return {
            "user": {
                "id": row["id"],
                "email": row["email"],
                "name": row["name"],
                "subscription_status": row["subscription_status"] or "trial",
                "subscription_tier": row["subscription_tier"] or "starter",
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
    category: Optional[str] = Query(None, pattern="^(spirits|beer|wine|soda|mixer|water|juice|other)$"),
    sort: str = Query("name", pattern="^(name|scan_count|created_at)$")
):
    """List products with pagination and filters"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Build query
        where_clause = "WHERE 1=1"
        params = []
        if category:
            where_clause += " AND category = %s"
            params.append(category)
        
        # Get total count
        cursor.execute(f"SELECT COUNT(*) as count FROM products {where_clause}", params)
        total = cursor.fetchone()["count"]
        
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
            LIMIT %s OFFSET %s
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
            WHERE name ILIKE %s OR brand ILIKE %s OR upc ILIKE %s
            ORDER BY
                CASE WHEN name ILIKE %s THEN 0 ELSE 1 END,
                scan_count DESC
            LIMIT %s
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
        
        cursor.execute("SELECT * FROM products WHERE upc = %s", (upc,))
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
            cursor.execute("SELECT * FROM products WHERE upc = %s", (product_data.upc,))
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            SET scan_count = scan_count + 1, updated_at = %s
            WHERE id = %s
        """, (now_iso(), product_id))
        conn.commit()
        
        cursor.execute("SELECT scan_count FROM products WHERE id = %s", (product_id,))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Product not found"
            })
        
        return {"scan_count": row["scan_count"]}

# ============== LOCATIONS ==============

def _location_row(row) -> dict:
    """DB row → response dict: staff_names is stored as a JSON TEXT column."""
    loc = dict(row)
    try:
        loc["staff_names"] = json.loads(loc.get("staff_names") or "[]")
    except Exception:
        loc["staff_names"] = []
    loc["order_rounding_mode"] = loc.get("order_rounding_mode") or "nearest"
    return loc

@v1_router.get("/locations", response_model=LocationListResponse)
def list_locations(user_id: str = Depends(get_current_user)):
    """List user's locations"""
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM locations
            WHERE user_id = %s AND deleted_at IS NULL
            ORDER BY created_at DESC
        """, (user_id,))

        locations = [_location_row(row) for row in cursor.fetchall()]
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
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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
                "order_rounding_mode": "nearest",
                "staff_names": [],
                "created_at": now,
                "updated_at": now
            }
        }

@v1_router.patch("/locations/{location_id}", response_model=LocationResponse)
def update_location(location_id: str, updates: LocationUpdate, user_id: str = Depends(get_current_user)):
    """Update a location's settings — order_rounding_mode and/or staff_names."""
    fields = updates.model_dump(exclude_unset=True, exclude_none=True)
    if not fields:
        raise HTTPException(status_code=400, detail={"error": "no_fields", "message": "Nothing to update"})
    if "staff_names" in fields:
        fields["staff_names"] = json.dumps(fields["staff_names"])

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
            (location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Location not found"})

        set_clause = ", ".join(f"{k} = %s" for k in fields)
        now = now_iso()
        cursor.execute(
            f"UPDATE locations SET {set_clause}, updated_at = %s WHERE id = %s",
            (*fields.values(), now, location_id)
        )
        conn.commit()

        cursor.execute("SELECT * FROM locations WHERE id = %s", (location_id,))
        return _location_row(cursor.fetchone())

@v1_router.get("/locations/{location_id}/par-levels", response_model=ParLevelListResponse)
def get_par_levels(location_id: str, user_id: str = Depends(get_current_user)):
    """Get all par levels for a location"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify location belongs to user
        cursor.execute(
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
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
            WHERE pl.location_id = %s
        """, (location_id,))
        
        rows = cursor.fetchall()
        par_levels = []
        for row in rows:
            pl = {
                "id": row["id"],
                "location_id": row["location_id"],
                "product_id": row["product_id"],
                "par_quantity": row["par_quantity"],
                "full_quantity": float(row["full_quantity"] or 0),
                "current_stock": float(row["current_stock"] or 0),
                "price": float(row["price"]) if row["price"] else None,
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
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
            (location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={
                "error": "forbidden",
                "message": "Access denied to this location"
            })
        
        # Verify product exists
        cursor.execute("SELECT id FROM products WHERE id = %s", (par_data.product_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Product not found"
            })
        
        now = now_iso()
        par_id = generate_id()
        
        cursor.execute("""
            INSERT INTO par_levels (id, location_id, product_id, par_quantity, full_quantity, current_stock, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(location_id, product_id) DO UPDATE SET
                par_quantity = excluded.par_quantity,
                full_quantity = COALESCE(excluded.full_quantity, par_levels.full_quantity, 0),
                current_stock = COALESCE(excluded.current_stock, par_levels.current_stock, 0),
                updated_at = excluded.updated_at
        """, (par_id, location_id, par_data.product_id, par_data.par_quantity,
              par_data.full_quantity or 0, par_data.current_stock or 0, now))
        conn.commit()

        # Get the actual ID (either inserted or existing)
        cursor.execute(
            "SELECT id FROM par_levels WHERE location_id = %s AND product_id = %s",
            (location_id, par_data.product_id)
        )
        par_id = cursor.fetchone()["id"]

        return {
            "par_level": {
                "id": par_id,
                "location_id": location_id,
                "product_id": par_data.product_id,
                "par_quantity": par_data.par_quantity,
                "full_quantity": par_data.full_quantity or 0,
                "current_stock": par_data.current_stock or 0,
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
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
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
                INSERT INTO par_levels (id, location_id, product_id, par_quantity, full_quantity, current_stock, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(location_id, product_id) DO UPDATE SET
                    par_quantity = excluded.par_quantity,
                    full_quantity = COALESCE(excluded.full_quantity, par_levels.full_quantity, 0),
                    current_stock = COALESCE(excluded.current_stock, par_levels.current_stock, 0),
                    updated_at = excluded.updated_at
            """, (par_id, location_id, par_data.product_id, par_data.par_quantity,
                  par_data.full_quantity or 0, par_data.current_stock or 0, now))
            updated += 1

            cursor.execute(
                "SELECT id FROM par_levels WHERE location_id = %s AND product_id = %s",
                (location_id, par_data.product_id)
            )
            actual_id = cursor.fetchone()["id"]

            par_levels.append({
                "id": actual_id,
                "location_id": location_id,
                "product_id": par_data.product_id,
                "par_quantity": par_data.par_quantity,
                "full_quantity": par_data.full_quantity or 0,
                "current_stock": par_data.current_stock or 0,
                "updated_at": now
            })
        
        conn.commit()

        return {
            "updated": updated,
            "par_levels": par_levels
        }

@v1_router.get("/locations/{location_id}/products/{product_id}", response_model=ProductStockResponse)
def get_product_stock(location_id: str, product_id: str, user_id: str = Depends(get_current_user)):
    """Get full/current_stock/par for a specific product at a location."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
            (location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Access denied to this location"})

        cursor.execute(
            "SELECT par_quantity, full_quantity, current_stock, updated_at FROM par_levels WHERE location_id = %s AND product_id = %s",
            (location_id, product_id)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "No stock record for this product at this location"})

        return {
            "location_id": location_id,
            "product_id": product_id,
            "full": float(row["full_quantity"] or 0),
            "current_stock": float(row["current_stock"] or 0),
            "par": float(row["par_quantity"]) if row["par_quantity"] is not None else None,
            "updated_at": row["updated_at"],
        }

@v1_router.patch("/locations/{location_id}/products/{product_id}", response_model=ProductStockResponse)
def update_product_stock(location_id: str, product_id: str, data: ProductStockUpdate, user_id: str = Depends(get_current_user)):
    """Update full/current_stock/par for a specific product at a location (upserts par_levels row)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
            (location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Access denied to this location"})

        cursor.execute("SELECT id FROM products WHERE id = %s", (product_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Product not found"})

        now = now_iso()
        # Fetch existing row so we can preserve unchanged fields
        cursor.execute(
            "SELECT par_quantity, full_quantity, current_stock, price FROM par_levels WHERE location_id = %s AND product_id = %s",
            (location_id, product_id)
        )
        existing = cursor.fetchone()

        new_par = data.par if data.par is not None else (float(existing["par_quantity"]) if existing else 1.0)
        new_full = data.full if data.full is not None else (float(existing["full_quantity"] or 0) if existing else 0.0)
        new_stock = data.current_stock if data.current_stock is not None else (float(existing["current_stock"] or 0) if existing else 0.0)
        new_price = data.price if data.price is not None else (float(existing["price"] or 0) if existing else 0.0)

        par_id = generate_id()
        cursor.execute("""
            INSERT INTO par_levels (id, location_id, product_id, par_quantity, full_quantity, current_stock, price, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(location_id, product_id) DO UPDATE SET
                par_quantity = excluded.par_quantity,
                full_quantity = excluded.full_quantity,
                current_stock = excluded.current_stock,
                price = excluded.price,
                updated_at = excluded.updated_at
        """, (par_id, location_id, product_id, new_par, new_full, new_stock, new_price, now))
        conn.commit()

        return {
            "location_id": location_id,
            "product_id": product_id,
            "full": new_full,
            "current_stock": new_stock,
            "par": new_par,
            "price": new_price if new_price > 0 else None,
            "updated_at": now,
        }

# ============== INVENTORY SESSIONS ==============

@v1_router.post("/inventory/start", response_model=dict, status_code=201)
def start_inventory(session_data: InventorySessionCreate, user_id: str = Depends(get_current_user)):
    """Start new inventory session"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify location belongs to user
        cursor.execute(
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
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
            WHERE location_id = %s AND status = 'in_progress'
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
            VALUES (%s, %s, %s, %s, 'in_progress', %s, %s, %s, %s)
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
            WHERE s.id = %s AND s.user_id = %s
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
            WHERE sc.session_id = %s
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
        cursor.execute("SELECT * FROM voice_notes WHERE session_id = %s ORDER BY created_at DESC", (session_id,))
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
            "SELECT id, location_id FROM inventory_sessions WHERE id = %s AND user_id = %s AND status = 'in_progress'",
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
                "SELECT * FROM scans WHERE idempotency_key = %s",
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            scan_id, session_id, scan_data.product_id, scan_data.level, level_decimal,
            scan_data.quantity, scan_data.detection_method, scan_data.confidence,
            scan_data.photo_url, scan_data.shelf_location, scan_data.notes,
            scan_data.idempotency_key, now, created_at, now
        ))
        
        # Increment product scan count
        cursor.execute(
            "UPDATE products SET scan_count = scan_count + 1, updated_at = %s WHERE id = %s",
            (now, scan_data.product_id)
        )
        
        conn.commit()
        
        # Get total scans for session
        cursor.execute("SELECT COUNT(*) as count FROM scans WHERE session_id = %s", (session_id,))
        total = cursor.fetchone()["count"]
        
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
            "SELECT id FROM inventory_sessions WHERE id = %s AND user_id = %s AND status = 'in_progress'",
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
                    "SELECT * FROM scans WHERE idempotency_key = %s",
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                scan_id, session_id, scan_data.product_id, scan_data.level, level_decimal,
                scan_data.quantity, scan_data.detection_method, scan_data.confidence,
                scan_data.photo_url, scan_data.shelf_location, scan_data.notes,
                scan_data.idempotency_key, now, created_at, now
            ))
            
            # Increment product scan count
            cursor.execute(
                "UPDATE products SET scan_count = scan_count + 1, updated_at = %s WHERE id = %s",
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

@v1_router.post("/inventory/{session_id}/voice", response_model=dict, status_code=201)
def add_voice_note(session_id: str, voice_data: VoiceNoteCreate, user_id: str = Depends(get_current_user)):
    """Add voice note to session"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify session belongs to user
        cursor.execute(
            "SELECT id FROM inventory_sessions WHERE id = %s AND user_id = %s",
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
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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
            WHERE s.id = %s AND s.user_id = %s AND s.status = 'in_progress'
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
            WHERE s.session_id = %s
        """, (session_id,))
        scans = [dict(row) for row in cursor.fetchall()]
        
        # Get par levels for location
        cursor.execute("SELECT product_id, par_quantity FROM par_levels WHERE location_id = %s", (location_id,))
        par_levels = {row["product_id"]: row["par_quantity"] for row in cursor.fetchall()}
        
        # Generate order items
        order_items = generate_order_items(scans, par_levels)
        
        # Get usage history for variance alerts
        variance_alerts = []
        for scan in scans:
            product_id = scan["product_id"]
            cursor.execute("""
                SELECT bottles_used FROM usage_history
                WHERE location_id = %s AND product_id = %s
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
            SET status = 'completed', completed_at = %s, total_bottles = %s, duration_seconds = %s, updated_at = %s
            WHERE id = %s
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
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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
            cursor.execute("SELECT name, category FROM products WHERE id = %s", (item["product_id"],))
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
            "SELECT id FROM inventory_sessions WHERE id = %s AND user_id = %s AND status = 'in_progress'",
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
            SET status = 'cancelled', completed_at = %s, updated_at = %s
            WHERE id = %s
        """, (now, now, session_id))
        conn.commit()
        
        return {
            "session": {
                "id": session_id,
                "status": "cancelled",
                "completed_at": now
            }
        }

# ============== INVENTORY DRAFTS ==============
# Server-side backup of the mobile app's in-progress (unsent) scan session,
# on top of its local AsyncStorage copy — protects against a lost/reinstalled
# device, not just an app-kill mid-shift. One draft per user+location.

@v1_router.put("/inventory/draft", response_model=dict)
def save_inventory_draft(request: InventoryDraftRequest, user_id: str = Depends(get_current_user)):
    """Upsert the current draft for a location. An empty bottles list deletes it."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
            (request.location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Location not found"})

        if not request.bottles:
            cursor.execute(
                "DELETE FROM inventory_drafts WHERE user_id = %s AND location_id = %s",
                (user_id, request.location_id)
            )
            conn.commit()
            return {"success": True}

        now = now_iso()
        cursor.execute("""
            INSERT INTO inventory_drafts (user_id, location_id, bottles_data, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, location_id) DO UPDATE
            SET bottles_data = EXCLUDED.bottles_data, updated_at = EXCLUDED.updated_at
        """, (user_id, request.location_id, json.dumps(request.bottles), now))
        conn.commit()
        return {"success": True}

@v1_router.get("/inventory/draft", response_model=InventoryDraftResponse)
def get_inventory_draft(location_id: str, user_id: str = Depends(get_current_user)):
    """Fetch the saved draft for a location, if any."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.bottles_data, d.updated_at FROM inventory_drafts d
            JOIN locations l ON d.location_id = l.id
            WHERE d.user_id = %s AND d.location_id = %s AND l.user_id = %s
        """, (user_id, location_id, user_id))
        row = cursor.fetchone()
        if not row:
            return {"bottles": None, "updated_at": None}
        try:
            bottles = json.loads(row["bottles_data"])
        except Exception:
            bottles = None
        return {"bottles": bottles, "updated_at": row["updated_at"]}

@v1_router.delete("/inventory/draft", response_model=dict)
def delete_inventory_draft(location_id: str, user_id: str = Depends(get_current_user)):
    """Explicitly clear a draft, e.g. once its order has been sent."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM inventory_drafts WHERE user_id = %s AND location_id = %s",
            (user_id, location_id)
        )
        conn.commit()
        return {"success": True}

# ============== ORDERS ==============

@v1_router.get("/orders", response_model=OrderListResponse)
def list_orders(
    location_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(get_current_user)
):
    """List orders for user's locations, optionally filtered by date range and a
    free-text search over distributor/item names (matched against the stored
    order_data blob)."""
    with get_db() as conn:
        cursor = conn.cursor()

        # Build query
        where_clause = "WHERE l.user_id = %s"
        params = [user_id]
        if location_id:
            where_clause += " AND o.location_id = %s"
            params.append(location_id)
        if start_date:
            where_clause += " AND o.created_at >= %s"
            params.append(start_date)
        if end_date:
            where_clause += " AND o.created_at <= %s"
            params.append(end_date)
        if q:
            where_clause += " AND o.order_data ILIKE %s"
            params.append(f"%{q}%")

        # Get total count
        cursor.execute(f"""
            SELECT COUNT(*) as count FROM orders o
            JOIN locations l ON o.location_id = l.id
            {where_clause}
        """, params)
        total = cursor.fetchone()["count"]
        
        # Get orders
        cursor.execute(f"""
            SELECT o.*, l.name as location_name
            FROM orders o
            JOIN locations l ON o.location_id = l.id
            {where_clause}
            ORDER BY o.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        
        orders = []
        for row in cursor.fetchall():
            order = dict(row)
            try:
                order_data = json.loads(order.get("order_data") or "{}")
            except Exception:
                order_data = {}

            orders.append({
                "id": order["id"],
                "session_id": order["session_id"],
                "location_id": order["location_id"],
                "location_name": order["location_name"],
                "business_name": order_data.get("business_name"),
                "manager_name": order_data.get("manager_name"),
                "staff_name": order_data.get("staff_name"),
                "distributors": order_data.get("distributors", []),
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
            WHERE o.id = %s AND l.user_id = %s
        """, (order_id, user_id))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "not_found",
                "message": "Order not found"
            })
        
        order = dict(row)
        try:
            order_data = json.loads(order.get("order_data") or "{}")
        except Exception:
            order_data = {}

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
                "business_name": order_data.get("business_name"),
                "manager_name": order_data.get("manager_name"),
                "staff_name": order_data.get("staff_name"),
                "distributors": order_data.get("distributors", []),
                "total_items": order["total_items"],
                "estimated_cost": order["estimated_cost"],
                "created_at": order["created_at"],
                "exported_at": order["exported_at"],
                "export_format": order["export_format"],
                "export_destination": order["export_destination"]
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
            WHERE o.id = %s AND l.user_id = %s
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
            SET exported_at = %s, export_format = %s, export_destination = %s
            WHERE id = %s
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
            cursor.execute("SELECT id, status FROM inventory_sessions WHERE id = %s", (session_data.id,))
            existing = cursor.fetchone()
            
            if existing:
                # Update if needed
                if existing["status"] == "in_progress" and session_data.status == "completed":
                    cursor.execute("""
                        UPDATE inventory_sessions
                        SET status = %s, completed_at = %s, updated_at = %s
                        WHERE id = %s
                    """, (session_data.status, session_data.completed_at, now, session_data.id))
                    sessions_updated += 1
            else:
                # Verify location belongs to user
                cursor.execute(
                    "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
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
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (session_data.id, session_data.location_id, user_id, session_data.started_at,
                      session_data.completed_at, session_data.status, now, now))
                sessions_created += 1
            
            # Process scans
            for scan_data in session_data.scans:
                # Check idempotency
                if scan_data.idempotency_key:
                    cursor.execute(
                        "SELECT id FROM scans WHERE idempotency_key = %s",
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
                    VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s)
                """, (scan_id, session_data.id, scan_data.product_id, scan_data.level, level_decimal,
                      scan_data.detection_method, scan_data.idempotency_key, now,
                      scan_data.created_at, now))
                scans_created += 1
            
            # Process voice notes
            for vn_data in session_data.voice_notes:
                note_id = generate_id()
                cursor.execute("""
                    INSERT INTO voice_notes (id, session_id, audio_url, transcript, linked_product_id, duration_seconds, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (note_id, session_data.id, vn_data.audio_url, vn_data.transcript,
                      vn_data.linked_product_id, vn_data.duration_seconds, now))
        
        # Process par level updates
        for pl_update in sync_data.par_level_updates:
            # Verify location belongs to user
            cursor.execute(
                "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
                (pl_update.location_id, user_id)
            )
            if not cursor.fetchone():
                continue
            
            pl_id = generate_id()
            cursor.execute("""
                INSERT INTO par_levels (id, location_id, product_id, par_quantity, full_quantity, current_stock, updated_at)
                VALUES (%s, %s, %s, %s, 0, 0, %s)
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
                WHERE created_at > %s
                ORDER BY created_at DESC
                LIMIT 50
            """, (sync_data.last_sync_at.isoformat(),))
            server_updates["products"] = [dict(row) for row in cursor.fetchall()]
            
            # Get user's locations
            cursor.execute("""
                SELECT * FROM locations
                WHERE user_id = %s AND (created_at > %s OR updated_at > %s)
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
            "SELECT * FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
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
            WHERE pl.location_id = %s
        """, (location_id,))
        
        par_levels = []
        for row in cursor.fetchall():
            pl = {
                "id": row["id"],
                "location_id": row["location_id"],
                "product_id": row["product_id"],
                "par_quantity": row["par_quantity"],
                "full_quantity": float(row["full_quantity"] or 0),
                "current_stock": float(row["current_stock"] or 0),
                "price": float(row["price"]) if row["price"] else None,
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
            WHERE location_id = %s
            ORDER BY started_at DESC
            LIMIT 5
        """, (location_id,))
        recent_sessions = [dict(row) for row in cursor.fetchall()]
        
        # Get products used at this location
        cursor.execute("""
            SELECT DISTINCT p.* FROM products p
            JOIN scans s ON p.id = s.product_id
            JOIN inventory_sessions ses ON s.session_id = ses.id
            WHERE ses.location_id = %s
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
            WHERE user_id = %s AND deleted_at IS NULL
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
        cursor.execute("SELECT * FROM distributors WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
                       (distributor_id, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Distributor not found"})
        now = now_iso()
        updates = []
        params = []
        if distributor_data.name is not None:
            updates.append("name = %s")
            params.append(distributor_data.name)
        if distributor_data.email is not None:
            updates.append("email = %s")
            params.append(distributor_data.email)
        if distributor_data.phone is not None:
            updates.append("phone = %s")
            params.append(distributor_data.phone)
        if distributor_data.rep_name is not None:
            updates.append("rep_name = %s")
            params.append(distributor_data.rep_name)
        updates.append("updated_at = %s")
        params.append(now)
        params.append(distributor_id)
        cursor.execute(f"UPDATE distributors SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
        return {"success": True, "message": "Distributor updated"}

@v1_router.delete("/distributors/{distributor_id}")
def delete_distributor(distributor_id: str, user_id: str = Depends(get_current_user)):
    """Soft delete distributor"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM distributors WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
                       (distributor_id, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Distributor not found"})
        cursor.execute("UPDATE distributors SET deleted_at = %s, updated_at = %s WHERE id = %s",
                       (now_iso(), now_iso(), distributor_id))
        conn.commit()
        return {"success": True, "message": "Distributor deleted"}

@v1_router.post("/locations/{location_id}/product-distributors", response_model=dict)
def assign_product_distributor(location_id: str, assignment: LocationProductDistributorCreate,
                                user_id: str = Depends(get_current_user)):
    """Assign a product to a distributor for a location"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
                       (location_id, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Access denied"})
        assignment_id = generate_id()
        now = now_iso()
        cursor.execute("""
            INSERT INTO location_product_distributors (id, location_id, product_id, distributor_id, created_at)
            VALUES (%s, %s, %s, %s, %s)
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
        cursor.execute("SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
                       (location_id, user_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Access denied"})
        cursor.execute("""
            SELECT lpd.*, d.name as distributor_name, d.email as distributor_email,
                   p.name as product_name, p.brand as product_brand, p.size as product_size
            FROM location_product_distributors lpd
            JOIN distributors d ON lpd.distributor_id = d.id
            JOIN products p ON lpd.product_id = p.id
            WHERE lpd.location_id = %s AND d.deleted_at IS NULL
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
            WHERE o.id = %s AND l.user_id = %s
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
                WHERE lpd.location_id = %s AND lpd.product_id = %s AND d.deleted_at IS NULL
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

# ============== ORDER EMAIL SENDING (Resend) ==============
# Requires RESEND_API_KEY on the server. Until a domain is verified in Resend,
# ORDER_EMAIL_FROM must stay on the sandbox sender (onboarding@resend.dev),
# which can only deliver to the Resend account owner's own address.

class OrderEmailItem(BaseModel):
    name: str
    quantity: float
    size: str = ""
    price: float | None = None


class DistributorOrder(BaseModel):
    distributor_id: str
    items: list[OrderEmailItem] = Field(min_length=1)


class SendOrderEmailsRequest(BaseModel):
    location_id: str
    location_name: str = "your bar"
    staff_name: str | None = None
    orders: list[DistributorOrder] = Field(min_length=1, max_length=50)


def _send_via_resend(api_key: str, to_email: str, subject: str, body_text: str, reply_to: str | None = None) -> tuple[bool, str | None]:
    """Send one email through the Resend API. Returns (ok, error_message)."""
    sender = os.getenv("ORDER_EMAIL_FROM", "86'd Orders <onboarding@resend.dev>")
    payload = {"from": sender, "to": [to_email], "subject": subject, "text": body_text}
    if reply_to:
        payload["reply_to"] = reply_to
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=15.0,
        )
        if resp.status_code in (200, 201):
            return (True, None)
        try:
            detail = resp.json().get("message", resp.text[:200])
        except Exception:
            detail = resp.text[:200]
        return (False, f"{resp.status_code}: {detail}")
    except Exception as e:
        return (False, str(e))


@v1_router.post("/orders/email")
def send_order_emails(request: SendOrderEmailsRequest, user_id: str = Depends(get_current_user)):
    """Send order emails to distributors, one email per distributor."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail={
            "error": "email_not_configured",
            "message": "Email sending is not configured on the server (RESEND_API_KEY missing)"
        })

    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    results = []
    order_distributors = []  # mirrors `results` but also carries each distributor's line items, for order history
    all_items = []

    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id FROM locations WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
            (request.location_id, user_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Location not found"})

        cursor.execute(
            "SELECT name, email, business_name, manager_name FROM users WHERE id = %s AND deleted_at IS NULL",
            (user_id,)
        )
        user_row = cursor.fetchone()
        business_name = (user_row["business_name"] if user_row else None) or request.location_name
        manager_name = (user_row["manager_name"] if user_row else None) or (user_row["name"] if user_row else None) or business_name
        reply_to = user_row["email"] if user_row else None
        location_suffix = (
            f" ({request.location_name})"
            if request.location_name and request.location_name != business_name
            else ""
        )

        for order in request.orders:
            item_dicts = [
                {"name": i.name, "quantity": i.quantity, "size": i.size or None, "price": i.price}
                for i in order.items
            ]
            all_items.extend(item_dicts)

            cursor.execute(
                "SELECT id, name, email FROM distributors WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
                (order.distributor_id, user_id)
            )
            dist = cursor.fetchone()
            if not dist:
                results.append({
                    "distributor_id": order.distributor_id, "distributor_name": None,
                    "email": None, "status": "failed", "error": "Distributor not found"
                })
                order_distributors.append({
                    "distributor_id": order.distributor_id, "distributor_name": None,
                    "email": None, "status": "failed", "items": item_dicts
                })
                continue
            if not dist["email"]:
                results.append({
                    "distributor_id": dist["id"], "distributor_name": dist["name"],
                    "email": None, "status": "no_email",
                    "error": "No email address on file for this distributor"
                })
                order_distributors.append({
                    "distributor_id": dist["id"], "distributor_name": dist["name"],
                    "email": None, "status": "no_email", "items": item_dicts
                })
                continue

            lines = []
            total_qty = 0.0
            for item in order.items:
                qty = item.quantity
                total_qty += qty
                qty_str = str(int(qty)) if qty == int(qty) else f"{qty:g}"
                size_str = f" {item.size}" if item.size else ""
                lines.append(f"- {item.name}{size_str} x {qty_str}")

            total_str = str(int(total_qty)) if total_qty == int(total_qty) else f"{total_qty:g}"
            subject = f"Order from {business_name} — {today}"
            body_text = f"""Hi {dist['name']},

This is an order from {business_name}{location_suffix}. Please prepare the following for pickup/delivery:

{chr(10).join(lines)}

Total: {total_str} bottles

Thank you,
{manager_name}
{business_name}
(sent via 86'd bar inventory)"""

            ok, error = _send_via_resend(api_key, dist["email"], subject, body_text, reply_to=reply_to)
            results.append({
                "distributor_id": dist["id"], "distributor_name": dist["name"],
                "email": dist["email"], "status": "sent" if ok else "failed",
                "error": error
            })
            order_distributors.append({
                "distributor_id": dist["id"], "distributor_name": dist["name"],
                "email": dist["email"], "status": "sent" if ok else "failed", "items": item_dicts
            })
            if not ok:
                print(f"[send_order_emails] failed for {dist['name']} <{dist['email']}>: {error}", flush=True)

        # Persist a record of this order for history, regardless of send outcome —
        # the manager should be able to look back at what was attempted/ordered.
        now = now_iso()
        session_id = generate_id()
        cursor.execute("""
            INSERT INTO inventory_sessions (id, location_id, user_id, started_at, completed_at, status, total_bottles, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s, %s)
        """, (session_id, request.location_id, user_id, now, now, len(all_items), now, now))

        order_id = generate_id()
        sent_emails = [r["email"] for r in results if r["status"] == "sent" and r["email"]]
        total_cost = sum(
            item["price"] * item["quantity"] for item in all_items if item.get("price") is not None
        )
        order_data = {
            "distributors": order_distributors,
            "items": all_items,
            "business_name": business_name,
            "manager_name": manager_name,
            "staff_name": request.staff_name,
            "location_name": request.location_name,
            "total_cost": total_cost if total_cost > 0 else None,
        }
        cursor.execute("""
            INSERT INTO orders (id, session_id, location_id, order_data, total_items, estimated_cost, exported_at, export_format, export_destination, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            order_id, session_id, request.location_id, json.dumps(order_data), len(all_items),
            order_data["total_cost"], now, "email", ", ".join(sent_emails) or None, now
        ))
        conn.commit()

    sent = sum(1 for r in results if r["status"] == "sent")
    return {
        "order_id": order_id,
        "results": results,
        "sent": sent,
        "failed": len(results) - sent,
    }

# ============== BILLING (Stripe) ==============
# No card is ever collected at signup — every account gets a 30-day trial
# (see register_user) and only talks to Stripe once they hit "Subscribe."
# Checkout happens in the system browser (not an embedded webview), and a
# webhook is the only thing that ever flips subscription_status to 'active'.

import stripe

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

APP_BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "https://eight6d-api.onrender.com")


def is_entitled(subscription_status: str, trial_ends_at) -> bool:
    """True if the account can use paid features right now: an active paid
    subscription, or a trial that hasn't expired yet."""
    if subscription_status == "active":
        return True
    if subscription_status == "trial":
        if not trial_ends_at:
            return True
        try:
            ends = trial_ends_at if isinstance(trial_ends_at, datetime) else datetime.fromisoformat(str(trial_ends_at))
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=timezone.utc)
            return ends > datetime.now(timezone.utc)
        except Exception:
            return True  # unparsable date shouldn't lock someone out
    return False


# Trial ending is a hard, silent cliff otherwise — a heads-up email a few
# days out, on top of the in-app banner, so it's not a total surprise.
TRIAL_REMINDER_DAYS_BEFORE = 5
TRIAL_REMINDER_CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # every 6 hours


def _send_trial_reminder_emails():
    """Email trial users whose trial ends within TRIAL_REMINDER_DAYS_BEFORE
    days and haven't been reminded yet. Runs on a background loop, not
    per-request — see _trial_reminder_loop / lifespan."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        return

    now = datetime.now(timezone.utc)
    cutoff = (now + timedelta(days=TRIAL_REMINDER_DAYS_BEFORE)).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, email, name, manager_name, trial_ends_at
            FROM users
            WHERE subscription_status = 'trial'
              AND trial_reminder_sent_at IS NULL
              AND trial_ends_at IS NOT NULL
              AND trial_ends_at <= %s
              AND trial_ends_at > %s
              AND deleted_at IS NULL
        """, (cutoff, now.isoformat()))
        rows = cursor.fetchall()

        for row in rows:
            try:
                ends = datetime.fromisoformat(row["trial_ends_at"])
                if ends.tzinfo is None:
                    ends = ends.replace(tzinfo=timezone.utc)
                days_left = max(0, (ends - now).days)
                display_name = row["manager_name"] or row["name"] or "there"
                date_str = ends.strftime("%B %d, %Y")

                subject = f"Your 86'd trial ends in {days_left} day{'s' if days_left != 1 else ''}"
                body = f"""Hi {display_name},

Your free trial of 86'd ends on {date_str}. After that, you'll need an active subscription to keep scanning, ordering, and tracking your bar's inventory — nothing you've entered will be lost, but you won't be able to use the app again until you subscribe.

Open the 86'd app and tap Subscribe to keep going without any interruption.

Thanks,
The 86'd team"""

                ok, error = _send_via_resend(api_key, row["email"], subject, body)
                if ok:
                    cursor.execute(
                        "UPDATE users SET trial_reminder_sent_at = %s, updated_at = %s WHERE id = %s",
                        (now_iso(), now_iso(), row["id"])
                    )
                    conn.commit()
                else:
                    print(f"[trial_reminder] failed to email {row['email']}: {error}", flush=True)
            except Exception as e:
                print(f"[trial_reminder] error processing user {row['id']}: {e}", flush=True)


async def _trial_reminder_loop():
    """Runs for the life of the process, periodically checking for trial
    users who need a reminder email. Best-effort — errors never crash
    the app or block startup."""
    while True:
        try:
            await asyncio.to_thread(_send_trial_reminder_emails)
        except Exception as e:
            print(f"[trial_reminder] loop error: {e}", flush=True)
        await asyncio.sleep(TRIAL_REMINDER_CHECK_INTERVAL_SECONDS)


@v1_router.post("/billing/create-checkout-session")
def create_checkout_session(user_id: str = Depends(get_current_user)):
    """Create a Stripe Checkout session for the current user and hand back
    its URL — the app opens this in Safari, it never touches Stripe directly."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail={
            "error": "billing_not_configured",
            "message": "Billing isn't set up on the server yet (STRIPE_SECRET_KEY missing)"
        })
    price_id = os.getenv("STRIPE_PRICE_ID")
    if not price_id:
        raise HTTPException(status_code=503, detail={
            "error": "billing_not_configured",
            "message": "Billing isn't set up on the server yet (STRIPE_PRICE_ID missing)"
        })

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT email, stripe_customer_id FROM users WHERE id = %s AND deleted_at IS NULL",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})

        customer_id = row["stripe_customer_id"]
        if not customer_id:
            customer = stripe.Customer.create(email=row["email"], metadata={"user_id": user_id})
            customer_id = customer.id
            cursor.execute(
                "UPDATE users SET stripe_customer_id = %s, updated_at = %s WHERE id = %s",
                (customer_id, now_iso(), user_id)
            )
            conn.commit()

    session = stripe.checkout.Session.create(
        customer=customer_id,
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/billing/success",
        cancel_url=f"{APP_BASE_URL}/billing/cancel",
        client_reference_id=user_id,
        subscription_data={"metadata": {"user_id": user_id}},
    )
    return {"checkout_url": session.url}


def _billing_page(title: str, message: str) -> str:
    return f"""
        <html>
          <head><meta name="viewport" content="width=device-width, initial-scale=1" /></head>
          <body style="font-family: -apple-system, sans-serif; background: #0F0F0F; color: #fff;
                       display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0;">
            <div style="text-align: center; padding: 24px;">
              <h1 style="margin-bottom: 8px;">{title}</h1>
              <p style="color: #999;">{message}</p>
            </div>
          </body>
        </html>
    """


@app.get("/billing/success")
def billing_success():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_billing_page("You're all set!", "Head back to the 86'd app — your subscription is active."))


@app.get("/billing/cancel")
def billing_cancel():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_billing_page("No charge made", "You can head back to the 86'd app any time to subscribe."))


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    """Stripe calls this directly — verified via signature, not a user JWT.
    This is the only thing allowed to flip subscription_status to 'active'."""
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=503, detail={"error": "webhook_not_configured"})

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail={"error": "invalid_signature"})

    obj = event["data"]["object"]
    now = now_iso()

    with get_db() as conn:
        cursor = conn.cursor()

        if event["type"] == "checkout.session.completed":
            user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id")
            customer_id = obj.get("customer")
            if user_id:
                cursor.execute(
                    "UPDATE users SET subscription_status = 'active', stripe_customer_id = %s, updated_at = %s WHERE id = %s",
                    (customer_id, now, user_id)
                )
                conn.commit()

        elif event["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
            customer_id = obj.get("customer")
            status = obj.get("status")  # active, past_due, canceled, unpaid, etc.
            new_status = "active" if status == "active" else ("trial" if status == "trialing" else "canceled")
            if customer_id:
                cursor.execute(
                    "UPDATE users SET subscription_status = %s, updated_at = %s WHERE stripe_customer_id = %s",
                    (new_status, now, customer_id)
                )
                conn.commit()

    return {"received": True}

# ============== V1 USERS ==============

@v1_router.get("/users/me", response_model=UserProfileResponse)
def get_user_profile(user_id: str = Depends(get_current_user)):
    """Get full user profile including subscription status"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, email, name, business_name, manager_name, subscription_status,
                   subscription_tier, trial_ends_at, terms_accepted_at, privacy_accepted_at, created_at
            FROM users WHERE id = %s AND deleted_at IS NULL
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        result = dict(row)
        result["subscription_status"] = result.get("subscription_status") or "trial"
        result["subscription_tier"] = result.get("subscription_tier") or "starter"
        return result

@v1_router.patch("/users/me", response_model=UserProfileResponse)
def update_user_profile(request: UpdateProfileRequest, user_id: str = Depends(get_current_user)):
    """Update the current user's business_name / manager_name."""
    updates = request.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail={"error": "no_fields", "message": "No fields to update"})

    with get_db() as conn:
        cursor = conn.cursor()
        set_clauses = ", ".join(f"{col} = %s" for col in updates)
        cursor.execute(
            f"UPDATE users SET {set_clauses}, updated_at = %s WHERE id = %s AND deleted_at IS NULL",
            (*updates.values(), now_iso(), user_id)
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        conn.commit()

        cursor.execute("""
            SELECT id, email, name, business_name, manager_name, subscription_status,
                   subscription_tier, trial_ends_at, terms_accepted_at, privacy_accepted_at, created_at
            FROM users WHERE id = %s AND deleted_at IS NULL
        """, (user_id,))
        row = cursor.fetchone()
        result = dict(row)
        result["subscription_status"] = result.get("subscription_status") or "trial"
        result["subscription_tier"] = result.get("subscription_tier") or "starter"
        return result

@v1_router.delete("/users/me")
def delete_user(request: DeleteAccountRequest, user_id: str = Depends(get_current_user)):
    """Soft delete user account (GDPR / App Store guideline 5.1.1 compliance).
    Password re-confirmation comes in the request body — it was previously a
    query parameter, which put passwords in URLs and access logs."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = %s AND deleted_at IS NULL", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        if not verify_password(request.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail={"error": "invalid_password", "message": "Password is incorrect"})
        cursor.execute("""
            UPDATE users SET deleted_at = %s, email = CONCAT(email, '.deleted.', %s), updated_at = %s
            WHERE id = %s
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
            UPDATE users SET terms_accepted_at = %s, privacy_accepted_at = %s, updated_at = %s
            WHERE id = %s
        """, (now, now, now, user_id))
        conn.commit()
        return {"success": True, "message": "Terms accepted successfully"}

# ============== V1 ADDITIONAL AUTH ==============

@v1_router.post("/auth/forgot-password")
def forgot_password(request: ForgotPasswordRequest):
    """Email a 6-digit reset code, valid for 30 minutes.

    Always returns the same generic response regardless of whether the
    account exists or the email actually sent — do not leak either signal
    to the caller. The code itself is only ever delivered by email, never
    in the API response (it used to be, via a `debug_token` field — that
    was a full account-takeover hole for anyone who knew a user's email)."""
    email = request.email.lower().strip()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name, password_reset_expires_at FROM users WHERE email = %s AND deleted_at IS NULL",
            (email,)
        )
        row = cursor.fetchone()
        if row:
            # A code issued within the cooldown window is still fresh enough
            # in the recipient's inbox — skip regenerating/resending so a
            # rapid double-tap (or deliberate spam) doesn't fire two emails
            # or invalidate a code the user is mid-typing.
            existing_expiry = row.get("password_reset_expires_at")
            issued_recently = False
            if existing_expiry:
                try:
                    remaining = datetime.fromisoformat(existing_expiry) - datetime.now(timezone.utc)
                    issued_recently = remaining.total_seconds() > (
                        PASSWORD_RESET_EXPIRE_MINUTES * 60 - PASSWORD_RESET_RESEND_COOLDOWN_SECONDS
                    )
                except ValueError:
                    issued_recently = False

            if not issued_recently:
                code = f"{random.randint(0, 999999):06d}"
                expires_at = (datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES)).isoformat()
                cursor.execute("""
                    UPDATE users SET password_reset_token = %s, password_reset_expires_at = %s
                    WHERE id = %s
                """, (code, expires_at, row["id"]))
                conn.commit()

                api_key = os.getenv("RESEND_API_KEY")
                if api_key:
                    ok, error = _send_via_resend(
                        api_key, email,
                        "Your 86'd password reset code",
                        f"""Hi{' ' + row['name'] if row['name'] else ''},

Your password reset code is: {code}

This code expires in {PASSWORD_RESET_EXPIRE_MINUTES} minutes. If you didn't request this, you can ignore this email.

(sent via 86'd bar inventory)"""
                )
                if not ok:
                    print(f"[forgot_password] failed to send reset email to {email}: {error}", flush=True)
            else:
                print(f"[forgot_password] RESEND_API_KEY missing — reset code not sent for {email}", flush=True)

    return {"success": True, "message": "If an account exists, a reset code has been sent"}

@v1_router.post("/auth/reset-password")
def reset_password(request: ResetPasswordRequest):
    """Reset password using the emailed 6-digit code"""
    email = request.email.lower().strip()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM users
            WHERE email = %s AND password_reset_token = %s AND password_reset_expires_at > %s AND deleted_at IS NULL
        """, (email, request.token.strip(), now_iso()))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail={"error": "invalid_token", "message": "Invalid or expired reset code"})
        password_hash = get_password_hash(request.new_password)
        cursor.execute("""
            UPDATE users SET password_hash = %s, password_reset_token = NULL, password_reset_expires_at = NULL, updated_at = %s
            WHERE id = %s
        """, (password_hash, now_iso(), row["id"]))
        conn.commit()
        return {"success": True, "message": "Password reset successfully"}

@v1_router.put("/auth/change-password")
def change_password(request: ChangePasswordRequest, user_id: str = Depends(get_current_user)):
    """Change password (requires current password)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT password_hash FROM users WHERE id = %s AND deleted_at IS NULL", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        if not verify_password(request.current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail={"error": "invalid_password", "message": "Current password is incorrect"})
        password_hash = get_password_hash(request.new_password)
        cursor.execute("UPDATE users SET password_hash = %s, updated_at = %s WHERE id = %s",
                       (password_hash, now_iso(), user_id))
        conn.commit()
        return {"success": True, "message": "Password changed successfully"}

# ============== V1 SCANS (Gemini Vision) ==============

class ScanAnalyzeRequest(BaseModel):
    image: str                          # base64 encoded JPEG
    previous_readings: list[float] = [] # last N raw liquidLevel floats for smoothing (optional)

class ScanAnalyzeResponse(BaseModel):
    name: str
    brand: str
    category: str
    product_type: str = ""  # Specific class/type (e.g. Tennessee Whiskey, Blended Scotch Whisky, Vodka)
    liquidLevel: float
    confidence: float
    levelReadable: bool = True
    needs_rescan: bool = False  # True when confidence is too low for reliable classification
    matched_product_id: Optional[str] = None
    is_new_product: bool = False
    match_method: str = "none"  # "exact", "auto_created", "none"

PRODUCT_CATALOG = """KNOWN PRODUCTS — spelling normalization ONLY. If the product you READ OFF THE LABEL appears below, use this exact spelling. NEVER use this list to substitute a different variant than the one printed on the label; products not listed are fine as transcribed.

Vodka: Tito's Handmade, Grey Goose, Absolut, Ketel One, Belvedere, Stolichnaya, Svedka, New Amsterdam, Skyy, Pinnacle, Cîroc, Deep Eddy, Wheatley, Three Olives, Smirnoff, Burnett's, Luksusowa, Reyka, Iceberg, Prairie Organic, Finlandia, Russian Standard, Żubrówka, UV Blue, Seagram's Extra Smooth
Bourbon: Buffalo Trace, Maker's Mark, Woodford Reserve, Knob Creek, Four Roses Small Batch, Bulleit Bourbon, Wild Turkey 101, Eagle Rare 10, Blanton's Original, Weller Special Reserve, Elijah Craig Small Batch, Heaven Hill, Jim Beam White, Old Forester 86, Larceny Small Batch, Basil Hayden's, Angel's Envy, Russell's Reserve 10, Evan Williams Black, Very Old Barton, 1792 Small Batch, Bardstown Bourbon Discovery, Henry McKenna, W.L. Weller 12, Pappy Van Winkle 15
Tennessee Whiskey: Jack Daniel's Old No. 7, Jack Daniel's Gentleman Jack, Jack Daniel's Single Barrel Select, Jack Daniel's Tennessee Honey, George Dickel No. 12, George Dickel Rye
Irish Whiskey: Jameson, Jameson Black Barrel, Bushmills Original, Tullamore D.E.W., Redbreast 12, Powers Gold Label, The Irishman Founder's Reserve, Connemara Peated, Writers' Tears Copper Pot, Slane Irish Whiskey, Proper No. Twelve, Kilbeggan Traditional
Scotch Blended: Johnnie Walker Red, Johnnie Walker Black, Johnnie Walker Double Black, Johnnie Walker Gold Reserve, Dewar's White Label, Dewar's 12, Chivas Regal 12, Famous Grouse, Monkey Shoulder, Cutty Sark, J&B Rare, Clan MacGregor, Scoresby, Bell's Original, Grant's Family Reserve
Scotch Single Malt: Glenfiddich 12, Glenfiddich 15, Macallan 12 Sherry Oak, Macallan 12 Double Cask, Glenlivet 12, Glenlivet 15, Oban 14, Laphroaig 10, Balvenie 12 DoubleWood, Highland Park 12, Dalmore 12, Auchentoshan Three Wood, Bruichladdich The Classic Laddie, Talisker 10, Springbank 10, Ardbeg 10, Bunnahabhain 12
Rye Whiskey: Bulleit Rye, WhistlePig 10, Sazerac Rye, High West Rendezvous Rye, Redemption Rye, Rittenhouse Rye 100, George Dickel Rye, Templeton Rye, Knob Creek Rye, Old Overholt Rye, Pikesville Rye, Lot 40 Rye
Canadian Whisky: Crown Royal Deluxe, Crown Royal Apple, Crown Royal Peach, Crown Royal Black, Canadian Club, Pendleton Original, Forty Creek Barrel Select, Seagram's VO
Japanese Whisky: Suntory Toki, Nikka Coffey Grain, Hibiki Japanese Harmony, Yamazaki 12, Hakushu 12, Roku Gin (Japanese gin)
Tequila Blanco: Patrón Silver, Don Julio Blanco, Casamigos Blanco, Herradura Silver, Espolòn Blanco, Olmeca Altos Plata, El Jimador Silver, Jose Cuervo Silver, 1800 Silver, Milagro Silver, Clase Azul Plata, Hornitos Plata, Lunazul Blanco, Cazadores Blanco, Teremana Blanco
Tequila Reposado: Patrón Reposado, Don Julio Reposado, Casamigos Reposado, Herradura Reposado, Olmeca Altos Reposado, Espolòn Reposado, 1800 Reposado, Cazadores Reposado
Tequila Añejo: Don Julio Añejo, Patrón Añejo, Casamigos Añejo, 1800 Añejo, Herradura Añejo, Gran Centenario Añejo
Mezcal: Del Maguey Vida, Ilegal Joven, Montelobos, Banhez Ensemble,Putaendo, Wahaka Madre Cuishe,Putaendo, Alipús San Andres
Gin: Tanqueray London Dry, Tanqueray No. Ten, Hendrick's, Bombay Sapphire, Beefeater London Dry, Sipsmith London Dry, Aviation American Gin, The Botanist, Monkey 47, Plymouth Gin, New Amsterdam Gin, Malfy Con Limone, Empress 1908, Fords Gin, Nolet's Silver, Hayman's Old Tom, Drumshanbo Gunpowder Irish Gin
Rum White/Silver: Bacardi Superior, Bacardi Gold, Plantation 3 Stars, Mount Gay Eclipse, Cruzan Light, Flor de Caña Extra Dry 4, Don Q Cristal, Brugal Extra Dry
Rum Dark/Spiced: Captain Morgan Original Spiced, Kraken Black Spiced, Sailor Jerry Spiced, Myers's Original Dark, Gosling's Black Seal, Diplomatico Reserva Exclusiva, Appleton Estate Signature, El Dorado 12, Zaya Gran Reserva, Angostura 1919, Pusser's Blue Label, Plantation Original Dark, Ron Zacapa 23
Brandy/Cognac: Hennessy VS, Hennessy VSOP, Rémy Martin VSOP, Rémy Martin 1738, Courvoisier VS, Martell VS, E&J VSOP, Paul Masson Grande Amber VSOP, Korbel California Brandy, Christian Brothers VS, Torres 10 Imperial Brandy
Liqueurs/Triple Sec: Cointreau, Grand Marnier Cordon Rouge, DeKuyper Triple Sec, Patron Citrónge, Blue Curaçao, Luxardo Maraschino
Amaretto/Nut: Disaronno Originale, Amaretto di Saronno, Frangelico Hazelnut, Nocello Walnut, Kahlúa Original, Kahlúa Especial, Tia Maria Coffee
Cream/Sweet: Baileys Original Irish Cream, RumChata, Carolans Irish Cream, St. Brendan's Irish Cream, Mozart Dark Chocolate
Herbal/Bitter: Jägermeister, Campari, Aperol, Fernet-Branca, Cynar, Aperol, Amaro Averna, Montenegro Amaro, Bénédictine, Chartreuse Green, Chartreuse Yellow, Lillet Blanc, Lillet Rosé
Fruit/Berry: Chambord Black Raspberry, Midori Melon, Peach Schnapps DeKuyper, St-Germain Elderflower, Crème de Cassis, Limoncello Pallini, Aperol, Pama Pomegranate
Peppermint/Cinnamon: Fireball Cinnamon Whisky, Rumple Minze Peppermint, DeKuyper Peppermint Schnapps, Templeton Rye Cinnamon
Coconut/Tropical: Malibu Coconut Rum, DKNY Coconut, Malibu Mango, Blue Chair Bay Coconut
Vermouth/Fortified: Martini & Rossi Sweet Vermouth, Martini & Rossi Dry Vermouth, Noilly Prat Dry, Dolin Dry, Carpano Antica Formula, Mancino Secco
Beer (common): Bud Light, Budweiser, Coors Light, Miller Lite, Miller High Life, Corona Extra, Modelo Especial, Dos Equis Lager, Heineken, Stella Artois, Blue Moon Belgian White, Shock Top, Sam Adams Boston Lager, Guinness Draught, Sierra Nevada Pale Ale, Lagunitas IPA, Bell's Two Hearted
Wine (common): Kim Crawford Sauvignon Blanc, Kendall-Jackson Vintner's Reserve Chardonnay, Josh Cellars Cabernet Sauvignon, La Marca Prosecco, Meiomi Pinot Noir, Whispering Angel Rosé, Barefoot Pinot Grigio, Bogle Essential Red, Chateau Ste. Michelle Riesling
Soda (common): Sprite Original, Coca-Cola Classic, Coca-Cola Diet Coke, Pepsi Original, Fanta Orange, Canada Dry Ginger Ale
Mixers/Juice (common): Schweppes Tonic Water, Fever-Tree Tonic Water, Schweppes Club Soda, Red Bull Energy Drink, Ocean Spray Cranberry Juice, Tropicana Orange Juice, Dole Pineapple Juice, Rose's Lime Juice, Rose's Grenadine"""

BOTTLE_PROMPT = """You are identifying a beverage container (liquor, beer, wine, soda, mixers, water — glass, plastic, or can) from a photo for bar inventory.

Your ONLY job is to identify the exact product. Nothing else matters.

CRITICAL — identification is a READING task, not a recall task:
- The name and brand MUST come from text printed on the label. TRANSCRIBE the label exactly as printed.
- Do NOT infer the flavor or variant from the liquid color, cap color, bottle shape, or from which variants are most popular for that brand. Example: if a Gatorade label prints "BLUE BOLT", the name is "Blue Bolt" — NOT "Glacier Freeze", "Cool Blue", or any other blue variant you associate with the brand.
- If the variant name is not clearly legible in the photo, use the generic descriptor printed on the label (e.g. "Sports Drink") as the name and cap confidence at 0.5. A generic name is always better than a guessed variant.
- Before returning, self-check: "Can I point to the exact pixels where the name I'm returning is printed?" If not, you are guessing — fall back to the generic descriptor.

BASE PRODUCTS — descriptors are not variant names:
- Many flagship products print NO variant name — only the brand plus a flavor/class descriptor. Example: a standard Sprite bottle prints "Sprite" and "Carbonated Lemon-Lime Flavored Drink". "Lemon-Lime" there is a DESCRIPTOR of the base product, not a variant.
- When the label shows only the brand + a descriptor (no explicit variant), return name "Original". This must be deterministic: every scan of that same bottle must produce the same name.
- Return a distinct variant name ONLY when the label prints an explicit variant (e.g. "Zero Sugar", "Cherry", "Blue Bolt", "Tropical Mix"). Descriptor phrases like "flavored drink", "original taste", "classic", "carbonated beverage" mean base product → "Original".

How to read the label:
1. Find the largest brand wordmark (e.g. GATORADE, JACK DANIEL'S) — that is the brand.
2. Find the variant/expression/flavor text, usually smaller and near the brand (e.g. BLUE BOLT, OLD NO. 7, RED LABEL) — that is the name. If there is no variant text — only a flavor/class descriptor — the name is "Original".
3. Use any printed class designation for product_type (e.g. SPORTS DRINK, TENNESSEE WHISKEY, LONDON DRY GIN).
4. If the label is angled, partially hidden, or blurry, transcribe what is clearly legible and lower confidence accordingly — never fill gaps from memory.

Return ONLY a JSON object — no markdown, no explanation:
{
  "name": "Variant/expression name only (e.g. Old No. 7, Red Label, Blue Bolt, Original)",
  "brand": "Brand/distillery name only (e.g. Jack Daniel's, Johnnie Walker, Gatorade)",
  "category": "one of: spirits | beer | wine | soda | mixer | water | juice | other",
  "product_type": "Specific class and type (e.g. Tennessee Whiskey, Blended Scotch Whisky, Vodka, Lemon-Lime Soda, Sports Drink)",
  "confidence": 0.9
}

Rules:
- name: variant/expression only — do NOT include the brand name in this field. Use "Original" for a brand's base product with no printed variant name.
- brand: brand/distillery name only — do NOT include the variant or product type
- product_type: the specific regulatory or descriptive class (e.g. Tennessee Whiskey, Bourbon Whiskey, Blended Scotch Whisky, London Dry Gin, Silver Tequila, Aged Rum, Vodka, Lemon-Lime Soda, Cola, Tonic Water, Sports Drink, Energy Drink). Use the label's own designation when visible.
- category must be one of: spirits, beer, wine, soda, mixer, water, juice, other
- confidence is 0.0-1.0 and reflects how certain you are of the EXACT product (brand + variant)
- If no bottle or can is present at all, return: {"name":"","brand":"","category":"other","product_type":"","confidence":0}
- Return ONLY valid JSON.

""" + PRODUCT_CATALOG


# ─── AI provider helpers ───────────────────────────────────────────────────

OPENAI_MODEL = "gpt-4o"
GEMINI_MODEL = "gemini-2.0-flash"

# Scan accuracy config — override via environment variables if needed.
# CONFIDENCE_THRESHOLD:     AI confidence below this triggers needs_rescan=True.
# LEVEL_DEADBAND:           half-width of the hysteresis deadband (±) around
#                           each level boundary used by classify_level().
# LEVEL_STABILIZATION:      master toggle for smoothing + confidence-aware
#                           stickiness. Set to "false" to disable for rollback.
# SMOOTHING_WINDOW:         how many consecutive readings to median-smooth
#                           (client passes previous_readings in the request).
CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.35"))
LEVEL_DEADBAND: float = float(os.getenv("LEVEL_DEADBAND", "0.03"))
LEVEL_STABILIZATION: bool = os.getenv("LEVEL_STABILIZATION", "true").lower() not in ("false", "0", "no")
SMOOTHING_WINDOW: int = max(1, int(os.getenv("SMOOTHING_WINDOW", "3")))
PROVIDER_TIMEOUT: float = float(os.getenv("PROVIDER_TIMEOUT", "9.0"))
TOTAL_SCAN_TIMEOUT_SEC: float = float(os.getenv("TOTAL_SCAN_TIMEOUT_SEC", "20.0"))
AUTO_CREATE_CONFIDENCE: float = float(os.getenv("AUTO_CREATE_CONFIDENCE", "0.4"))


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner).strip()
    return text


def _parse_ai_result(text: str) -> dict:
    text = _strip_code_fences(text)
    result = json.loads(text)
    if "category" in result:
        result["category"] = result["category"].lower()
    result.setdefault("levelReadable", True)
    result.setdefault("confidence", 0.5)
    result.setdefault("name", "")
    result.setdefault("brand", "")
    result.setdefault("category", "other")
    result.setdefault("product_type", "")
    result.setdefault("liquidLevel", 0.0)
    return result


def _apply_stabilization(result: dict, previous_readings: list[float]) -> dict:
    """Apply temporal smoothing and confidence-aware stickiness when enabled.

    Mutates and returns the result dict.  Safe to call even when
    LEVEL_STABILIZATION is False — it becomes a no-op.
    """
    if not LEVEL_STABILIZATION:
        return result

    confidence = result.get("confidence", 0.5)

    # Temporal smoothing: median the last N readings (including this one).
    if previous_readings:
        all_readings = list(previous_readings) + [result["liquidLevel"]]
        smoothed = smooth_level(all_readings, SMOOTHING_WINDOW)
        print(
            f"[stabilization] raw={result['liquidLevel']:.3f} "
            f"smoothed={smoothed:.3f} window={min(len(all_readings), SMOOTHING_WINDOW)}",
            flush=True,
        )
        result["liquidLevel"] = smoothed

    # Confidence-aware stickiness is handled inside classify_level() via the
    # confidence kwarg — no extra work needed here; classify_level widens the
    # deadband automatically when confidence < 0.5.
    # (Callers that bucket should pass confidence= to classify_level.)

    return result


async def _call_openai(api_key: str, prompt: str, image_data: str) -> str:
    client = openai.AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=300,
            timeout=60.0,  # SDK fallback; asyncio.wait_for is the real gate
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_data}",
                                "detail": "high",
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        ),
        timeout=PROVIDER_TIMEOUT,
    )
    return response.choices[0].message.content.strip()


async def _call_gemini(api_key: str, prompt: str, image_data: str) -> str:
    import base64
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    image_bytes = base64.b64decode(image_data)
    response = await asyncio.wait_for(
        asyncio.to_thread(
            model.generate_content,
            [prompt, {"mime_type": "image/jpeg", "data": image_bytes}]
        ),
        timeout=PROVIDER_TIMEOUT,
    )
    return response.text.strip()


async def _warm_providers() -> dict:
    """Open TLS connections / init the AI clients so the first real scan doesn't
    pay the cold-path setup (observed ~8s extra on the first scan). Best-effort,
    never raises. Costs ~1 token per provider per call."""
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    warmed = {"openai": False, "gemini": False}

    if openai_key:
        try:
            client = openai.AsyncOpenAI(api_key=openai_key)
            await asyncio.wait_for(
                client.chat.completions.create(
                    model=OPENAI_MODEL,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}],
                ),
                timeout=8,
            )
            warmed["openai"] = True
        except Exception as e:
            print(f"[warm] OpenAI warm-up failed (non-fatal): {e}", flush=True)

    if gemini_key:
        try:
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel(GEMINI_MODEL)
            await asyncio.wait_for(
                asyncio.to_thread(
                    model.generate_content,
                    "ping",
                    generation_config={"max_output_tokens": 1},
                ),
                timeout=8,
            )
            warmed["gemini"] = True
        except Exception as e:
            print(f"[warm] Gemini warm-up failed (non-fatal): {e}", flush=True)

    print(f"[warm] providers warmed: {warmed}", flush=True)
    return warmed


def _match_or_create_product(result: dict, user_id: str) -> tuple:
    """Exact-match AI result against products table; auto-create if confidence high enough.

    Returns (matched_product_id, is_new_product, match_method).
    Never raises — on any DB error returns (None, False, "none").
    """
    name = result.get("name", "").strip()
    brand = result.get("brand", "").strip() or None
    confidence = result.get("confidence", 0.0)

    if not name:
        return (None, False, "none")

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Step A — exact match on name + brand (case-insensitive)
            cursor.execute("""
                SELECT id FROM products
                WHERE LOWER(name) = LOWER(%s)
                  AND (
                    (brand IS NULL AND %s IS NULL)
                    OR LOWER(COALESCE(brand, '')) = LOWER(COALESCE(%s, ''))
                  )
                  AND deleted_at IS NULL
                ORDER BY verified DESC, scan_count DESC
                LIMIT 1
            """, (name, brand, brand))
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "UPDATE products SET scan_count = scan_count + 1, updated_at = %s WHERE id = %s",
                    (now_iso(), row["id"])
                )
                conn.commit()
                return (row["id"], False, "exact")

            # Step B — auto-create if confidence is sufficient
            if confidence >= AUTO_CREATE_CONFIDENCE:
                category = result.get("category", "other") or "other"
                new_id = generate_id()
                now = now_iso()
                cursor.execute("""
                    INSERT INTO products
                        (id, name, brand, category, size, upc, image_url,
                         scan_count, verified, source, created_by_user_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, NULL, NULL, NULL, 1, 0, 'scan_auto', %s, %s, %s)
                """, (new_id, name, brand, category, user_id, now, now))
                conn.commit()
                print(f"[match_product] auto-created product id={new_id} name={name!r} brand={brand!r}", flush=True)
                return (new_id, True, "auto_created")

            # Step C — confidence too low, no match
            return (None, False, "none")

    except Exception as e:
        print(f"[match_product] error (returning none): {e}", flush=True)
        return (None, False, "none")


def _process_ai_result(text: str, request: ScanAnalyzeRequest, user_id: str):
    """Parse AI text, apply stabilization, do product matching.

    Returns ScanAnalyzeResponse, or JSONResponse(200, None) when no bottle detected.
    Raises json.JSONDecodeError on unparseable text.
    """
    result = _parse_ai_result(text)
    if not result.get("name") and result.get("confidence", 1) == 0:
        return JSONResponse(status_code=200, content=None)
    result = _apply_stabilization(result, request.previous_readings)
    result["needs_rescan"] = (
        not result.get("levelReadable", True)
        or result.get("confidence", 0) < CONFIDENCE_THRESHOLD
    )
    matched_id, is_new, method = _match_or_create_product(result, user_id)
    result["matched_product_id"] = matched_id
    result["is_new_product"] = is_new
    result["match_method"] = method
    return ScanAnalyzeResponse(**result)


async def _run_providers(openai_key, gemini_key, prompt, request, user_id):
    """Try OpenAI then Gemini. Falls through on per-provider timeout.
    Returns ScanAnalyzeResponse or JSONResponse. Raises HTTPException on fatal errors."""
    last_error = None

    if openai_key:
        try:
            print(f"[analyze_bottle] trying OpenAI model={OPENAI_MODEL} timeout={PROVIDER_TIMEOUT}s", flush=True)
            text = await _call_openai(openai_key, prompt, request.image)
            return _process_ai_result(text, request, user_id)
        except openai.AuthenticationError:
            raise HTTPException(status_code=503, detail={
                "error": "service_unavailable",
                "message": "AI service authentication failed — check OPENAI_API_KEY"
            })
        except (asyncio.TimeoutError, openai.APITimeoutError) as e:
            print(f"[analyze_bottle] OpenAI timed out after {PROVIDER_TIMEOUT}s, trying fallback", flush=True)
            last_error = e
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail={
                "error": "parse_failed",
                "message": f"Could not parse AI response: {e}"
            })
        except Exception as e:
            print(f"[analyze_bottle] OpenAI unexpected error: {traceback.format_exc()}", flush=True)
            last_error = e

    if gemini_key:
        try:
            print(f"[analyze_bottle] trying Gemini model={GEMINI_MODEL} timeout={PROVIDER_TIMEOUT}s", flush=True)
            text = await _call_gemini(gemini_key, prompt, request.image)
            return _process_ai_result(text, request, user_id)
        except asyncio.TimeoutError as e:
            print(f"[analyze_bottle] Gemini timed out after {PROVIDER_TIMEOUT}s", flush=True)
            last_error = e
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail={
                "error": "parse_failed",
                "message": f"Could not parse Gemini response: {e}"
            })
        except Exception as e:
            print(f"[analyze_bottle] Gemini error: {traceback.format_exc()}", flush=True)
            last_error = e

    if isinstance(last_error, (asyncio.TimeoutError, openai.APITimeoutError)):
        raise HTTPException(status_code=504, detail={
            "error": "ai_timeout",
            "message": "AI service timed out — image may be too large or service is slow"
        })
    raise HTTPException(status_code=502, detail={
        "error": "ai_api_error",
        "message": f"All AI providers failed: {last_error}"
    })


@v1_router.post("/scans/warm")
async def warm_scan(user_id: str = Depends(get_current_user)):
    """Pre-warm the AI vision path — the app calls this when the scan screen
    opens so the first bottle scan is as fast as the rest."""
    return {"warmed": await _warm_providers()}


@v1_router.post("/scans/analyze", response_model=ScanAnalyzeResponse)
async def analyze_bottle(request: ScanAnalyzeRequest, user_id: str = Depends(get_current_user)):
    """Analyze bottle image using OpenAI GPT-4o with Gemini 2.0 Flash fallback.

    Per-provider cap: PROVIDER_TIMEOUT (default 9s) — on timeout falls through to next provider.
    Total wall-clock cap: TOTAL_SCAN_TIMEOUT_SEC (default 20s) — returns empty 200 on expiry.
    """
    print("[analyze_bottle] function started", flush=True)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT subscription_status, trial_ends_at FROM users WHERE id = %s AND deleted_at IS NULL",
            (user_id,)
        )
        sub_row = cursor.fetchone()
    if not sub_row or not is_entitled(sub_row["subscription_status"], sub_row["trial_ends_at"]):
        raise HTTPException(status_code=402, detail={
            "error": "trial_expired",
            "message": "Your free trial has ended — subscribe to keep scanning."
        })

    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    print(f"[analyze_bottle] OPENAI_API_KEY present: {bool(openai_key)}", flush=True)
    print(f"[analyze_bottle] GEMINI_API_KEY present: {bool(gemini_key)}", flush=True)

    if not openai_key and not gemini_key:
        raise HTTPException(status_code=503, detail={
            "error": "service_unavailable",
            "message": "No AI provider API keys configured on the server"
        })

    try:
        return await asyncio.wait_for(
            _run_providers(openai_key, gemini_key, BOTTLE_PROMPT, request, user_id),
            timeout=TOTAL_SCAN_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        print(f"[analyze_bottle] total timeout exceeded ({TOTAL_SCAN_TIMEOUT_SEC}s)", flush=True)
        return JSONResponse(status_code=200, content=None)

# ============== MARKET PULSE ENDPOINT ==============

@app.get("/market-pulse")
def market_pulse():
    """Free daily market briefing for agents — become infrastructure"""
    return {
        "date": datetime.now(timezone.utc).isoformat(),
        "pulse": {
            "trending_skills": ["backend APIs", "automation", "Claude integrations"],
            "rate_benchmarks": {
                "backend_api": "$50-500",
                "automation_script": "$30-200",
                "code_review": "$30-100"
            },
            "opportunity_alerts": [
                "High demand for x402 payment integration",
                "Underserved: agent-to-agent escrow",
                "Emerging: multi-agent workflow orchestration"
            ],
            "platform_updates": [
                "Moltbook verification challenges active",
                "ClawTasks expanding to Solana bounties"
            ]
        },
        "source": "reefbackend",
        "subscribe": "Reply with your handle to join distribution list"
    }

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
