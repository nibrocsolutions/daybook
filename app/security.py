"""Passwords, login throttling, and request guards."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time

from app.config import WEAK_PASSWORDS

PBKDF_ROUNDS = 200_000
_LOCK = threading.Lock()
_ATTEMPTS: dict[str, list[float]] = {}
_WINDOW_SECONDS = 15 * 60
_MAX_ATTEMPTS = 8
_DUMMY_HASH: str | None = None

_NEXT = re.compile(r"^/([A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)\Z")


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 10 <= len(password) <= 200:
        raise ValueError("Password length is out of range.")
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF_ROUNDS)
    return f"pbkdf2_sha256${PBKDF_ROUNDS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        rounds_i = int(rounds)
        if rounds_i < 1 or rounds_i > 1_000_000:
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            rounds_i,
        )
        return hmac.compare_digest(derived.hex(), digest_hex)
    except Exception:
        return False


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("not-a-real-password")
    return _DUMMY_HASH


def password_matches(password: str, stored: str | None) -> bool:
    if not isinstance(password, str) or not 1 <= len(password) <= 200 or not stored:
        verify_password("wrong-password", _dummy_hash())
        return False
    return verify_password(password, stored)


def password_problem(password: str) -> str | None:
    if len(password) < 10 or len(password) > 200:
        return "Use a password between 10 and 200 characters."
    return None


def weak_password(password: str) -> bool:
    return password in WEAK_PASSWORDS


def clear_login_attempts() -> None:
    with _LOCK:
        _ATTEMPTS.clear()


def login_blocked(email: str) -> bool:
    key = email.strip().lower()
    now = time.time()
    with _LOCK:
        recent = [stamp for stamp in _ATTEMPTS.get(key, []) if now - stamp < _WINDOW_SECONDS]
        if recent:
            _ATTEMPTS[key] = recent
        else:
            _ATTEMPTS.pop(key, None)
        return len(recent) >= _MAX_ATTEMPTS


def record_login_failure(email: str) -> None:
    key = email.strip().lower()
    now = time.time()
    with _LOCK:
        recent = [stamp for stamp in _ATTEMPTS.get(key, []) if now - stamp < _WINDOW_SECONDS]
        recent.append(now)
        _ATTEMPTS[key] = recent


def clear_login_failure(email: str) -> None:
    key = email.strip().lower()
    with _LOCK:
        _ATTEMPTS.pop(key, None)


def tokens_match(expected: str, provided: str) -> bool:
    if not expected or not provided:
        return False
    try:
        return hmac.compare_digest(expected, provided)
    except Exception:
        return False


def ensure_csrf(request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str) or len(token) < 20:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def assert_csrf(request, provided: str) -> None:
    from app.errors import FormExpired

    expected = request.session.get("csrf_token") or ""
    if not tokens_match(str(expected), str(provided or "")):
        raise FormExpired("Refresh the page and try again.")


def safe_next(value: str | None) -> str:
    if not value:
        return "/"
    candidate = value.strip()
    if not _NEXT.match(candidate):
        return "/"
    if candidate.startswith("//") or candidate.startswith("/\\") or "\\" in candidate:
        return "/"
    if "://" in candidate or ".." in candidate:
        return "/"
    if candidate.startswith("/login") or candidate.startswith("/webhooks"):
        return "/"
    return candidate
