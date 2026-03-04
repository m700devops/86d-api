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

# ============== HEALTH & INFO (ROOT LEVEL) ==============

@app.get("/", response_model=APIInfoResponse)
def root():
    """API info and endpoints list"""
    return {
        "name": "86'd API",
        "version": "1.0.0",
        "status": "healthy",
        "docs": "/docs",
        "endpoints": {
            "auth": ["/v1/auth/register", "/v1/auth/login", "/v1/auth/refresh"],
            "products": ["/v1/products", "/v1/products/search", "/v1/products/barcode/{upc}"],
            "locations": ["/v1/locations", "/v1/locations/{id}/par-levels"],
            "inventory": ["/v1/inventory/start", "/v1/inventory/{id}", "/v1/inventory/{id}/scan"],
            "orders": ["/v1/orders", "/v1/orders/{id}"],
            "distributors": ["/v1/distributors"],
            "sync": ["/v1/sync"]
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

# ============== V1 ROUTER ==============

v1_router = APIRouter(prefix="/v1")

# ============== V1 AUTHENTICATION ==============

@v1_router.post("/auth/register", response_model=TokenResponse, status_code=201)
def register(user_data: UserCreate):
    """Create new user account"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check if terms were accepted
        if not user_data.terms_accepted:
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

@v1_router.post("/auth/forgot-password")
def forgot_password(request: ForgotPasswordRequest):
    """Request password reset (returns success even if email doesn't exist)"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT id FROM users WHERE email = ? AND deleted_at IS NULL",
            (request.email.lower().strip(),)
        )
        row = cursor.fetchone()
        
        if row:
            # Generate reset token
            reset_token = create_password_reset_token(row["id"])
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            
            cursor.execute("""
                UPDATE users 
                SET password_reset_token = ?, password_reset_expires_at = ?
                WHERE id = ?
            """, (reset_token, expires_at, row["id"]))
            conn.commit()
            
            # In production, send email here
            # For now, return token in response (development only)
            return {
                "success": True,
                "message": "If an account exists, a reset link has been sent",
                "debug_token": reset_token  # Remove in production
            }
        
        return {
            "success": True,
            "message": "If an account exists, a reset link has been sent"
        }

@v1_router.post("/auth/reset-password")
def reset_password(request: ResetPasswordRequest):
    """Reset password using token"""
    user_id = verify_password_reset_token(request.token)
    if not user_id:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_token",
            "message": "Invalid or expired reset token"
        })
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Verify token matches stored token
        cursor.execute(
            "SELECT id FROM users WHERE id = ? AND password_reset_token = ? AND password_reset_expires_at > ?",
            (user_id, request.token, now_iso())
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=400, detail={
                "error": "invalid_token",
                "message": "Invalid or expired reset token"
            })
        
        # Update password
        password_hash = get_password_hash(request.new_password)
        cursor.execute("""
            UPDATE users 
            SET password_hash = ?, password_reset_token = NULL, password_reset_expires_at = NULL, updated_at = ?
            WHERE id = ?
        """, (password_hash, now_iso(), user_id))
        conn.commit()
        
        return {"success": True, "message": "Password reset successfully"}

@v1_router.put("/auth/change-password")
def change_password(request: ChangePasswordRequest, user_id: str = Depends(get_current_user)):
    """Change password (requires current password)"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT password_hash FROM users WHERE id = ? AND deleted_at IS NULL",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        
        if not verify_password(request.current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail={
                "error": "invalid_password",
                "message": "Current password is incorrect"
            })
        
        password_hash = get_password_hash(request.new_password)
        cursor.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now_iso(), user_id)
        )
        conn.commit()
        
        return {"success": True, "message": "Password changed successfully"}

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
        
        cursor.execute(
            "SELECT password_hash FROM users WHERE id = ? AND deleted_at IS NULL",
            (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "User not found"})
        
        if not verify_password(password, row["password_hash"]):
            raise HTTPException(status_code=401, detail={
                "error": "invalid_password",
                "message": "Password is incorrect"
            })
        
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
