"""Password hashing, tokens, and security headers."""

from __future__ import annotations

import hmac
import secrets as _secrets

import bcrypt

SESSION_COOKIE = "pgbench_session"
CSRF_FIELD = "csrf_token"

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    # Self-contained UI: only inline + same-origin assets, no external network.
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self'; "
        "base-uri 'none'; form-action 'self'"
    ),
}


def hash_password(password: str) -> str:
    """argon2/bcrypt-class hash (bcrypt). Never store plaintext."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, pw_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), pw_hash.encode())
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return _secrets.token_urlsafe(32)


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
