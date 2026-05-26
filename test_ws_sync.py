#!/usr/bin/env python3
"""WebSocket sync test — admin sends player_update, viewer receives it."""

import json
import os
import subprocess
import sys
import time
import tempfile
import tempfile
import re

URL = os.environ.get("SYNC_PARTY_URL", "https://sync-party.onrender.com")
WS_URL = URL.replace("https://", "wss://").replace("http://", "ws://")

def curl(path, **kw):
    cmd = ["curl", "-s", "-w", "\n%{http_code}\n%{url_effective}"]
    if "cookie" in kw:
        cmd += ["-b", kw["cookie"]]
    cmd += ["--max-time", "10", path if path.startswith("http") else f"{URL}{path}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.stdout.strip()

def main():
    print(f"\n{'#'*60}")
    print("# Sync Party WebSocket Sync Test")
    print(f"# URL: {URL}")
    print(f"# Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")

    # 1. Create room → get slug (with rate-limit retry)
    print("📦 1. Creating room (retry until rate limit clears)...")
    cj = tempfile.mktemp(suffix=".cookies")
    slug = None
    for attempt in range(5):
        if attempt > 0:
            print(f"  Retry {attempt}/4 in 65s (rate limit)...")
            time.sleep(65)
        r = subprocess.run([
            "curl", "-s", "-c", cj, "-b", cj, "-L",
            "-X", "POST", "-d", "name=WSTest&admin_password=wspwd",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "--max-time", "10", f"{URL}/create",
        ], capture_output=True, text=True, timeout=15)
        lines = r.stdout.strip().split("\n")
        final_url = lines[-1] if lines else ""
        m = re.search(r"/party/([a-f0-9]+)/admin", final_url)
        slug = m.group(1) if m else None
        if slug:
            print(f"  ✅ Room created: {slug}")
            break
        print(f"  ⚠️ Create failed (rate limit?), waiting...")
    if not slug:
        print("❌ No slug after 5 attempts — abort")
        sys.exit(1)

    # 2. Get admin auth token from cookie file
    with open(cj) as f:
        cookie_data = f.read()
    token_match = re.search(r"sync_party_auth\s+([^\s]+)$", cookie_data, re.MULTILINE)
    admin_token = token_match.group(1) if token_match else ""
    if not admin_token:
        print("❌ No admin token — abort")
        sys.exit(1)
    print(f"✅ Admin token: {admin_token[:20]}...")

    # 3. Connect admin WS & viewer WS using websocat or python
    try:
        import asyncio
        import websockets
    except ImportError:
        print("⚠️ websockets not installed. Installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "websockets", "-q"],
                       capture_output=True)
        import asyncio
        import websockets

    async def test_sync():
        ws_url = f"{WS_URL}/ws/{slug}"
        admin_received = []
        viewer_received = []

        async def admin_ws():
            async with websockets.connect(ws_url) as ws:
                # Auth
                await ws.send(json.dumps({"role": "admin", "token": admin_token}))
                auth_resp = json.loads(await ws.recv())
                assert auth_resp["type"] == "auth_ok", f"Admin auth failed: {auth_resp}"
                admin_received.append(auth_resp)
                print("✅ Admin WS connected")

                # Send player_update
                await ws.send(json.dumps({
                    "type": "player_update",
                    "video_id": "dQw4w9WgXcQ",
                    "title": "Rick Astley - Never Gonna Give You Up",
                    "state": 1,
                    "current_time": 42.0,
                }))
                print("✅ Admin sent player_update")
                await asyncio.sleep(1)

        async def viewer_ws():
            async with websockets.connect(ws_url) as ws:
                # Auth
                await ws.send(json.dumps({"role": "viewer", "token": ""}))
                init_state = json.loads(await ws.recv())
                viewer_received.append(init_state)

                # Receive player_update
                update = json.loads(await ws.recv())
                viewer_received.append(update)
                print(f"✅ Viewer received: {update['type']} — {update.get('video_title', '?')}")

        # Run both WS connections concurrently
        await asyncio.gather(admin_ws(), viewer_ws())

        # Verify
        print("\n📊 Results:")
        print(f"  Admin messages: {len(admin_received)}")
        print(f"  Viewer messages: {len(viewer_received)}")

        # Check sync
        viewer_player_state = None
        for msg in viewer_received:
            if msg.get("type") == "player_state":
                viewer_player_state = msg
                break

        if viewer_player_state:
            print(f"  ✅ Player state received")
            print(f"  ✅ Video: {viewer_player_state.get('video_title', '?')}")
            print(f"  ✅ State: {viewer_player_state.get('state', '?')}")
            print(f"  ✅ Time: {viewer_player_state.get('current_time', '?')}")
        else:
            print("  ❌ No player_state received by viewer")
            return False

        return True

    success = asyncio.run(test_sync())

    print(f"\n{'='*60}")
    if success:
        print("  ✅ WebSocket sync test PASSED")
    else:
        print("  ❌ WebSocket sync test FAILED")
    print(f"{'='*60}")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()