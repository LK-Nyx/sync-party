#!/usr/bin/env python3
"""Sync Party v3 — secure, synced watch party. No passwords in URLs."""

import json
import secrets
import time
import hmac
import hashlib
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import qrcode
import io
import base64
from jinja2 import Environment, FileSystemLoader
from providers.base import list_all as list_providers, get as get_provider
import providers.youtube
import providers.spotify

# ── Config ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
SERVER_SECRET = secrets.token_hex(32)
ROOM_TTL = 7200
MAX_ROOMS = 50
RATE_WINDOW = 60
RATE_MAX_CREATE = 5
RATE_MAX_LOGIN = 15

app = FastAPI(title="Sync Party")
_jinja = Environment(loader=FileSystemLoader(BASE_DIR / "templates"))

# ── Rate limiter ───────────────────────────────────────────────
_rate: dict[str, list[float]] = {}

def _ratelimit(key: str, max_req: int = RATE_MAX_LOGIN) -> bool:
    now = time.time()
    bucket = _rate.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < RATE_WINDOW]
    if len(bucket) >= max_req:
        return False
    bucket.append(now)
    return True

# ── Auth (HMAC-signed cookies — no passwords in URLs) ─────────
def _sign(slug: str, role: str) -> str:
    payload = f"{slug}:{role}:{int(time.time())}"
    sig = hmac.new(SERVER_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"

def _verify(token: str, slug: str, role: str = "admin") -> bool:
    try:
        parts = token.rsplit(":", 1)
        payload, sig = parts[0], parts[1]
        expected = hmac.new(SERVER_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            return False
        t_slug, t_role, _ = payload.split(":")
        return t_slug == slug and (role == "any" or t_role == role)
    except (ValueError, IndexError):
        return False

def _cookie_auth(request: Request) -> Optional[str]:
    return request.cookies.get("sync_party_auth")

# ── Room store ─────────────────────────────────────────────────
rooms: dict[str, "Room"] = {}

class Room:
    def __init__(self, slug: str, name: str, admin_password: str):
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

    def touch(self):
        self.last_activity = time.time()

    def expired(self) -> bool:
        return (time.time() - self.last_activity) > ROOM_TTL

    def player_state(self) -> dict:
        return {
            "type": "player_state",
            "video_id": self.video_id,
            "video_title": self.video_title,
            "state": self.state,
            "current_time": self.current_time,
            "global_mode": self.global_mode,
            "playlist_url": self.playlist_url,
            "provider": self.provider,
        }

    def viewer_list(self) -> list[dict]:
        return [{
            "name": v["name"], "mode": v["mode"],
            "muted": v["muted"], "is_dj": v["is_dj"], "role": v["role"],
        } for v in self.viewer_ws]

def _cleanup():
    for s in [s for s, r in rooms.items() if r.expired()]:
        del rooms[s]

# ── Helpers ────────────────────────────────────────────────────
def render(tpl: str, **kw) -> str:
    return _jinja.get_template(tpl).render(**kw)

async def _bcast(room: Room, msg: dict, exclude: Optional[WebSocket] = None):
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

async def _tell_admin(room: Room, msg: dict):
    if room.admin_ws:
        try:
            await room.admin_ws.send_text(json.dumps(msg))
        except Exception:
            room.admin_ws = None

# ── Routes ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(render("index.html"))


@app.post("/create")
async def create_room(request: Request, name: str = Form(...), admin_password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    if not _ratelimit(f"create:{ip}", RATE_MAX_CREATE):
        raise HTTPException(429, "Too many rooms. Wait.")
    _cleanup()
    if len(rooms) >= MAX_ROOMS:
        raise HTTPException(429, "Server full.")

    slug = secrets.token_hex(4)
    rooms[slug] = Room(slug, name, admin_password)
    token = _sign(slug, "admin")
    resp = RedirectResponse(f"/party/{slug}/admin", status_code=303)
    resp.set_cookie("sync_party_auth", token, httponly=True, samesite="strict",
                    max_age=ROOM_TTL, secure=True)
    return resp


@app.get("/party/{slug}/admin", response_class=HTMLResponse)
async def admin_page(request: Request, slug: str):
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404)
    token = _cookie_auth(request)
    if not token or not _verify(token, slug, "admin"):
        return HTMLResponse(render("admin_login.html", slug=slug, name=room.name))
    room.touch()
    return HTMLResponse(render("admin.html",
        slug=slug, name=room.name,
        playlist_url=room.playlist_url,
        global_mode=room.global_mode,
        provider=room.provider,
        auth_token=token,
    ))


@app.post("/party/{slug}/login")
async def admin_login(request: Request, slug: str, password: str = Form(...)):
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404)
    ip = request.client.host if request.client else "unknown"
    if not _ratelimit(f"login:{slug}:{ip}", RATE_MAX_LOGIN):
        raise HTTPException(429, "Trop de tentatives.")
    if password != room.admin_password:
        raise HTTPException(401, "Mot de passe incorrect.")
    token = _sign(slug, "admin")
    resp = RedirectResponse(f"/party/{slug}/admin", status_code=303)
    resp.set_cookie("sync_party_auth", token, httponly=True, samesite="strict",
                    max_age=ROOM_TTL, secure=True)
    return resp


@app.get("/party/{slug}", response_class=HTMLResponse)
async def watch_page(slug: str):
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404)
    room.touch()
    return HTMLResponse(render("watch.html", slug=slug, name=room.name))


@app.get("/party/{slug}/qr")
async def qr_img(slug: str):
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404)
    url = room.playlist_url or f"https://sync-party.onrender.com/party/{slug}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return HTMLResponse(f'<img src="data:image/png;base64,{base64.b64encode(buf.read()).decode()}">')


@app.get("/api/room/{slug}/state")
async def room_state(slug: str):
    """Cold-start: viewer gets current state when loading page."""
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404)
    room.touch()
    st = room.player_state()
    st["viewer_count"] = len(room.viewer_ws)
    return st


@app.get("/providers")
async def providers_list():
    return [p.__dict__ for p in list_providers()]


@app.get("/health")
async def health():
    _cleanup()
    return {"status": "ok", "rooms": len(rooms)}


# ── WebSocket ──────────────────────────────────────────────────

@app.websocket("/ws/{slug}")
async def ws_endpoint(ws: WebSocket, slug: str):
    room = rooms.get(slug)
    if not room:
        await ws.close(code=4004, reason="Room not found")
        return
    await ws.accept()
    room.touch()

    viewer = None  # will be set for non-admin roles

    # Client must send auth as first message
    try:
        raw = await ws.receive_text()
        auth = json.loads(raw)
    except (WebSocketDisconnect, json.JSONDecodeError):
        await ws.close(code=4001)
        return

    token = auth.get("token", "")
    claimed = auth.get("role", "viewer")
    role = "viewer"

    if claimed == "admin":
        if _verify(token, slug, "admin"):
            role = "admin"
        else:
            await ws.send_text(json.dumps({"type": "error", "message": "Bad admin token"}))
            await ws.close(code=4001)
            return
    elif claimed == "moderator" and room.moderator_password:
        if token == room.moderator_password:
            role = "moderator"

    # ── Register ────────────────────────────────────────────
    if role == "admin":
        room.admin_ws = ws
        await ws.send_text(json.dumps({
            "type": "auth_ok", "role": "admin",
            "viewers": room.viewer_list(),
        }))
    else:
        name = f"Guest-{secrets.token_hex(2)}"
        is_dj = room.guest_dj_enabled or role == "moderator"
        viewer = {"ws": ws, "name": name, "mode": room.global_mode,
                  "muted": False, "is_dj": is_dj, "role": role}
        room.viewer_ws.append(viewer)

        state = room.player_state()
        state["viewer_count"] = len(room.viewer_ws)
        state["your_name"] = name
        await ws.send_text(json.dumps(state))

        await _tell_admin(room, {
            "type": "viewer_join", "viewer": name,
            "viewers": room.viewer_list(),
        })
        await _bcast(room, {
            "type": "viewer_joined", "name": name,
            "count": len(room.viewer_ws),
        }, exclude=ws)

    # ── Message loop ─────────────────────────────────────────
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if role == "admin":
                await _admin_msg(room, msg)
            else:
                await _viewer_msg(room, msg, viewer)
    except WebSocketDisconnect:
        pass
    finally:
        if role == "admin":
            room.admin_ws = None
        else:
            vname = viewer.get("name", "???")
            try:
                room.viewer_ws.remove(viewer)
            except ValueError:
                pass
            await _tell_admin(room, {
                "type": "viewer_leave", "viewer": vname,
                "viewers": room.viewer_list(),
            })
            await _bcast(room, {
                "type": "viewer_left", "name": vname,
                "count": len(room.viewer_ws),
            })


# ── Message handlers ──────────────────────────────────────────

async def _admin_msg(room: Room, msg: dict):
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
        await _bcast(room, {"type": "playlist_set", "url": room.playlist_url,
                            "video_id": room.video_id})

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

    elif t == "mute_viewer":
        target = msg["target"]
        for v in room.viewer_ws:
            if v["name"] == target:
                v["muted"] = True
                try:
                    await v["ws"].send_text(json.dumps({"type": "muted"}))
                except Exception:
                    pass
        await _tell_admin(room, {"type": "viewer_list", "viewers": room.viewer_list()})

    elif t == "unmute_viewer":
        target = msg["target"]
        for v in room.viewer_ws:
            if v["name"] == target:
                v["muted"] = False
                try:
                    await v["ws"].send_text(json.dumps({"type": "unmuted"}))
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

    elif t == "show_qr":
        url = room.playlist_url or f"https://sync-party.onrender.com/party/{room.slug}"
        await _bcast(room, {"type": "show_qr", "url": url})

    elif t == "hide_qr":
        await _bcast(room, {"type": "hide_qr"})


async def _viewer_msg(room: Room, msg: dict, viewer: dict):
    t = msg.get("type", "")

    if t == "set_name":
        viewer["name"] = msg["name"]
        await _tell_admin(room, {"type": "viewer_list", "viewers": room.viewer_list()})

    elif t == "set_mode":
        if not viewer.get("muted"):
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
