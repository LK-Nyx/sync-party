#!/usr/bin/env python3
"""Sync Party — YouTube watch party with admin controls, QR codes, and karaoke mode."""

import json
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import qrcode
from qrcode.image.pil import PilImage
import io
import base64
from jinja2 import Environment, FileSystemLoader
from providers.base import list_all as list_providers, get as get_provider
import providers.youtube  # register
import providers.spotify  # register

# ── App setup ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
app = FastAPI(title="Sync Party")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
_jinja = Environment(loader=FileSystemLoader(BASE_DIR / "templates"))


def render(template: str, **kwargs) -> str:
    """Render a Jinja2 template directly, no cache-hashing of Python objects."""
    return _jinja.get_template(template).render(**kwargs)

# ── Room store (in-memory — Render free tier resets on deploy) ─
rooms: dict[str, dict] = {}

# ── Models ─────────────────────────────────────────────────────

class Room:
    """A watch party room with admin and viewer WebSocket connections."""
    
    def __init__(self, slug: str, name: str, admin_password: str):
        self.slug = slug
        self.name = name
        self.admin_password = admin_password
        self.created_at = time.time()
        
        # Player state
        self.playlist_url: str = ""
        self.video_id: str = ""
        self.state: int = -1  # YouTube state: -1 unstarted, 1 playing, 2 paused
        self.current_time: float = 0
        self.global_mode: str = "resume"  # resume | video | audio | karaoke
        self.provider: str = "youtube"  # provider key
        self.moderator_password: str = ""  # for moderator role
        self.guest_dj_enabled: bool = False  # anyone can DJ
        
        # Connections
        self.admin_ws: Optional[WebSocket] = None
        self.viewer_ws: list[dict] = []  # {ws, name, mode, muted, is_dj}
        
    def broadcast(self, msg: dict, exclude: Optional[WebSocket] = None):
        """Send message to all connected viewers."""
        dead = []
        raw = json.dumps(msg)
        for v in self.viewer_ws:
            if v["ws"] == exclude:
                continue
            try:
                # FastAPI WebSocket is async but we run sync for simplicity
                pass  # handled in async methods
            except:
                dead.append(v)
        for d in dead:
            self.viewer_ws.remove(d)
    
    def player_state_dict(self) -> dict:
        return {
            "type": "player_state",
            "video_id": self.video_id,
            "state": self.state,
            "current_time": self.current_time,
            "global_mode": self.global_mode,
            "playlist_url": self.playlist_url,
            "provider": self.provider,
        }

    def viewer_list(self) -> list[dict]:
        return [
            {
                "name": v["name"],
                "mode": v["mode"],
                "muted": v["muted"],
                "is_dj": v["is_dj"],
                "role": v["role"],
            }
            for v in self.viewer_ws
        ]


# ── Async WebSocket handlers (wrapped sync) ──────────────────

async def _broadcast(room: Room, msg: dict, exclude: Optional[WebSocket] = None):
    """Send message to all viewers in a room."""
    dead = []
    raw = json.dumps(msg)
    for v in room.viewer_ws:
        if v["ws"] == exclude:
            continue
        try:
            await v["ws"].send_text(raw)
        except:
            dead.append(v)
    for d in dead:
        try:
            room.viewer_ws.remove(d)
        except ValueError:
            pass


async def _send_admin(room: Room, msg: dict):
    """Send message to the admin."""
    if room.admin_ws:
        try:
            await room.admin_ws.send_text(json.dumps(msg))
        except:
            room.admin_ws = None


# ── Routes ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    """Landing page — create or join a room."""
    return HTMLResponse(render("index.html"))


@app.post("/create")
async def create_room(
    name: str = Form(...),
    admin_password: str = Form(...),
    moderator_password: str = Form(""),
    guest_dj: bool = Form(False),
):
    """Create a new room."""
    slug = secrets.token_hex(4)  # 8 chars, unique enough
    room = Room(slug=slug, name=name, admin_password=admin_password)
    room.moderator_password = moderator_password
    room.guest_dj_enabled = guest_dj
    rooms[slug] = room
    return RedirectResponse(f"/party/{slug}/admin", status_code=303)


@app.get("/party/{slug}/admin", response_class=HTMLResponse)
async def admin_dashboard(slug: str, pwd: str = ""):
    """Admin dashboard — shows password form if not authenticated."""
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404, "Room not found")
    
    # Check password via query param (simple cookie-less auth)
    if pwd != room.admin_password:
        return HTMLResponse(render("admin_login.html", slug=slug, name=room.name))
    
    return HTMLResponse(render("admin.html",
        slug=slug, name=room.name,
        playlist_url=room.playlist_url,
        global_mode=room.global_mode,
        provider=room.provider,
    ))


@app.get("/party/{slug}", response_class=HTMLResponse)
async def watch(slug: str):
    """Viewer page — no auth needed."""
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404, "Room not found")
    return HTMLResponse(render("watch.html",
        slug=slug, name=room.name,
    ))


@app.get("/party/{slug}/qr")
async def qr_code(slug: str):
    """Generate QR code for the room URL."""
    room = rooms.get(slug)
    if not room:
        raise HTTPException(404, "Room not found")
    
    url = room.playlist_url or f"https://sync-party.onrender.com/party/{slug}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return HTMLResponse(
        f'<img src="data:image/png;base64,{base64.b64encode(buf.read()).decode()}">'
    )


# ── WebSocket ──────────────────────────────────────────────────

@app.websocket("/ws/{slug}")
async def websocket_endpoint(ws: WebSocket, slug: str):
    """Main WebSocket endpoint for both admin and viewers."""
    room = rooms.get(slug)
    if not room:
        await ws.close(code=4004, reason="Room not found")
        return
    
    await ws.accept()
    
    qp = ws.query_params
    role = qp.get("role", "viewer")
    pwd = qp.get("pwd", "")
    
    if role == "admin":
        if pwd != room.admin_password:
            await ws.send_text(json.dumps({"type": "error", "message": "Wrong password"}))
            await ws.close(code=4001, reason="Wrong password")
            return
        room.admin_ws = ws
        
        # Send viewer list on connect
        await ws.send_text(json.dumps({
            "type": "viewer_list",
            "viewers": room.viewer_list(),
        }))
        
    else:  # viewer
        viewer_name = f"Guest-{secrets.token_hex(2)}"
        # Determine role from password
        viewer_role = "viewer"
        if pwd == room.moderator_password and room.moderator_password:
            viewer_role = "moderator"
        elif room.guest_dj_enabled:
            viewer_role = "dj"

        viewer = {
            "ws": ws,
            "name": viewer_name,
            "mode": room.global_mode,
            "muted": False,
            "is_dj": (viewer_role == "dj"),
            "role": viewer_role,
        }
        room.viewer_ws.append(viewer)
        
        # Send current player state
        await ws.send_text(json.dumps(room.player_state_dict()))
        
        # Notify admin of new viewer
        await _send_admin(room, {
            "type": "viewer_join",
            "viewer": viewer_name,
            "viewers": room.viewer_list(),
        })
        
        # Notify other viewers
        await _broadcast(room, {
            "type": "viewer_joined",
            "name": viewer_name,
            "count": len(room.viewer_ws),
        }, exclude=ws)
    
    # ── Message loop ─────────────────────────────────────────
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type", "")
            
            if role == "admin":
                await _handle_admin_message(room, msg, ws)
            else:
                await _handle_viewer_message(room, msg, ws, viewer)
                
    except WebSocketDisconnect:
        pass
    finally:
        if role == "admin":
            room.admin_ws = None
        else:
            try:
                room.viewer_ws.remove(viewer)
            except (ValueError, NameError):
                pass
            await _send_admin(room, {
                "type": "viewer_leave",
                "viewer": viewer.get("name", "unknown"),
                "viewers": room.viewer_list(),
            })
            await _broadcast(room, {
                "type": "viewer_left",
                "name": viewer.get("name", "unknown"),
                "count": len(room.viewer_ws),
            })


# ── Message handlers ──────────────────────────────────────────

async def _handle_admin_message(room: Room, msg: dict, ws: WebSocket):
    msg_type = msg.get("type", "")

    if msg_type == "hide_qr":
        await _broadcast(room, {"type": "hide_qr"})
    
    elif msg_type == "set_provider":
        room.provider = msg["provider"]
        await _broadcast(room, {
            "type": "provider_changed",
            "provider": room.provider,
        })

    elif msg_type == "set_playlist":
        room.playlist_url = msg["url"]
        room.video_id = msg.get("video_id", "")
        await _broadcast(room, {
            "type": "playlist_set",
            "url": room.playlist_url,
            "video_id": room.video_id,
        })
    
    elif msg_type == "player_update":
        room.video_id = msg.get("video_id", room.video_id)
        room.state = msg.get("state", room.state)
        room.current_time = msg.get("current_time", room.current_time)
        await _broadcast(room, room.player_state_dict())
    
    elif msg_type == "set_mode":
        room.global_mode = msg["mode"]
        await _broadcast(room, {
            "type": "mode_changed",
            "mode": room.global_mode,
        })
    
    elif msg_type == "force_mode":
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
        await _broadcast(room, {
            "type": "mode_forced",
            "target": target,
            "mode": mode,
        })
        await _send_admin(room, {
            "type": "viewer_list",
            "viewers": room.viewer_list(),
        })
    
    elif msg_type == "mute_viewer":
        target = msg["target"]
        for v in room.viewer_ws:
            if v["name"] == target:
                v["muted"] = True
                try:
                    await v["ws"].send_text(json.dumps({"type": "muted"}))
                except:
                    pass
        await _send_admin(room, {
            "type": "viewer_list",
            "viewers": room.viewer_list(),
        })
    
    elif msg_type == "unmute_viewer":
        target = msg["target"]
        for v in room.viewer_ws:
            if v["name"] == target:
                v["muted"] = False
                try:
                    await v["ws"].send_text(json.dumps({"type": "unmuted"}))
                except:
                    pass
        await _send_admin(room, {
            "type": "viewer_list",
            "viewers": room.viewer_list(),
        })
    
    elif msg_type == "kick_viewer":
        target = msg["target"]
        for v in room.viewer_ws[:]:
            if v["name"] == target:
                try:
                    await v["ws"].send_text(json.dumps({"type": "kicked"}))
                    await v["ws"].close()
                except:
                    pass
                room.viewer_ws.remove(v)
        await _broadcast(room, {"type": "viewer_kicked", "name": target})
        await _send_admin(room, {
            "type": "viewer_list",
            "viewers": room.viewer_list(),
        })
    
    elif msg_type == "promote_dj":
        target = msg["target"]
        for v in room.viewer_ws:
            v["is_dj"] = (v["name"] == target)
            try:
                await v["ws"].send_text(json.dumps({
                    "type": "dj_status",
                    "is_dj": v["is_dj"],
                }))
            except:
                pass
        await _send_admin(room, {
            "type": "viewer_list",
            "viewers": room.viewer_list(),
        })
    
    elif msg_type == "show_qr":
        url = room.playlist_url or f"https://sync-party.onrender.com/party/{room.slug}"
        await _broadcast(room, {"type": "show_qr", "url": url})
    
    elif msg_type == "hide_qr":
        await _broadcast(room, {"type": "hide_qr"})


async def _handle_viewer_message(room: Room, msg: dict, ws: WebSocket, viewer: dict):
    msg_type = msg.get("type", "")
    
    if msg_type == "set_name":
        viewer["name"] = msg["name"]
        await _send_admin(room, {
            "type": "viewer_list",
            "viewers": room.viewer_list(),
        })
    
    elif msg_type == "set_mode":
        if not viewer.get("muted", False):
            viewer["mode"] = msg["mode"]
            await _send_admin(room, {
                "type": "viewer_list",
                "viewers": room.viewer_list(),
            })
    
    # Moderator commands (mimic admin actions)
    elif viewer.get("role") == "moderator":
        if msg_type == "set_provider":
            room.provider = msg["provider"]
            await _broadcast(room, {"type": "provider_changed", "provider": room.provider})
            await _send_admin(room, {"type": "provider_changed", "provider": room.provider})
        elif msg_type == "set_playlist":
            room.playlist_url = msg["url"]
            await _broadcast(room, {"type": "playlist_set", "url": room.playlist_url})
        elif msg_type == "player_update":
            room.state = msg.get("state", room.state)
            room.current_time = msg.get("current_time", room.current_time)
            await _broadcast(room, room.player_state_dict())

    elif msg_type == "dj_command":
        if viewer.get("is_dj", False):
            cmd = msg.get("command", "")
            if cmd in ("next", "prev"):
                await _send_admin(room, {
                    "type": "dj_request",
                    "viewer": viewer["name"],
                    "command": cmd,
                })


@app.get("/providers")
async def providers_list():
    """List available providers."""
    return [p.__dict__ for p in list_providers()]


# ── Health check (for Render) ──────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "rooms": len(rooms)}


# ── Run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
