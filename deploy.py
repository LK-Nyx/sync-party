#!/usr/bin/env python3
"""Sync Party deploy & dashboard access scripts.

Usage:
  python3 deploy.py deploy                # Trigger deploy on Render
  python3 deploy.py logs [n]              # Pull logs from /admin/logs
  python3 deploy.py dashboard             # Open dashboard URL
  python3 deploy.py test                  # Run full test suite
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import ssl
import subprocess

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Config ─────────────────────────────────────────────────────
RENDER_TOKEN = open(os.path.expanduser("~/.hermes/render_token")).read().strip()
SERVICE_ID = "srv-d8a7iiml51nc73chvc5g"
SUPERADMIN_PWD = os.environ.get("SUPER_ADMIN_PWD", "changeme_superadmin")
URL = os.environ.get("SYNC_PARTY_URL", "https://sync-party.onrender.com")

# ── Deploy ─────────────────────────────────────────────────────

def trigger_deploy():
    print(f"🚀 Triggering deploy for {SERVICE_ID}...")
    req = urllib.request.Request(
        f"https://api.render.com/v1/services/{SERVICE_ID}/deploys",
        method="POST",
        data=json.dumps({"clearCache": "clear"}).encode(),
        headers={
            "Authorization": f"Bearer {RENDER_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=30)
    data = json.loads(resp.read())
    deploy_id = data.get("id", "??")
    status = data.get("status", "??")
    print(f"✅ Deploy triggered — id={deploy_id} status={status}")
    
    # Poll until complete
    for i in range(30):
        time.sleep(10)
        req2 = urllib.request.Request(
            f"https://api.render.com/v1/services/{SERVICE_ID}/deploys/{deploy_id}",
            headers={"Authorization": f"Bearer {RENDER_TOKEN}"},
        )
        try:
            resp2 = urllib.request.urlopen(req2, context=SSL_CTX, timeout=15)
            deploy_data = json.loads(resp2.read())
            s = deploy_data.get("status", "created")
            print(f"  [{i+1}0s] {s}")
            if s in ("live", "deactivated"):
                print("✅ Deploy complete!")
                return True
            if s == "build_failed":
                print("❌ Build failed")
                return False
        except Exception as e:
            print(f"  ⚠️ Poll error: {e}")
    print("⚠️ Timeout — deploy may still be in progress")


def check_health(timeout=120):
    """Wait until /health returns 200."""
    print(f"⏳ Waiting for {URL}/health...")
    for i in range(timeout // 5):
        try:
            req = urllib.request.Request(f"{URL}/health")
            resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=10)
            if resp.code == 200:
                data = json.loads(resp.read())
                print(f"✅ Healthy — {data.get('rooms', 0)} rooms")
                return True
        except Exception:
            pass
        time.sleep(5)
    print("❌ Timeout waiting for health")
    return False


# ── Dashboard / Logs ───────────────────────────────────────────

def fetch_logs(n=100, level=""):
    """Pull logs from /admin/logs endpoint."""
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    
    # Step 1: Login as superadmin
    import urllib.parse
    data = f"password={urllib.parse.quote(SUPERADMIN_PWD)}"
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=SSL_CTX),
    )
    req = urllib.request.Request(
        f"{URL}/admin/login", method="POST",
        data=data.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = opener.open(req, timeout=15)
    if resp.code != 303:
        print(f"❌ Login failed: {resp.code}")
        return
    
    # Step 2: Fetch logs
    log_url = f"{URL}/admin/logs?n={n}"
    if level:
        log_url += f"&level={level}"
    req2 = urllib.request.Request(log_url)
    resp2 = opener.open(req2, timeout=15)
    logs = resp2.read().decode("utf-8", errors="replace")
    print(logs)


def open_dashboard():
    """Print dashboard URLs."""
    print(f"""
╔══════════════════════════════════════════════════════════╗
║  Sync Party Dashboard                                   ║
╠══════════════════════════════════════════════════════════╣
║  Super-admin:  {URL}/admin               
║  Logs:         {URL}/admin/logs          
║  Health:       {URL}/health              
║  Providers:    {URL}/providers           
╚══════════════════════════════════════════════════════════╝

Mot de passe super-admin: {'défini' if SUPERADMIN_PWD != 'changeme_superadmin' else '⚠️ par défaut'}
Token Render: {'configuré' if RENDER_TOKEN else '❌ absent'}
""")

# ── Main ───────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dashboard"
    
    if cmd == "deploy":
        if trigger_deploy():
            check_health()
    
    elif cmd == "deploy-full":
        if trigger_deploy():
            time.sleep(5)
            if check_health():
                print("\n🧪 Running test suite...")
                subprocess.run([sys.executable, __file__.replace("deploy.py", "test_sync_party.py"),
                                "--url", URL, "--superadmin-pwd", SUPERADMIN_PWD])
    
    elif cmd == "logs":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        level = sys.argv[3] if len(sys.argv) > 3 else ""
        fetch_logs(n, level)
    
    elif cmd == "dashboard":
        open_dashboard()
    
    elif cmd == "test":
        subprocess.run([sys.executable, __file__.replace("deploy.py", "test_sync_party.py"),
                        "--url", URL, "--superadmin-pwd", SUPERADMIN_PWD])
    
    else:
        print(f"Usage: {sys.argv[0]} deploy|deploy-full|logs|dashboard|test")
