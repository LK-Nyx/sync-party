#!/usr/bin/env python3
"""Sync Party v3.2 — structured logging, secure auth, full sync.

A real-time watch-party server built on FastAPI + WebSocket.

Architecture
~~~~~~~~~~~~~
- Rooms: In-memory dict keyed by slug. Each Room holds playlist state,
  a WebSocket connection for the admin, and a list of viewer dicts.
- Authentication: HMAC-signed tokens stored in httponly SameSite=Lax
  cookies. Three roles — admin (cookie-signed), moderator (password),
  viewer (no auth needed).
- Slug modes: ``hex8`` (random 8-char hex), ``name4`` (normalised name
  + 4-hex suffix, default), ``name`` (normalised name only, errors on
  collision).
- WebSocket protocol: First message must be an auth payload; subsequent
  messages are JSON with a ``type`` field dispatched by role-specific
  handlers (``_admin_msg``, ``_viewer_msg``).
- Providers: Pluggable media providers (YouTube, Spotify) discovered via
  ``providers.base.list_all``.
- Logging: Structured key=value format via StructuredFormatter with a
  ring-buffer handler available at ``/admin/logs``.
"""

import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Optional

import qrcode
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader
from starlette.middleware.base import BaseHTTPMiddleware

from providers.base import list_all as list_providers, get as get_provider
import providers.youtube
import providers.spotify

# ── Structured logging ─────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

class StructuredFormatter(logging.Formatter):
    """Emit structured log lines: ts=ISO8601 level=LVL rid=ID key=value msg=..."""

    def format(self, record: logging.LogRecord) -> str:
        now = datetime.datetime.utcnow()
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        fields = [f"ts={ts}", f"level={record.levelname}"]
        for attr in ("rid", "slug", "role", "ip", "method", "path", "status", "ms"):
            val = getattr(record, attr, None)
            if val is not None:
                fields.append(f"{attr}={val}")
        fields.append(f"msg={record.getMessage()}")
        return " ".join(fields)

logger = logging.getLogger("sync-party")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(StructuredFormatter())
logger.handlers = [handler]

# NOTE: Never use reserved LogRecord attribute names as keys in extra={}.
# Reserved names include: msg, name, args, created, relativeCreated,
# exc_info, exc_text, stack_info, lineno, funcName, pathname, thread,
# threadName, process, processName, levelname, levelno, message, msecs,
# taskName. Using any of these would silently override internal LogRecord
# fields and cause subtle bugs.
#
# Currently used extra keys: rid, slug, role, ip, method, path, status,
# ms, room_name, action, count, reason, dead, total, secure, has_token,
# slug_mode, viewers, url, target, by — all safe.

# In-memory ring buffer for the last N log messages (accessible via /admin/logs)
_LOG_RING_SIZE = int(os.environ.get("LOG_RING_SIZE", "500"))
_log_ring: list[str] = []

class RingBufferHandler(logging.Handler):
    """Captures formatted log records into a fixed-size ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        _log_ring.append(self.format(record))
        if len(_log_ring) > _LOG_RING_SIZE:
            _log_ring.pop(0)

ring_handler = RingBufferHandler()
ring_handler.setFormatter(StructuredFormatter())
logger.addHandler(ring_handler)

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Inject a short request-ID (X-Request-ID header or random) and log every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
        request.state.rid = rid
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            try:
                logger.warning(
                    f"req method={request.method} path={request.url.path} status=500 ms={elapsed_ms} err={e}",
                    extra={
                        "rid": rid, "ip": request.client.host if request.client else "-",
                        "method": request.method, "path": request.url.path,
                        "status": 500, "ms": elapsed_ms,
                        "slug": request.path_params.get("slug", "-"),
                    },
                )
            except Exception:
                pass
            raise
        elapsed_ms = int((time.monotonic() - start) * 1000)
        try:
            logger.info(
                f"req method={request.method} path={request.url.path} status={response.status_code} ms={elapsed_ms}",
                extra={
                    "rid": rid, "ip": request.client.host if request.client else "-",
                    "method": request.method, "path": request.url.path,
                    "status": response.status_code, "ms": elapsed_ms,
                    "slug": request.path_params.get("slug", "-"),
                },
            )
        except Exception:
            pass
        return response

# ── Slug generation ──────────────────────────────────────────────
SLUG_MODES = ("hex8", "name4", "name")

def _normalize_name(name: str) -> str:
    """Slugify a room name: accents→ascii, lowercase, spaces→hyphens,
       remove non-alphanumeric (except hyphens), collapse hyphens, strip edges."""
    # Transliterate stubborn Latin chars that NFD/NFKD won't decompose
    _LATIN_MAP = str.maketrans({
        'Ł': 'L', 'ł': 'l', 'Đ': 'D', 'đ': 'd',
        'Ø': 'O', 'ø': 'o', 'Æ': 'AE', 'æ': 'ae',
        'Ð': 'D', 'ð': 'd', 'Þ': 'Th', 'þ': 'th',
        'ß': 'ss',
    })
    name = name.translate(_LATIN_MAP)
    # NFKD decomposes most accented chars (é → e + combining acute)
    nfkd = unicodedata.normalize("NFKD", name)
    # Remove combining marks (diacritics)
    no_combining = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    # Drop anything still non-ASCII
    ascii_only = no_combining.encode("ascii", "ignore").decode("ascii")
    lower = ascii_only.lower()
    # Replace any non-alphanumeric (except hyphens/spaces) with space
    cleaned = re.sub(r"[^a-z0-9\s-]", "", lower)
    # Spaces and underscores → hyphens
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    # Collapse multiple hyphens
    cleaned = re.sub(r"-+", "-", cleaned)
    # Strip leading/trailing hyphens
    cleaned = cleaned.strip("-")
    return cleaned or "room"

def _generate_slug(name: str, mode: str = "name4") -> str:
    """Generate a slug based on the chosen mode.
       hex8  → 8-char random hex (classic)
       name4 → normalized name + 4-char hex suffix (default)
       name  → normalized name only (collision = error)
    """
    if mode == "hex8":
        return secrets.token_hex(4)
    normalized = _normalize_name(name)
    if mode == "name":
        return normalized
    # name4 (default)
    suffix = secrets.token_hex(2)
    return f"{normalized}-{suffix}"

# ── Config ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
SERVER_SECRET = secrets.token_hex(32)
SUPER_ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PWD", "changeme_superadmin")
ROOM_TTL = int(os.environ.get("ROOM_TTL", "7200"))
MAX_ROOMS = int(os.environ.get("MAX_ROOMS", "50"))
RATE_WINDOW = 60
RATE_MAX_CREATE = 5
RATE_MAX_LOGIN = 15

logger.info(f"startup secret={'set' if SERVER_SECRET else 'unset'} ttl={ROOM_TTL}s max_rooms={MAX_ROOMS}", extra={"rid": "boot"})

# ── Cookie helper ──────────────────────────────────────────────
def _is_secure(request: Optional[Request] = None) -> bool:
    """Return True if the request came over HTTPS (via X-Forwarded-Proto)."""
    if request is None:
        return False
    return request.headers.get("X-Forwarded-Proto", "") == "https"

def _set_auth_cookie(resp: RedirectResponse, name: str, token: str, request: Optional[Request] = None) -> None:
    """Set an httponly SameSite=Lax auth cookie on *resp*, scoped to the room TTL."""
    secure = _is_secure(request)
    resp.set_cookie(name, token, httponly=True, samesite="lax", max_age=ROOM_TTL, secure=secure)
    logger.debug("cookie_set", extra={"rid": getattr(request, "state", None) and request.state.rid or "?", "slug": name[:20], "secure": str(secure)})

# ── App ────────────────────────────────────────────────────────
app = FastAPI(title="Sync Party")
app.add_middleware(RequestIDMiddleware)
_jinja = Environment(loader=FileSystemLoader(BASE_DIR / "templates"))

# ── Rate limiter ───────────────────────────────────────────────
_rate: dict[str, list[float]] = {}

def _ratelimit(key: str, max_req: int = RATE_MAX_LOGIN) -> bool:
    """Sliding-window rate limiter. Returns True if the request is allowed."""
    now = time.time()
    bucket = _rate.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < RATE_WINDOW]
    ok = len(bucket) < max_req
    if ok:
        bucket.append(now)
    return ok

# ── Auth ───────────────────────────────────────────────────────
def _sign(slug: str, role: str) -> str:
    """Create an HMAC-signed token for the given *slug* and *role*."""
    payload = f"{slug}:{role}:{int(time.time())}"
    sig = hmac.new(SERVER_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"

def _verify(token: str, slug: str, role: str = "admin") -> bool:
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

def _verify_superadmin(token: str) -> bool:
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

def _sign_superadmin() -> str:
    """Create a superadmin auth token (globally scoped)."""
    payload = f"global:superadmin:{int(time.time())}"
    sig = hmac.new(SERVER_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"

# ── Room store ─────────────────────────────────────────────────
rooms: dict[str, "Room"] = {}

class Room:
    """In-memory model of a watch-party room.

    Attributes:
        slug: Unique URL-safe identifier.
        name: Human-readable display name.
        admin_password: Shared password for admin login.
        state: Player state code (-1=ended, 0=queued/stopped, 1=playing, 2=paused).
    """

    def __init__(self, slug: str, name: str, admin_password: str) -> None:
        self.slug = slug
        self.name = name
        self.admin_password = admin_password
        self.created_at = time.time()
        self.last_activity = time.time()
        self.playlist_url = ""
        self.video_id = ""
        self.video_title = ""
        self.state = -1
        self.current_time = 0.0
        self.global_mode = "resume"
        self.provider = "youtube"
        self.moderator_password = ""
        self.guest_dj_enabled = False
        self.admin_ws: Optional[WebSocket] = None
        self.viewer_ws: list[dict] = []

    def touch(self) -> None:
        """Update last_activity timestamp to prevent TTL eviction."""
        self.last_activity = time.time()

    def expired(self) -> bool:
        """Return True if the room has exceeded the configured TTL."""
        return (time.time() - self.last_activity) > ROOM_TTL

    def player_state(self) -> dict:
        """Return a dict describing current playback state.

        State codes: -1=ended, 0=queued/stopped, 1=playing, 2=paused.
        """
        return {
            "type": "player_state", "video_id": self.video_id, "video_title": self.video_title,
            "state": self.state, "current_time": self.current_time,
            "global_mode": self.global_mode, "playlist_url": self.playlist_url, "provider": self.provider,
        }

    def viewer_list(self) -> list[dict]:
        """Return a serialisable list of viewer metadata (no WebSocket refs)."""
        return [{"name": v["name"], "mode": v["mode"], "muted": v["muted"], "is_dj": v["is_dj"], "role": v["role"]} for v in self.viewer_ws]

def _cleanup() -> None:
    """Remove all expired rooms from the store."""
    expired = [(s, r.name) for s, r in rooms.items() if r.expired()]
    for s, _ in expired:
        del rooms[s]
    if expired:
        logger.info("cleanup", extra={"rid": "sched", "slug": "-", "count": str(len(expired))})

# ── Helpers ────────────────────────────────────────────────────
def render(tpl: str, **kw) -> str:
    """Render a Jinja2 template from the templates directory."""
    return _jinja.get_template(tpl).render(**kw)

async def _bcast(room: Room, msg: dict, exclude: Optional[WebSocket] = None) -> None:
    """Broadcast a JSON message to all viewers, optionally excluding one WS connection.
    Silently removes stale connections."""
    dead = []
    raw = json.dumps(msg)
    for v in room.viewer_ws:
        if v["ws"] is exclude:
            continue
        try:
            await v["ws"].send_text(raw)
        except Exception:
            dead.append(v)
    for d in dead:
        try:
            room.viewer_ws.remove(d)
        except ValueError:
            pass
    if dead:
        logger.debug("bcast_dead", extra={"slug": room.slug, "dead": str(len(dead)), "total": str(len(room.viewer_ws))})

async def _tell_admin(room: Room, msg: dict) -> None:
    """Send a JSON message to the room's admin WebSocket, if connected."""
    if room.admin_ws:
        try:
            await room.admin_ws.send_text(json.dumps(msg))
        except Exception:
            room.admin_ws = None
            logger.debug("broadcast_fail", extra={"slug": room.slug, "reason": "ws_send_failed"})

# ── Routes ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Landing page — room creation form."""
    return HTMLResponse(render("index.html"))

@app.post("/create")
async def create_room(request: Request, name: str = Form(...), admin_password: str = Form(...), slug_mode: str = Form("name4")) -> RedirectResponse:
    """Create a new room, rate-limited, then redirect to its admin page."""
    rid = request.state.rid
    ip = request.client.host if request.client else "unknown"
    if not _ratelimit(f"create:{ip}", RATE_MAX_CREATE):
        logger.warning("ratelimit_hit", extra={"rid": rid, "slug": "-", "ip": ip, "action": "create"})
        raise HTTPException(429, "Too many rooms. Wait.")
    _cleanup()
    if len(rooms) >= MAX_ROOMS:
        logger.warning("max_rooms", extra={"rid": rid, "slug": "-", "count": str(len(rooms))})
        raise HTTPException(429, "Server full.")
    # Validate slug_mode
    if slug_mode not in SLUG_MODES:
        slug_mode = "name4"
    # Generate slug with collision handling
    slug = _generate_slug(name, slug_mode)
    attempts = 0
    while slug in rooms and attempts < 10:
        if slug_mode == "name":
            # name mode: can't auto-resolve, reject
            raise HTTPException(409, f"Slug '{slug}' déjà pris. Choisis un autre nom ou utilise le mode nom+code.")
        # Regenerate with new random suffix
        slug = _generate_slug(name, slug_mode)
        attempts += 1
    if slug in rooms:
        raise HTTPException(409, "Slug collision — réessaie.")
    rooms[slug] = Room(slug, name, admin_password)
    token = _sign(slug, "admin")
    resp = RedirectResponse(f"/party/{slug}/admin", status_code=303)
    _set_auth_cookie(resp, "sync_party_auth", token, request=request)
    logger.info("room_created", extra={"rid": rid, "slug": slug, "slug_mode": slug_mode, "room_name": name[:50], "ip": ip})
    return resp

@app.get("/party/{slug}/admin", response_class=HTMLResponse)
async def admin_page(request: Request, slug: str) -> HTMLResponse:
    """Admin dashboard (requires auth cookie) or login form."""
    rid = request.state.rid
    room = rooms.get(slug)
    if not room:
        logger.warning("admin_404", extra={"rid": rid, "slug": slug, "reason": "unknown_slug"})
        raise HTTPException(404)
    token = request.cookies.get("sync_party_auth", "")
    if not token or not _verify(token, slug, "admin"):
        logger.debug("admin_noauth", extra={"rid": rid, "slug": slug, "has_token": str(bool(token))})
        return HTMLResponse(render("admin_login.html", slug=slug, name=room.name))
    room.touch()
    logger.info("admin_page", extra={"rid": rid, "slug": slug, "room_name": room.name[:50]})
    return HTMLResponse(render("admin.html", slug=slug, name=room.name,
        playlist_url=room.playlist_url, global_mode=room.global_mode,
        provider=room.provider, auth_token=token))

@app.post("/party/{slug}/login")
async def admin_login(request: Request, slug: str, password: str = Form(...)) -> RedirectResponse:
    """Authenticate with the room password; set auth cookie and redirect."""
    rid = request.state.rid
    ip = request.client.host if request.client else "unknown"
    room = rooms.get(slug)
    if not room:
        logger.warning("login_404", extra={"rid": rid, "slug": slug, "reason": "unknown_slug"})
        raise HTTPException(404)
    if not _ratelimit(f"login:{slug}:{ip}", RATE_MAX_LOGIN):
        logger.warning("ratelimit_hit", extra={"rid": rid, "slug": slug, "ip": ip, "action": "login"})
        raise HTTPException(429, "Trop de tentatives.")
    if password != room.admin_password:
        logger.info("login_fail", extra={"rid": rid, "slug": slug, "ip": ip})
        raise HTTPException(401, "Mot de passe incorrect.")
    logger.info("login_ok", extra={"rid": rid, "slug": slug, "room_name": room.name[:50], "ip": ip})
    token = _sign(slug, "admin")
    resp = RedirectResponse(f"/party/{slug}/admin", status_code=303)
    _set_auth_cookie(resp, "sync_party_auth", token, request=request)
    return resp

@app.get("/party/{slug}", response_class=HTMLResponse)
async def watch_page(slug: str) -> HTMLResponse:
    """Viewer watch page for a room."""
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404)
    room.touch()
    return HTMLResponse(render("watch.html", slug=slug, name=room.name))

@app.get("/party/{slug}/qr")
async def qr_img(slug: str, type: str = "", request: Request = None) -> Response:
    """Generate a QR code image pointing to the room (type='room') or playlist URL (default)."""
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404)
    scheme = "https" if _is_secure(request) else "http"
    host = request.headers.get("host", "sync-party.onrender.com") if request else "sync-party.onrender.com"
    base = f"{scheme}://{host}/party/{slug}"
    if type == "room":
        url = base
    else:
        # Default: source (playlist URL), fallback to room URL
        url = room.playlist_url or base
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")

@app.get("/api/room/{slug}/state")
async def room_state(slug: str) -> dict:
    """JSON endpoint returning current room playback state + viewer count."""
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404)
    room.touch()
    st = room.player_state()
    st["viewer_count"] = len(room.viewer_ws)
    return st

@app.get("/providers")
async def providers_list() -> list[dict]:
    """List all available media providers."""
    return [p.__dict__ for p in list_providers()]

@app.get("/health")
async def health() -> dict:
    """Health-check endpoint; also triggers room expiry cleanup."""
    _cleanup()
    return {"status": "ok", "rooms": len(rooms)}

# ── Super-admin ────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def superadmin_login_page(request: Request) -> Response:
    """Super-admin login page (or redirect to dashboard if already authed)."""
    token = request.cookies.get("sync_party_su", "")
    if token and _verify_superadmin(token):
        return RedirectResponse("/admin/dashboard", status_code=303)
    return HTMLResponse(render("superadmin_login.html"))

@app.post("/admin/login")
async def superadmin_login(request: Request, password: str = Form(...)) -> RedirectResponse:
    """Authenticate as super-admin; set cookie and redirect to dashboard."""
    rid = request.state.rid
    ip = request.client.host if request.client else "unknown"
    if not _ratelimit(f"superadmin:{ip}", 5):
        logger.warning("ratelimit_hit", extra={"rid": rid, "slug": "-", "ip": ip, "action": "superadmin_login"})
        raise HTTPException(429, "Trop de tentatives.")
    if password != SUPER_ADMIN_PASSWORD:
        logger.info("superadmin_fail", extra={"rid": rid, "slug": "-", "ip": ip})
        raise HTTPException(401, "Mot de passe incorrect.")
    logger.info("superadmin_ok", extra={"rid": rid, "slug": "-", "ip": ip})
    token = _sign_superadmin()
    resp = RedirectResponse("/admin/dashboard", status_code=303)
    _set_auth_cookie(resp, "sync_party_su", token, request=request)
    return resp

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def superadmin_dashboard(request: Request) -> Response:
    """Super-admin dashboard showing all active rooms."""
    rid = request.state.rid
    token = request.cookies.get("sync_party_su", "")
    if not token or not _verify_superadmin(token):
        logger.debug("superadmin_noauth", extra={"rid": rid, "slug": "-", "has_token": str(bool(token))})
        return RedirectResponse("/admin", status_code=303)
    _cleanup()
    room_list = [{
        "slug": r.slug, "name": r.name, "provider": r.provider,
        "playlist_url": r.playlist_url or "-",
        "state": ["⏹", "▶", "⏸"][r.state + 1 if -1 <= r.state <= 2 else 0],
        "viewers": len(r.viewer_ws), "age": int(time.time() - r.created_at),
        "admin_online": r.admin_ws is not None,
    } for r in rooms.values()]
    room_list.sort(key=lambda r: r["viewers"], reverse=True)
    logger.info("superadmin_dashboard", extra={"rid": rid, "slug": "-", "rooms": str(len(room_list))})
    return HTMLResponse(render("superadmin_dashboard.html", rooms=room_list))

@app.post("/admin/room/{slug}/delete")
async def superadmin_delete_room(request: Request, slug: str) -> dict:
    """Force-delete a room (super-admin only)."""
    rid = request.state.rid
    token = request.cookies.get("sync_party_su", "")
    if not token or not _verify_superadmin(token):
        raise HTTPException(403)
    room = rooms.pop(slug, None)
    if not room:
        raise HTTPException(404, "Room not found")
    logger.info("room_deleted", extra={"rid": rid, "slug": slug, "room_name": room.name[:50], "by": "superadmin"})
    return {"deleted": slug, "name": room.name}


@app.get("/admin/logs")
async def superadmin_logs(request: Request, n: int = 100, level: str = "") -> Response:
    """Return the last N log lines from the ring buffer. Auth: super-admin cookie."""
    token = request.cookies.get("sync_party_su", "")
    if not token or not _verify_superadmin(token):
        raise HTTPException(403)
    lines = _log_ring[-n:]
    if level:
        lines = [l for l in lines if f"level={level.upper()}" in l]
    return Response(content="\n".join(lines), media_type="text/plain")


# ── WebSocket ──────────────────────────────────────────────────

@app.websocket("/ws/{slug}")
async def ws_endpoint(ws: WebSocket, slug: str) -> None:
    """WebSocket endpoint for room real-time communication.

    First message must be an auth JSON payload with ``token`` and ``role``.
    Subsequent messages are dispatched by role (admin → ``_admin_msg``,
    viewer/moderator → ``_viewer_msg``).
    """
    qp = dict(ws.query_params)
    role_hint = qp.get("role", "viewer")
    logger.debug("ws_connect", extra={"slug": slug, "role": role_hint})

    room = rooms.get(slug)
    if not room:
        await ws.accept()
        await ws.send_text(json.dumps({"type": "error", "message": "Room not found"}))
        await ws.close(code=4004, reason="Room not found")
        logger.info("ws_reject", extra={"slug": slug, "reason": "unknown_room", "role": role_hint})
        return

    await ws.accept()
    room.touch()
    logger.debug("ws_accepted", extra={"slug": slug, "role": role_hint})

    viewer = None

    # Auth: first message must be auth payload
    try:
        raw = await ws.receive_text()
        auth = json.loads(raw)
    except WebSocketDisconnect:
        logger.debug("ws_auth_timeout", extra={"slug": slug})
        await ws.close(code=4001)
        return
    except json.JSONDecodeError:
        await ws.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
        await ws.close(code=4001)
        return

    token = auth.get("token", "")
    claimed = auth.get("role", "viewer")
    role = "viewer"

    if claimed == "admin":
        if _verify(token, slug, "admin"):
            role = "admin"
        else:
            logger.warning("ws_bad_admin", extra={"slug": slug, "role": role_hint})
            await ws.send_text(json.dumps({"type": "error", "message": "Bad admin token"}))
            await ws.close(code=4001)
            return
    elif claimed == "moderator" and room.moderator_password:
        if token == room.moderator_password:
            role = "moderator"

    # Register
    if role == "admin":
        room.admin_ws = ws
        logger.info("ws_admin_joined", extra={"slug": slug, "viewers": str(len(room.viewer_ws))})
        await ws.send_text(json.dumps({"type": "auth_ok", "role": "admin", "viewers": room.viewer_list()}))
    else:
        name = f"Guest-{secrets.token_hex(2)}"
        is_dj = room.guest_dj_enabled or role == "moderator"
        viewer = {"ws": ws, "name": name, "mode": room.global_mode, "muted": False, "is_dj": is_dj, "role": role}
        room.viewer_ws.append(viewer)
        logger.info("ws_viewer_joined", extra={"slug": slug, "room_name": name, "role": role, "total": str(len(room.viewer_ws))})

        state = room.player_state()
        state["viewer_count"] = len(room.viewer_ws)
        state["your_name"] = name
        await ws.send_text(json.dumps(state))
        await _tell_admin(room, {"type": "viewer_join", "viewer": name, "viewers": room.viewer_list()})
        await _bcast(room, {"type": "viewer_joined", "name": name, "count": len(room.viewer_ws)}, exclude=ws)

    # Message loop
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if role == "admin":
                await _admin_msg(room, msg)
            else:
                await _viewer_msg(room, msg, viewer)
    except WebSocketDisconnect:
        logger.debug("ws_disconnect", extra={"slug": slug, "role": role})
        pass
    finally:
        if role == "admin":
            room.admin_ws = None
            logger.info("ws_admin_left", extra={"slug": slug})
        else:
            try:
                room.viewer_ws.remove(viewer)
            except (ValueError, NameError):
                pass
            vname = viewer.get("name", "???") if viewer else "???"
            logger.info("ws_viewer_left", extra={"slug": slug, "room_name": vname, "remaining": str(len(room.viewer_ws))})
            await _tell_admin(room, {"type": "viewer_leave", "viewer": vname, "viewers": room.viewer_list()})
            await _bcast(room, {"type": "viewer_left", "name": vname, "count": len(room.viewer_ws)})

# ── Message handlers ──────────────────────────────────────────

async def _admin_msg(room: Room, msg: dict) -> None:
    """Dispatch an incoming message from the room admin WebSocket."""
    t = msg.get("type", "")
    if t == "player_update":
        room.video_id = msg.get("video_id", room.video_id)
        room.video_title = msg.get("title", room.video_title)
        room.state = msg.get("state", room.state)
        room.current_time = msg.get("current_time", room.current_time)
        await _bcast(room, room.player_state())
    elif t == "set_playlist":
        room.playlist_url = msg["url"]
        room.video_id = msg.get("video_id", "")
        logger.info("playlist_set", extra={"slug": room.slug, "url": room.playlist_url[:80]})
        await _bcast(room, {"type": "playlist_set", "url": room.playlist_url, "video_id": room.video_id})
    elif t == "set_provider":
        room.provider = msg["provider"]
        await _bcast(room, {"type": "provider_changed", "provider": room.provider})
    elif t == "set_mode":
        room.global_mode = msg["mode"]
        await _bcast(room, {"type": "mode_changed", "mode": room.global_mode})
    elif t == "force_mode":
        target = msg.get("target", "all")
        mode = msg["mode"]
        if target == "all":
            for v in room.viewer_ws:
                v["mode"] = mode
            room.global_mode = mode
        else:
            for v in room.viewer_ws:
                if v["name"] == target:
                    v["mode"] = mode
        await _bcast(room, {"type": "mode_forced", "target": target, "mode": mode})
        await _tell_admin(room, {"type": "viewer_list", "viewers": room.viewer_list()})
    elif t in ("mute_viewer", "unmute_viewer"):
        target = msg["target"]
        muted = t == "mute_viewer"
        for v in room.viewer_ws:
            if v["name"] == target:
                v["muted"] = muted
                try:
                    await v["ws"].send_text(json.dumps({"type": "muted" if muted else "unmuted"}))
                except Exception:
                    pass
        await _tell_admin(room, {"type": "viewer_list", "viewers": room.viewer_list()})
    elif t == "kick_viewer":
        target = msg["target"]
        for v in room.viewer_ws[:]:
            if v["name"] == target:
                try:
                    await v["ws"].send_text(json.dumps({"type": "kicked"}))
                    await v["ws"].close()
                except Exception:
                    pass
                room.viewer_ws.remove(v)
        logger.info("viewer_kicked", extra={"slug": room.slug, "target": target})
        await _bcast(room, {"type": "viewer_kicked", "name": target})
        await _tell_admin(room, {"type": "viewer_list", "viewers": room.viewer_list()})
    elif t == "promote_dj":
        target = msg["target"]
        for v in room.viewer_ws:
            v["is_dj"] = (v["name"] == target)
            try:
                await v["ws"].send_text(json.dumps({"type": "dj_status", "is_dj": v["is_dj"]}))
            except Exception:
                pass
        await _tell_admin(room, {"type": "viewer_list", "viewers": room.viewer_list()})

async def _viewer_msg(room: Room, msg: dict, viewer: dict) -> None:
    """Dispatch an incoming message from a viewer (or moderator) WebSocket."""
    t = msg.get("type", "")
    if t == "set_name":
        viewer["name"] = msg["name"]
        await _tell_admin(room, {"type": "viewer_list", "viewers": room.viewer_list()})
    elif t == "set_mode" and not viewer.get("muted"):
        viewer["mode"] = msg["mode"]
        await _tell_admin(room, {"type": "viewer_list", "viewers": room.viewer_list()})
    elif viewer.get("role") == "moderator":
        if t == "set_playlist":
            room.playlist_url = msg["url"]
            await _bcast(room, {"type": "playlist_set", "url": room.playlist_url})
        elif t == "player_update":
            room.state = msg.get("state", room.state)
            room.current_time = msg.get("current_time", room.current_time)
            await _bcast(room, room.player_state())
        elif t == "set_provider":
            room.provider = msg["provider"]
            await _bcast(room, {"type": "provider_changed", "provider": room.provider})
    elif t == "dj_command" and viewer.get("is_dj"):
        cmd = msg.get("command", "")
        if cmd in ("next", "prev"):
            await _tell_admin(room, {"type": "dj_request", "viewer": viewer["name"], "command": cmd})

# ── Run ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)