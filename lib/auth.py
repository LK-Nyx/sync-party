"""Authentication — HMAC-signed tokens, cookie helpers, sliding-window rate limiter."""

import hashlib
import hmac
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse

from lib.config import SERVER_SECRET, ROOM_TTL, RATE_WINDOW, RATE_MAX_LOGIN


# ── Rate limiter ───────────────────────────────────────────────
_rate: dict[str, list[float]] = {}


def ratelimit(key: str, max_req: int = RATE_MAX_LOGIN) -> bool:
    """Sliding-window rate limiter. Returns True if the request is allowed."""
    now = time.time()
    bucket = _rate.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < RATE_WINDOW]
    ok = len(bucket) < max_req
    if ok:
        bucket.append(now)
    return ok


# ── Auth helpers ───────────────────────────────────────────────
def _make_payload(slug: str, role: str) -> str:
    return f"{slug}:{role}:{int(time.time())}"


def _sign_payload(payload: str) -> str:
    sig = hmac.new(SERVER_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"


def sign(slug: str, role: str) -> str:
    """Create an HMAC-signed token for the given *slug* and *role*."""
    return _sign_payload(_make_payload(slug, role))


def verify(token: str, slug: str, role: str = "admin") -> bool:
    """Verify an auth token matches *slug* and *role* (use role='any' to skip role check)."""
    try:
        parts = token.rsplit(":", 1)
        expected = hmac.new(SERVER_SECRET.encode(), parts[0].encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(parts[1], expected):
            return False
        t_slug, t_role, _ = parts[0].split(":")
        return t_slug == slug and (role == "any" or t_role == role)
    except (ValueError, IndexError):
        return False


def verify_superadmin(token: str) -> bool:
    """Verify an auth token is a valid superadmin token."""
    try:
        parts = token.rsplit(":", 1)
        expected = hmac.new(SERVER_SECRET.encode(), parts[0].encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(parts[1], expected):
            return False
        _, t_role, _ = parts[0].split(":")
        return t_role == "superadmin"
    except (ValueError, IndexError):
        return False


def sign_superadmin() -> str:
    """Create a superadmin auth token (globally scoped)."""
    return _sign_payload(_make_payload("global", "superadmin"))


# ── Cookie helpers ─────────────────────────────────────────────
def is_secure(request: Optional[Request] = None) -> bool:
    """Return True if the request came over HTTPS (via X-Forwarded-Proto)."""
    if request is None:
        return False
    return request.headers.get("X-Forwarded-Proto", "") == "https"


def set_auth_cookie(resp: RedirectResponse, name: str, token: str,
                    request: Optional[Request] = None, rid: str = "?",
                    logger=None) -> None:
    """Set an httponly SameSite=Lax auth cookie on *resp*, scoped to ROOM_TTL."""
    secure = is_secure(request)
    resp.set_cookie(name, token, httponly=True, samesite="lax",
                    max_age=ROOM_TTL, secure=secure)
    if logger:
        logger.debug("cookie_set", extra={"rid": rid, "slug": name[:20],
                                           "secure": str(secure)})
