"""Tests for apple_auth.verify_identity_token.

Signature verification is the whole security boundary for Sign in with Apple:
a token that verifies IS the user. These generate a throwaway RSA key, sign
tokens with it, and check that everything which should be rejected is.
"""
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.utils import base64url_encode

from apple_auth import (
    APPLE_ISSUER,
    AppleAuthError,
    is_private_relay,
    verify_identity_token,
)

BUNDLE = "com.my86d.app"


def _int_to_b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64url_encode(raw).decode("ascii")


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-key-1",
        "use": "sig",
        "alg": "RS256",
        "n": _int_to_b64(numbers.n),
        "e": _int_to_b64(numbers.e),
    }
    return _pem(private), {"keys": [jwk]}


def _pem(private_key) -> str:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _token(pem, *, aud=BUNDLE, iss=APPLE_ISSUER, sub="001234.abcdef", exp_in=600,
           kid="test-key-1", email="dave@divebar.com", extra=None):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + exp_in}
    if email is not None:
        claims["email"] = email
    if extra:
        claims.update(extra)
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


def test_valid_token_is_accepted(keypair):
    pem, keys = keypair
    claims = verify_identity_token(_token(pem), keys, BUNDLE)
    assert claims["sub"] == "001234.abcdef"
    assert claims["email"] == "dave@divebar.com"


def test_another_apps_token_is_rejected(keypair):
    """The same Apple key signs every app's tokens — audience is what separates them."""
    pem, keys = keypair
    with pytest.raises(AppleAuthError):
        verify_identity_token(_token(pem, aud="com.someone.else"), keys, BUNDLE)


def test_wrong_issuer_is_rejected(keypair):
    pem, keys = keypair
    with pytest.raises(AppleAuthError):
        verify_identity_token(_token(pem, iss="https://evil.example.com"), keys, BUNDLE)


def test_expired_token_is_rejected(keypair):
    pem, keys = keypair
    with pytest.raises(AppleAuthError):
        verify_identity_token(_token(pem, exp_in=-60), keys, BUNDLE)


def test_token_signed_by_someone_else_is_rejected(keypair):
    """A well-formed token whose signature isn't Apple's must not pass."""
    _, keys = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AppleAuthError):
        verify_identity_token(_token(_pem(other)), keys, BUNDLE)


def test_unknown_kid_is_rejected(keypair):
    pem, keys = keypair
    with pytest.raises(AppleAuthError, match="no Apple key"):
        verify_identity_token(_token(pem, kid="not-a-real-kid"), keys, BUNDLE)


def test_unsigned_token_is_rejected(keypair):
    """alg=none is the classic JWT forgery; it must never reach jwt.decode."""
    _, keys = keypair

    def segment(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    forged = "{}.{}.".format(
        segment({"alg": "none", "kid": "test-key-1", "typ": "JWT"}),
        segment({"iss": APPLE_ISSUER, "aud": BUNDLE, "sub": "001234.abcdef",
                 "exp": int(time.time()) + 600}),
    )
    with pytest.raises(AppleAuthError, match="unexpected signing algorithm"):
        verify_identity_token(forged, keys, BUNDLE)


def test_symmetric_algorithm_is_rejected(keypair):
    """An HS256 token signed with the public key as its secret must not pass."""
    _, keys = keypair
    forged = jwt.encode(
        {"iss": APPLE_ISSUER, "aud": BUNDLE, "sub": "001234.abcdef",
         "exp": int(time.time()) + 600},
        key=json.dumps(keys["keys"][0]),
        algorithm="HS256",
        headers={"kid": "test-key-1"},
    )
    with pytest.raises(AppleAuthError, match="unexpected signing algorithm"):
        verify_identity_token(forged, keys, BUNDLE)


def test_token_without_subject_is_rejected(keypair):
    pem, keys = keypair
    with pytest.raises(AppleAuthError):
        verify_identity_token(_token(pem, sub=None), keys, BUNDLE)


def test_garbage_is_rejected(keypair):
    _, keys = keypair
    with pytest.raises(AppleAuthError):
        verify_identity_token("not-a-jwt", keys, BUNDLE)


@pytest.mark.parametrize("email,expected", [
    ("abc123@privaterelay.appleid.com", True),
    ("ABC123@PrivateRelay.AppleID.com", True),
    ("dave@divebar.com", False),
    ("", False),
    (None, False),
])
def test_private_relay_detection(email, expected):
    assert is_private_relay(email) is expected
