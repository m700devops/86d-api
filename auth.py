from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

# Security configuration
import os
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7   # 7 days (was 60 min — short lifetime caused re-login loops)
REFRESH_TOKEN_EXPIRE_DAYS = 30              # 30 days (was 7)
PASSWORD_RESET_EXPIRE_MINUTES = 30          # emailed 6-digit code — short-lived, see /auth/forgot-password
PASSWORD_RESET_RESEND_COOLDOWN_SECONDS = 60 # don't re-send/regenerate on rapid repeat requests

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class TokenData(BaseModel):
    user_id: Optional[str] = None
    token_type: str = "access"

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Generate password hash"""
    return pwd_context.hash(password)

def create_access_token(user_id: str, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token"""
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = {
        "sub": user_id,
        "exp": expire,
        "type": "access",
        "iat": datetime.now(timezone.utc)
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    """Create JWT refresh token"""
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode = {
        "sub": user_id,
        "exp": expire,
        "type": "refresh",
        "iat": datetime.now(timezone.utc)
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str, token_type: str = "access") -> Optional[str]:
    """Verify JWT token and return user_id if valid"""
    claims = get_token_claims(token, token_type)
    return claims.get("sub") if claims else None


def get_token_claims(token: str, token_type: str = "access") -> Optional[dict]:
    """Verify a JWT and return its full claims (sub, iat, ...) — for callers
    that need the issued-at time (password-change revocation)."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") is None:
            return None
        if payload.get("type", "access") != token_type:
            return None
        return payload
    except JWTError:
        return None


