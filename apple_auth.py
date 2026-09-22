"""Verification of Sign in with Apple identity tokens.

Kept apart from main.py for the same reason phones.py and callwindow.py are:
this is pure logic over (token, keys, clock) and can be tested without a
database, a network or a real Apple account — which matters more here than
most places, because the failure mode of getting it wrong is that anyone can
mint a token and become any user.

Apple needs no API key for this. The identity token is a standard RS256 JWT
signed by Apple; the public keys are published at a well-known URL and the
audience is our own bundle id. There is no secret to configure and so no
env var that can be left unset in production and quietly disable the check.
"""
import json
import threading
import time
from typing import Optional

import httpx
from jose import jwt, JWTError

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

# Apple's audience is the app's bundle id. Overridable so a second bundle
# (a TestFlight-only build, say) doesn't need a code change, but it has a
# working default on purpose: an unset env var here must never widen what
# is accepted.
DEFAULT_BUNDLE_ID = "com.my86d.app"

# Apple rotates these keys. Cache them, but not forever — a rotation while we
# hold a stale set would reject every sign-in until a redeploy.
_JWKS_TTL_SECONDS = 60 * 60
_jwks_cache: Optional[dict] = None
_jwks_fetched_at: float = 0.0
_jwks_lock = threading.Lock()


class AppleAuthError(Exception):
    """The token could not be trusted. The message is safe to log, not to show."""


def fetch_apple_keys(force: bool = False) -> dict:
    """Return Apple's JWKS, cached for an hour.

    Raises AppleAuthError if Apple can't be reached and nothing is cached —
    better an explicit failure the client can retry than a sign-in that
    silently skips signature verification.
    """
    global _jwks_cache, _jwks_fetched_at
    with _jwks_lock:
        fresh = _jwks_cache is not None and (time.time() - _jwks_fetched_at) < _JWKS_TTL_SECONDS
        if fresh and not force:
            return _jwks_cache
        try:
            response = httpx.get(APPLE_JWKS_URL, timeout=10.0)
            response.raise_for_status()
            keys = response.json()
        except Exception as e:
            if _jwks_cache is not None:
                # Stale keys still verify tokens signed before the rotation,
                # so a blip at Apple's end shouldn't lock everyone out.
                print(f"[apple] key fetch failed, using cached keys: {e}", flush=True)
                return _jwks_cache
            raise AppleAuthError(f"could not fetch Apple's signing keys: {e}")
        _jwks_cache = keys
        _jwks_fetched_at = time.time()
        return keys


def _key_for(token: str, keys: dict) -> dict:
    """Find the JWK matching the token's `kid` header."""
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as e:
        raise AppleAuthError(f"malformed token header: {e}")

    kid = header.get("kid")
    if not kid:
        raise AppleAuthError("token header carries no kid")
    # Only RS256 is accepted. Taking the algorithm from the header without
    # constraining it is how a token signed with "none", or an HS256 token
    # signed with the public key as its secret, gets accepted as genuine.
    if header.get("alg") != "RS256":
        raise AppleAuthError(f"unexpected signing algorithm {header.get('alg')!r}")

    for key in keys.get("keys", []):
        if key.get("kid") == kid:
            return key
    raise AppleAuthError(f"no Apple key matches kid {kid!r}")


def verify_identity_token(token: str, keys: dict, bundle_id: str) -> dict:
    """Verify an Apple identity token and return its claims.

    Checks the signature against Apple's published key, that the audience is
    our bundle id, that Apple issued it, and that it hasn't expired. Anything
    short of all four raises.
    """
    key = _key_for(token, keys)
    try:
        claims = jwt.decode(
            token,
            json.dumps(key),
            algorithms=["RS256"],
            audience=bundle_id,
            issuer=APPLE_ISSUER,
        )
    except JWTError as e:
        raise AppleAuthError(f"token rejected: {e}")

    subject = claims.get("sub")
    if not subject:
        raise AppleAuthError("token carries no subject")
    return claims


def is_private_relay(email: Optional[str]) -> bool:
    """True for an address Apple's Hide My Email is forwarding.

    Worth knowing because it's an address the venue never published: CRM
    attribution can't match a lead on it, and mail sent to it is forwarded
    rather than delivered.
    """
    return bool(email) and email.lower().strip().endswith("@privaterelay.appleid.com")
