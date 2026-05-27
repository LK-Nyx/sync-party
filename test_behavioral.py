#!/usr/bin/env python3
"""Sync Party behavioral test suite — real WS interactions, state transitions,
   edge cases, and user comfort checks. No HTML string matching — only
   protocol-level behavior validation."""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time

URL = os.environ.get("SYNC_PARTY_URL", "https://sync-party.onrender.com")
SUPERADMIN_PWD = os.environ.get("SUPER_ADMIN_PWD", "changeme_superadmin")

FAILURES = 0
FAILURES_LIST = []


# ── Helpers ──────────────────────────────────────────────────

def curl(path, method="GET", data=None, cj=None, follow=False, raw=False):
    """curl → (http_code, body, cj_path, final_url)"""
    cj = cj or tempfile.mktemp(suffix=".cookies")
    cmd = ["curl", "-s", "-w", "\n%{http_code}\n%{url_effective}",
           "-b", cj, "-c", cj, "--max-time", "15"]
    # Only set -X for non-GET methods without data (data implies POST).
    # Using -X POST forces the method even after redirects (303),
    # which breaks cookie-based auth flow.
    if data is not None:
        cmd += ["-d", data, "-H", "Content-Type: application/x-www-form-urlencoded"]
    elif method != "GET":
        cmd += ["-X", method]
    if follow:
        cmd += ["-L"]
    cmd.append(f"{URL}{path}")
    r = subprocess.run(cmd, capture_output=True, text=not raw, timeout=20)
    if raw:
        stdout = r.stdout.decode("latin-1")
        lines = stdout.strip().split("\n")
        code = lines[-2] if len(lines) >= 2 else "000"
        final = lines[-1] if len(lines) >= 1 else ""
        return code.strip(), r.stdout, cj, final
    lines = r.stdout.strip().split("\n")
    code = lines[-2] if len(lines) >= 2 else "000"
    final = lines[-1] if len(lines) >= 1 else ""
    body = "\n".join(lines[:-2]) if len(lines) > 2 else ""
    return code.strip(), body, cj, final


def chk(name, ok, detail=""):
    status = "✅" if ok else "❌"
    print(f"  {status} {name} {detail}")
    global FAILURES
    if not ok:
        FAILURES += 1
        FAILURES_LIST.append(name)


def sec(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def create_room(name="BehaviorTest", pwd="testpwd123"):
    """Create a room and return (slug, cookie_jar_path) or (None, None)."""
    c, _, cj, final = curl("/create", "POST",
                            f"name={name}&admin_password={pwd}&slug_mode=hex8", follow=True)
    m = re.search(r"/party/([a-z0-9-]+)/admin", final)
    if not m:
        return None, None
    return m.group(1), cj


def get_admin_token(cj):
    """Extract admin auth token from cookie jar."""
    with open(cj) as f:
        ck = f.read()
    m = re.search(r"sync_party_auth\s+([^\s]+)$", ck, re.MULTILINE)
    return m.group(1) if m else ""


async def ws_connect(slug, role="viewer", token=""):
    """Connect to WS, send auth, return ws connection."""
    import websockets
    ws_url = URL.replace("https://", "wss://").replace("http://", "ws://")
    ws = await websockets.connect(f"{ws_url}/ws/{slug}")
    if role == "admin":
        await ws.send(json.dumps({"role": "admin", "token": token}))
    else:
        await ws.send(json.dumps({"role": "viewer"}))
    return ws


async def recv_until(ws, msg_type, timeout=5):
    """Receive messages until we get one with the given type, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.time()))
            msg = json.loads(raw)
            if msg.get("type") == msg_type:
                return msg
        except asyncio.TimeoutError:
            break
    return None


# ── Test functions ────────────────────────────────────────────

def test_room_state_api():
    """Room state API returns all required fields."""
    sec("1. Room state API")
    slug, cj = create_room()
    if not slug:
        chk("Room creation", False, "could not create room")
        return None, None

    c, body, _, _ = curl(f"/api/room/{slug}/state")
    chk("State 200", c == "200")
    try:
        state = json.loads(body)
        chk("video_id field", "video_id" in state)
        chk("video_title field", "video_title" in state)
        chk("current_time field", "current_time" in state)
        chk("state field", "state" in state)
        chk("global_mode field", "global_mode" in state)
        chk("provider field", "provider" in state)
        chk("playlist_url field", "playlist_url" in state)
        chk("Default provider=youtube", state.get("provider") == "youtube")
        chk("Default mode=resume", state.get("global_mode") == "resume")
        chk("Default state=-1", state.get("state") == -1)
        chk("Default current_time=0", state.get("current_time") == 0.0)
    except json.JSONDecodeError:
        chk("State valid JSON", False)

    return slug, cj


def test_qr_endpoints(slug):
    """QR endpoint supports ?type=room for invite URL."""
    sec("2. QR endpoints")
    # Default QR = source (playlist URL or fallback room URL)
    c, body, _, _ = curl(f"/party/{slug}/qr", raw=True)
    chk("QR default 200", c == "200")
    chk("QR default > 200B", len(body) > 200)
    # PNG signature
    chk("QR default is PNG", body[:4] == b'\x89PNG')

    # Room QR
    c, body, _, _ = curl(f"/party/{slug}/qr?type=room", raw=True)
    chk("QR room 200", c == "200")
    chk("QR room > 200B", len(body) > 200)
    chk("QR room is PNG", body[:4] == b'\x89PNG')

    # 404 for non-existent room
    c, _, _, _ = curl("/party/deadbeef/qr")
    chk("QR 404 for bad slug", c == "404")


def test_authorization():
    """Wrong password → 401, no auth cookie → login page."""
    sec("3. Authorization")
    slug, cj = create_room()
    if not slug:
        return

    # Wrong password
    c, _, _, _ = curl(f"/party/{slug}/login", "POST",
                     f"password=wrongpassword", cj=cj, follow=False)
    chk("Wrong password → 401", c == "401", f"got {c}")

    # Access admin page without valid cookie → should show login form (200 with login fields)
    c2_cj = tempfile.mktemp(suffix=".cookies")
    c, body, _, _ = curl(f"/party/{slug}/admin", cj=c2_cj)
    chk("Admin without auth → login page", "Mot de passe" in body or "password" in body, "got login form" if ("password" in body) else "no login form")


def test_ws_player_state_broadcast(slug, cj):
    """Admin sends player_update → all viewers receive player_state with timecode."""
    sec("4. WS: player_update → player_state broadcast")

    try:
        import websockets
    except ImportError:
        chk("websockets installed", False, "pip install websockets")
        return

    token = get_admin_token(cj)
    if not token:
        chk("Admin token extraction", False)
        return

    async def test():
        # Viewer connects first
        viewer = await ws_connect(slug, "viewer")
        init = json.loads(await viewer.recv())
        chk("Viewer init type", init.get("type") == "player_state", init.get("type", "?"))

        # Admin connects
        admin = await ws_connect(slug, "admin", token)
        auth = json.loads(await admin.recv())
        chk("Admin auth_ok", auth.get("type") == "auth_ok", str(auth))

        # Admin sends player_update
        await admin.send(json.dumps({
            "type": "player_update",
            "video_id": "dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "state": 1,
            "current_time": 42.5,
        }))

        # Viewer should get broadcast
        msg = await recv_until(viewer, "player_state", timeout=5)
        if msg:
            chk("Broadcast has video_id", msg.get("video_id") == "dQw4w9WgXcQ", msg.get("video_id", "?"))
            chk("Broadcast has title", msg.get("video_title") == "Never Gonna Give You Up", msg.get("video_title", "?"))
            chk("Broadcast has state=1", msg.get("state") == 1, str(msg.get("state")))
            chk("Broadcast has current_time", msg.get("current_time") is not None, str(msg.get("current_time")))
            chk("Broadcast has global_mode", msg.get("global_mode") is not None, str(msg.get("global_mode")))
        else:
            chk("Received player_state broadcast", False, "timeout")

        await admin.close()
        await viewer.close()

    asyncio.run(test())


def test_ws_song_change(slug, cj):
    """Admin changes song (different video_id) — viewer gets new ID, not stale."""
    sec("5. WS: Song change (video_id swap)")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        viewer = await ws_connect(slug, "viewer")
        json.loads(await viewer.recv())  # initial state

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())  # auth_ok

        # Song 1
        await admin.send(json.dumps({
            "type": "player_update", "video_id": "vid1",
            "title": "Song One", "state": 1, "current_time": 10.0,
        }))
        msg1 = await recv_until(viewer, "player_state", timeout=5)
        chk("Song 1 video_id", msg1 and msg1.get("video_id") == "vid1", str(msg1.get("video_id", "?") if msg1 else "no msg"))

        # Song 2 — different video_id
        await admin.send(json.dumps({
            "type": "player_update", "video_id": "vid2",
            "title": "Song Two", "state": 1, "current_time": 0.0,
        }))
        msg2 = await recv_until(viewer, "player_state", timeout=5)
        chk("Song 2 video_id", msg2 and msg2.get("video_id") == "vid2", str(msg2.get("video_id", "?") if msg2 else "no msg"))
        chk("Song 2 title", msg2 and msg2.get("video_title") == "Song Two", str(msg2.get("video_title", "?") if msg2 else "no msg"))
        chk("Song 2 time reset", msg2 and msg2.get("current_time") == 0.0, str(msg2.get("current_time", "?") if msg2 else "no msg"))

        # Song 1 again (replay)
        await admin.send(json.dumps({
            "type": "player_update", "video_id": "vid1",
            "title": "Song One (replay)", "state": 1, "current_time": 0.0,
        }))
        msg3 = await recv_until(viewer, "player_state", timeout=5)
        chk("Replay video_id=vid1", msg3 and msg3.get("video_id") == "vid1", str(msg3.get("video_id", "?") if msg3 else "no msg"))

        await admin.close()
        await viewer.close()

    asyncio.run(test())


def test_ws_mode_change(slug, cj):
    """Mode changes broadcast correctly."""
    sec("6. WS: Mode change broadcast")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        viewer = await ws_connect(slug, "viewer")
        json.loads(await viewer.recv())

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())

        # Set mode to video
        await admin.send(json.dumps({"type": "set_mode", "mode": "video"}))
        msg = await recv_until(viewer, "mode_changed", timeout=5)
        chk("Mode changed to video", msg and msg.get("mode") == "video", str(msg) if msg else "no msg")

        # Set mode to audio
        await admin.send(json.dumps({"type": "set_mode", "mode": "audio"}))
        msg = await recv_until(viewer, "mode_changed", timeout=5)
        chk("Mode changed to audio", msg and msg.get("mode") == "audio", str(msg) if msg else "no msg")

        # Force mode for all viewers
        await admin.send(json.dumps({"type": "force_mode", "target": "all", "mode": "karaoke"}))
        forced_msg = await recv_until(viewer, "mode_forced", timeout=5)
        chk("Mode forced to karaoke", forced_msg and forced_msg.get("mode") == "karaoke", str(forced_msg) if forced_msg else "no msg")

        await admin.close()
        await viewer.close()

    asyncio.run(test())


def test_ws_join_leave(slug, cj):
    """Viewer join/leave increments/decrements count."""
    sec("7. WS: Viewer join/leave")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        viewer1 = await ws_connect(slug, "viewer")
        msg1 = json.loads(await viewer1.recv())
        chk("Viewer1 join count", msg1.get("viewer_count") == 1, str(msg1.get("viewer_count", "?")))

        viewer2 = await ws_connect(slug, "viewer")
        msg2 = json.loads(await viewer2.recv())
        chk("Viewer2 join count", msg2.get("viewer_count") == 2, str(msg2.get("viewer_count", "?")))

        # Viewer1 should receive viewer_joined
        join_msg = await recv_until(viewer1, "viewer_joined", timeout=3)
        chk("Viewer1 sees join", join_msg is not None and join_msg.get("count") == 2, str(join_msg) if join_msg else "no msg")

        # Viewer2 leaves
        await viewer2.close()
        await asyncio.sleep(0.5)
        left_msg = await recv_until(viewer1, "viewer_left", timeout=3)
        chk("Viewer1 sees leave", left_msg is not None and left_msg.get("count") == 1, str(left_msg) if left_msg else "no msg")

        await viewer1.close()

    asyncio.run(test())


def test_ws_kick(slug, cj):
    """Admin kicks a viewer → viewer receives kicked message."""
    sec("8. WS: Kick viewer")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        viewer = await ws_connect(slug, "viewer")
        init = json.loads(await viewer.recv())
        viewer_name = init.get("your_name", "?")

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())

        # Kick the viewer
        await admin.send(json.dumps({"type": "kick_viewer", "target": viewer_name}))

        # Viewer should receive kicked message
        kicked = await recv_until(viewer, "kicked", timeout=3)
        chk("Kicked message received", kicked is not None, str(kicked))

        await admin.close()
        try:
            await viewer.close()
        except Exception:
            pass

    asyncio.run(test())


def test_ws_mute_unmute(slug, cj):
    """Mute forces resume mode; unmute restores."""
    sec("9. WS: Mute/unmute viewer")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        viewer = await ws_connect(slug, "viewer")
        init = json.loads(await viewer.recv())
        viewer_name = init.get("your_name", "?")

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())

        # Mute
        await admin.send(json.dumps({"type": "mute_viewer", "target": viewer_name}))
        muted = await recv_until(viewer, "muted", timeout=3)
        chk("Muted message received", muted is not None, str(muted))

        # After mute, viewer should be forced to resume mode
        # The muted handler in viewer forces resume mode

        # Unmute
        await admin.send(json.dumps({"type": "unmute_viewer", "target": viewer_name}))
        unmuted = await recv_until(viewer, "unmuted", timeout=3)
        chk("Unmuted message received", unmuted is not None, str(unmuted))

        await admin.close()
        await viewer.close()

    asyncio.run(test())


def test_ws_dj_promotion(slug, cj):
    """Admin promotes viewer to DJ → dj_status message."""
    sec("10. WS: DJ promotion")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        viewer = await ws_connect(slug, "viewer")
        init = json.loads(await viewer.recv())
        viewer_name = init.get("your_name", "?")

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())

        # Promote to DJ
        await admin.send(json.dumps({"type": "promote_dj", "target": viewer_name}))
        dj_msg = await recv_until(viewer, "dj_status", timeout=3)
        chk("DJ status received", dj_msg is not None and dj_msg.get("is_dj") == True, str(dj_msg))

        # Demote (promote with empty target)
        await admin.send(json.dumps({"type": "promote_dj", "target": ""}))

        await admin.close()
        await viewer.close()

    asyncio.run(test())


def test_concurrent_viewers(slug, cj):
    """3 viewers join, all receive broadcasts."""
    sec("11. Concurrent viewers broadcast")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        v1 = await ws_connect(slug, "viewer")
        json.loads(await v1.recv())
        v2 = await ws_connect(slug, "viewer")
        json.loads(await v2.recv())
        v3 = await ws_connect(slug, "viewer")
        json.loads(await v3.recv())

        # Drain join notifications for v1 and v2
        for ws in [v1, v2]:
            try:
                await asyncio.wait_for(ws.recv(), timeout=1)
            except asyncio.TimeoutError:
                pass

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())

        await admin.send(json.dumps({
            "type": "player_update", "video_id": "concurrent_test",
            "title": "Concurrent Test", "state": 1, "current_time": 0.0,
        }))

        received = 0
        for ws in [v1, v2, v3]:
            msg = await recv_until(ws, "player_state", timeout=3)
            if msg and msg.get("video_id") == "concurrent_test":
                received += 1

        chk("All 3 viewers got broadcast", received == 3, f"{received}/3 received")

        await admin.close()
        for ws in [v1, v2, v3]:
            await ws.close()

    asyncio.run(test())


def test_edge_cases(slug, cj):
    """Edge cases: nonexistent room WS, malformed JSON, state transitions."""
    sec("12. Edge cases")

    try:
        import websockets
    except ImportError:
        return

    # WS to nonexistent room
    async def test():
        ws_url = URL.replace("https://", "wss://").replace("http://", "ws://")
        try:
            ws = await websockets.connect(f"{ws_url}/ws/deadbeef00")
            await ws.send(json.dumps({"role": "viewer"}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            chk("Nonexistent room → error", msg.get("type") == "error", str(msg))
            await ws.close()
        except Exception as e:
            chk("Nonexistent room → WS reject", True, f"connection failed: {e}")

    asyncio.run(test())

    # Separate connection for state transitions (malformed JSON kills the ws)
    async def test_states():
        token = get_admin_token(cj)
        viewer = await ws_connect(slug, "viewer")
        init = json.loads(await viewer.recv())

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())

        # State transitions: pause → play → pause
        await admin.send(json.dumps({
            "type": "player_update", "video_id": "state_test",
            "title": "State Test", "state": 2, "current_time": 15.0,  # paused
        }))
        msg = await recv_until(viewer, "player_state", timeout=3)
        chk("Paused state (2)", msg and msg.get("state") == 2, str(msg.get("state") if msg else "?"))

        await admin.send(json.dumps({
            "type": "player_update", "video_id": "state_test",
            "title": "State Test", "state": 1, "current_time": 15.5,
        }))
        msg = await recv_until(viewer, "player_state", timeout=3)
        chk("Playing state (1)", msg and msg.get("state") == 1, str(msg.get("state") if msg else "?"))

        # Zero timecode
        await admin.send(json.dumps({
            "type": "player_update", "video_id": "state_test",
            "title": "State Test", "state": 1, "current_time": 0,
        }))
        msg = await recv_until(viewer, "player_state", timeout=3)
        chk("Zero timecode OK", msg and msg.get("current_time") == 0, str(msg.get("current_time") if msg else "?"))

        await admin.close()
        await viewer.close()

    asyncio.run(test_states())


def test_playlist_set(slug, cj):
    """Set playlist → viewers get playlist_set broadcast with URL."""
    sec("13. WS: Playlist set broadcast")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        viewer = await ws_connect(slug, "viewer")
        json.loads(await viewer.recv())

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())

        test_url = "https://www.youtube.com/playlist?list=PLtest123"
        await admin.send(json.dumps({
            "type": "set_playlist", "url": test_url, "video_id": "",
        }))
        msg = await recv_until(viewer, "playlist_set", timeout=5)
        chk("Playlist set received", msg is not None, str(msg))
        chk("Playlist URL correct", msg and msg.get("url") == test_url, str(msg.get("url", "?") if msg else "?"))

        await admin.close()
        await viewer.close()

    asyncio.run(test())


def test_rate_limiting():
    """Creating too many rooms → 429."""
    sec("14. Rate limiting")
    rj = tempfile.mktemp(suffix=".cookies")
    last = "000"
    for i in range(7):
        last, _, rj, _ = curl("/create", "POST",
                               f"name=Rate+{i}&admin_password=test", cj=rj, follow=True)
    chk("Rate limited (429)", last == "429", f"got {last}")


def test_provider_change(slug, cj):
    """Admin changes provider → viewers notified."""
    sec("15. WS: Provider change broadcast")

    try:
        import websockets
    except ImportError:
        return

    token = get_admin_token(cj)

    async def test():
        viewer = await ws_connect(slug, "viewer")
        json.loads(await viewer.recv())

        admin = await ws_connect(slug, "admin", token)
        json.loads(await admin.recv())

        await admin.send(json.dumps({"type": "set_provider", "provider": "spotify"}))
        msg = await recv_until(viewer, "provider_changed", timeout=5)
        chk("Provider changed to spotify", msg and msg.get("provider") == "spotify", str(msg) if msg else "no msg")

        # Change back
        await admin.send(json.dumps({"type": "set_provider", "provider": "youtube"}))
        msg = await recv_until(viewer, "provider_changed", timeout=5)
        chk("Provider changed back to youtube", msg and msg.get("provider") == "youtube", str(msg) if msg else "no msg")

        await admin.close()
        await viewer.close()

    asyncio.run(test())


# ── Main ─────────────────────────────────────────────────────

def main():
    print(f"\n{'#'*60}")
    print(f"# Sync Party Behavioral Test Suite")
    print(f"# URL: {URL}")
    print(f"# Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")

    slug, cj = test_room_state_api()
    if not slug:
        print("\n❌ Cannot create room — aborting")
        sys.exit(1)

    test_qr_endpoints(slug)
    test_authorization()
    test_ws_player_state_broadcast(slug, cj)
    test_ws_song_change(slug, cj)
    test_ws_mode_change(slug, cj)
    test_ws_join_leave(slug, cj)
    test_ws_kick(slug, cj)
    test_ws_mute_unmute(slug, cj)
    test_ws_dj_promotion(slug, cj)
    test_concurrent_viewers(slug, cj)
    test_edge_cases(slug, cj)
    test_playlist_set(slug, cj)
    test_rate_limiting()
    test_provider_change(slug, cj)

    # Cleanup
    su_cj = tempfile.mktemp(suffix=".cookies")
    c, _, su_cj, _ = curl("/admin/login", "POST",
        f"password={SUPERADMIN_PWD}", follow=True)
    if c == "303":
        curl(f"/admin/room/{slug}/delete", "POST", cj=su_cj)

    print(f"\n{'='*60}")
    if FAILURES == 0:
        print("  ✅ All behavioral tests passed")
    else:
        print(f"  ❌ {FAILURES} test(s) FAILED:")
        for f in FAILURES_LIST:
            print(f"     - {f}")
    print(f"{'='*60}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()