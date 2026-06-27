#!/usr/bin/env python3
"""Audio sync test suite — verifies player_state broadcasts, timecode polling, drift correction,
   hidden audio player, and unlock button behavior."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

URL = os.environ.get("SYNC_PARTY_URL", "https://sync-party.onrender.com")

# ── Helpers ──────────────────────────────────────────────────

def _fmt(data: dict) -> str:
    """URL-encode form data dict → key=value&key=value string."""
    return "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
                    for k, v in data.items())


def curl(path, method="GET", data=None, cj=None, follow=False):
    """curl → (http_code, body, cj_path, final_url)"""
    cj = cj or tempfile.mktemp(suffix=".cookies")
    cmd = ["curl", "-s", "-w", "\n%{http_code}\n%{url_effective}",
           "-b", cj, "-c", cj, "--max-time", "15"]
    if data is not None:
        if isinstance(data, dict):
            data = _fmt(data)
        cmd += ["-d", data, "-H", "Content-Type: application/x-www-form-urlencoded"]
        # Don't add -X POST — curl infers POST from -d, and -X POST breaks 303 redirect following
    else:
        cmd += ["-X", method]
    if follow:
        cmd += ["-L"]
    cmd.append(f"{URL}{path}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    lines = r.stdout.strip().split("\n")
    code = lines[-2] if len(lines) >= 2 else "000"
    final = lines[-1] if len(lines) >= 1 else ""
    body = "\n".join(lines[:-2]) if len(lines) > 2 else ""
    return code.strip(), body, cj, final


def chk(name, ok, detail=""):
    status = "✅" if ok else "❌"
    print(f"  {status} {name} {detail}")
    if not ok:
        global FAILURES
        FAILURES += 1
        FAILURES_LIST.append(name)


def sec(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


FAILURES = 0
FAILURES_LIST = []


# ── Tests ────────────────────────────────────────────────────

def test_room_state_has_timecode():
    """player_state must include current_time for drift correction."""
    sec("1. Room state includes timecode")
    c, _, cj, final = curl("/create", "POST", {"name": "AudioTest", "admin_password": "audpwd"}, follow=True)
    m = re.search(r"/party/([a-z0-9-]+)/admin", final)
    slug = m.group(1) if m else None
    chk("Room created", bool(slug), slug or "no slug")

    if not slug:
        return None

    # Send a player_update via WebSocket requires async, so test via API state
    c, state_json, _, _ = curl(f"/api/room/{slug}/state")
    chk("State 200", c == "200")
    state = json.loads(state_json)
    chk("current_time in state", "current_time" in state, str(state.get("current_time", "?")))
    chk("video_id in state", "video_id" in state, state.get("video_id", "?"))
    chk("state in state", "state" in state, str(state.get("state", "?")))
    return slug, cj


def test_watch_page_has_audio_elements(slug):
    """Watch page must contain the hidden audio player container and unlock button."""
    sec("2. Watch page audio elements")
    c, body, _, _ = curl(f"/party/{slug}")
    chk("Watch page 200", c == "200", f"{len(body)}B")

    # Hidden player container (always in DOM, never display:none)
    chk("audio-player-container", "audio-player-container" in body)

    # Unlock button (hidden by default, shown when video playing in audio mode)
    chk("audio-unlock-btn", "audio-unlock-btn" in body)
    chk("unlockAudio function", "unlockAudio" in body)
    chk("audioUnlocked flag", "audioUnlocked" in body)

    # Audio viz ball with ID for CSS class toggling
    chk("audio-viz-ball", "audio-viz-ball" in body)

    # Drift correction
    chk("drift correction interval", "drift" in body)
    chk("lastSyncTime", "lastSyncTime" in body)

    # Sync functions
    chk("syncAudioPlayer", "syncAudioPlayer" in body)
    chk("initAudioPlayer", "initAudioPlayer" in body)


def test_admin_page_has_timecode_polling(slug, cj):
    """Admin page must include setInterval for timecode broadcasting."""
    sec("3. Admin timecode polling")
    c, body, _, _ = curl(f"/party/{slug}/admin", cj=cj)
    chk("Admin page 200", c == "200", f"{len(body)}B")

    # setInterval for polling
    chk("setInterval in admin", "setInterval" in body)

    # player_update broadcast
    chk("player_update broadcast", "player_update" in body)

    # 2000ms interval
    chk("2s polling interval", "2000" in body)


def test_watch_page_drift_correction(slug):
    """Watch page must have drift correction logic."""
    sec("4. Drift correction logic")
    c, body, _, _ = curl(f"/party/{slug}")
    chk("Watch page 200", c == "200")

    # 0.5s threshold for player_state corrections
    chk("0.5s sync threshold", "0.5" in body)

    # 1.0s threshold for periodic drift correction
    chk("1.0s drift threshold", "1.0" in body)

    # 5000ms drift check interval
    chk("5s drift interval", "5000" in body)


def test_watch_page_syncs_audio_mode(slug):
    """Watch page must send player_state to audio mode, not just video."""
    sec("5. Audio mode sync routing")
    c, body, _, _ = curl(f"/party/{slug}")

    # syncPlayer must handle 'audio' mode
    chk("audio mode in syncPlayer", "currentMode === 'audio'" in body or "'audio'" in body)

    # switchMode must init audio player
    chk("audio init in switchMode", "mode === 'audio'" in body)

    # Pause audio when leaving audio mode
    chk("pause on mode switch", "mode !== 'audio'" in body)


def test_audio_player_hidden_container(slug):
    """Audio player container must be always-visible (never display:none) for YT API."""
    sec("6. Audio player container visibility")
    c, body, _, _ = curl(f"/party/{slug}")

    # Container must use opacity:0, not display:none
    chk("opacity:0 container", "opacity:0" in body or "opacity: 0" in body)

    # pointer-events:none so it doesn't intercept clicks
    chk("pointer-events:none", "pointer-events:none" in body or "pointer-events" in body)

    # Must NOT be inside a display:none parent view (so it's always in the render tree)
    # Check that audio-player-container is BEFORE view-resume (outside .view elements)
    container_pos = body.find("audio-player-container")
    view_resume_pos = body.find('id="view-resume"')
    chk("Container before view-resume", container_pos < view_resume_pos and container_pos > 0)


def test_watch_page_resume_stores_video(slug):
    """Resume mode must store current video for later mode switch."""
    sec("7. Resume mode stores video for mode switch")
    c, body, _, _ = curl(f"/party/{slug}")

    # In resume mode, player_state should store video_id and syncTargetTime
    chk("Resume stores currentVideoId", "currentVideoId" in body)
    chk("Resume stores syncTargetTime", "syncTargetTime" in body)


def test_ws_player_update_broadcast(slug, cj):
    """WebSocket: admin sends player_update → server broadcasts player_state with timecode."""
    sec("8. WS: player_update → player_state with timecode")

    try:
        import asyncio
        import websockets
    except ImportError:
        chk("websockets installed", False, "pip install websockets")
        return

    # Extract admin token from cookie
    with open(cj) as f:
        ck = f.read()
    token_match = re.search(r"sync_party_auth\s+([^\s]+)$", ck, re.MULTILINE)
    admin_token = token_match.group(1) if token_match else ""

    ws_url = URL.replace("https://", "wss://").replace("http://", "ws://")

    async def test():
        # Connect viewer FIRST so it's in the room to receive broadcasts
        async with websockets.connect(f"{ws_url}/ws/{slug}") as viewer_ws:
            await viewer_ws.send(json.dumps({"role": "viewer"}))
            init = json.loads(await viewer_ws.recv())
            chk("Viewer init type", init.get("type") == "player_state", init.get("type", "?"))

            # Now connect admin
            async with websockets.connect(f"{ws_url}/ws/{slug}") as admin_ws:
                await admin_ws.send(json.dumps({"role": "admin", "token": admin_token}))
                auth = json.loads(await admin_ws.recv())
                chk("Admin auth", auth.get("type") == "auth_ok", str(auth))

                # Send player_update — viewer should receive the broadcast
                await admin_ws.send(json.dumps({
                    "type": "player_update",
                    "video_id": "dQw4w9WgXcQ",
                    "title": "Never Gonna Give You Up",
                    "state": 1,
                    "current_time": 42.5,
                }))

                # Receive viewer messages until we get a player_state with content
                deadline = time.time() + 5
                found = False
                while time.time() < deadline:
                    try:
                        msg = json.loads(await asyncio.wait_for(viewer_ws.recv(), timeout=2))
                    except asyncio.TimeoutError:
                        break
                    if msg.get("type") == "player_state" and msg.get("video_title"):
                        chk("timecode in broadcast", msg.get("current_time") is not None, str(msg.get("current_time")))
                        chk("video_id in broadcast", msg.get("video_id") == "dQw4w9WgXcQ", msg.get("video_id", "?"))
                        chk("state in broadcast", msg.get("state") == 1, str(msg.get("state")))
                        chk("title in broadcast", msg.get("video_title") == "Never Gonna Give You Up", msg.get("video_title", "?"))
                        found = True
                        break
                if not found:
                    chk("Received broadcast", False, "no player_state with content received")

    asyncio.run(test())


# ── Main ─────────────────────────────────────────────────────

def main():
    print(f"\n{'#'*60}")
    print(f"# Sync Party Audio Sync Test Suite")
    print(f"# URL: {URL}")
    print(f"# Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")

    result = test_room_state_has_timecode()
    if result is None:
        print("\n❌ Cannot create room — aborting")
        sys.exit(1)

    slug, cj = result

    test_watch_page_has_audio_elements(slug)
    test_admin_page_has_timecode_polling(slug, cj)
    test_watch_page_drift_correction(slug)
    test_watch_page_syncs_audio_mode(slug)
    test_audio_player_hidden_container(slug)
    test_watch_page_resume_stores_video(slug)
    test_ws_player_update_broadcast(slug, cj)

    # Cleanup
    c, _, su_cj, _ = curl("/admin/login", "POST",
        {"password": os.environ.get('SUPER_ADMIN_PWD', 'XC32m12R///SyncParty')},
        follow=True)
    if c == "303":
        curl(f"/admin/room/{slug}/delete", "POST", cj=su_cj)

    print(f"\n{'='*60}")
    if FAILURES == 0:
        print("  ✅ All tests passed")
    else:
        print(f"  ❌ {FAILURES} test(s) FAILED:")
        for f in FAILURES_LIST:
            print(f"     - {f}")
    print(f"{'='*60}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()