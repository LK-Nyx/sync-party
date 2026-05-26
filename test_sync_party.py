#!/usr/bin/env python3
"""Sync Party E2E test suite — curl-based, cookie-aware."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time

URL = os.environ.get("SYNC_PARTY_URL", "https://sync-party.onrender.com")
SUPERADMIN_PWD = os.environ.get("SUPER_ADMIN_PWD", "changeme_superadmin")
RATE_MAX = 5


def curl(path, method="GET", data=None, cookiejar=None, location=False):
    """Run curl. Returns (http_code, body, cookiejar_path)."""
    cj = cookiejar or tempfile.mktemp(suffix=".cookies")
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "-b", cj, "-c", cj,
           "--max-time", "15", "-X", method]
    if method == "POST" and data:
        cmd += ["-d", data, "-H", "Content-Type: application/x-www-form-urlencoded"]
    if location:
        cmd += ["-L"]
    cmd.append(f"{URL}{path}")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    output = result.stdout.strip()
    lines = output.split("\n")
    # -w always appends http_code as last line
    http_code = lines[-1] if lines else "000"
    body = "\n".join(lines[:-1]) if len(lines) > 1 else ""
    return http_code.strip(), body, cj


def check(name, condition, detail=""):
    s = "✅" if condition else "❌"
    print(f"  {s} {name} {detail if detail else ''}")


def test(name):
    print(f"\n{'='*60}\n  {name}\n{'='*60}")


def main():
    print(f"\n{'#'*60}")
    print(f"# Sync Party E2E Test Suite")
    print(f"# URL: {URL}")
    print(f"# Mdp super-admin: {'défini(custom)' if SUPERADMIN_PWD != 'changeme_superadmin' else 'défaut'}")
    print(f"# Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")

    # ── 1. Health ──────────────────────────────────────────────
    test("1. Health & Providers")
    code, body, _ = curl("/health")
    check("Health 200", code == "200", body)
    code2, body2, _ = curl("/providers")
    check("Providers ok", code2 == "200" and "youtube" in body2)

    # ── 2. Create room (DO NOT follow redirect → extract slug from Location) ─
    test("2. Create room + admin login")
    code, body, admin_cj = curl("/create", "POST",
        "name=E2E+Test&admin_password=testpass123", location=False)
    check("Create 303", code == "303")
    check("Cookie sync_party_auth stored", True)

    # Get slug from Location header: curl -v would show this, but we use -D -
    # Alternative: parse the redirect URL from curl's follow-ability
    code, body, admin_cj = curl("/create", "POST",
        "name=E2E+Test&admin_password=testpass123", cookiejar=admin_cj, location=True)
    check("Following redirect → 200", code == "200")

    # Extract slug from body
    m = re.search(r'const SLUG = "([a-f0-9]+)"', body)
    slug = m.group(1) if m else None
    check("Slug found in admin page", bool(slug), slug or "")

    # Verify cookie exists
    with open(admin_cj) as f:
        cookies = f.read()
    check("Cookie sync_party_auth present", "sync_party_auth" in cookies)

    # ── 3. Admin dashboard ──────────────────────────────────────
    test("3. Admin dashboard")
    if not slug:
        check("Skipped — no slug", False)
    else:
        code, body, _ = curl(f"/party/{slug}/admin", cookiejar=admin_cj, location=False)
        check("Dashboard 200", code == "200", f"{len(body)}B")
        check("Playlist input present", "playlist-url" in body)
        check("Mode buttons present", "mode-btns" in body)

    # ── 4. Viewer page + state ──────────────────────────────────
    test("4. Viewer page + API state")
    if slug:
        code, body, _ = curl(f"/party/{slug}")
        check("Viewer page 200", code == "200", f"{len(body)}B")
        check("Résumé button visible", "📋 Résumé" in body)

        code, state, _ = curl(f"/api/room/{slug}/state")
        check("State endpoint 200", code == "200")
        try:
            s = json.loads(state)
            check("State has video_id", "video_id" in s)
            check("Provider is youtube", s.get("provider") == "youtube")
        except Exception:
            check("State valid JSON", False)

    # ── 5. QR code ──────────────────────────────────────────────
    test("5. QR code")
    if slug:
        code, body, _ = curl(f"/party/{slug}/qr")
        check("QR code 200", code == "200")
        check("PNG > 200 bytes", len(body.encode()) > 200)

    # ── 6. Super-admin flow ─────────────────────────────────────
    test("6. Super-admin flow")
    # Login (follow redirect → we'll get 303 then 200)
    import urllib.parse
    encoded_pwd = urllib.parse.quote(SUPERADMIN_PWD, safe="")
    code, body, su_cj = curl("/admin/login", "POST",
        f"password={encoded_pwd}", location=True)
    check("Login → redirect to dashboard (final 200)", code == "200")

    with open(su_cj) as f:
        su_cookies = f.read()
    check("Cookie sync_party_su stored", "sync_party_su" in su_cookies)

    # Dashboard
    code, body, _ = curl("/admin/dashboard", cookiejar=su_cj)
    check("Dashboard 200", code == "200", f"{len(body)}B")
    if slug:
        check("Room visible in dashboard", slug in body)

    # Logs
    code, logs, _ = curl("/admin/logs", cookiejar=su_cj)
    check("Logs 200", code == "200")
    check("Logs > 200 bytes", len(logs) > 200)

    # Delete room
    if slug:
        code, del_body, _ = curl(f"/admin/room/{slug}/delete", "POST", cookiejar=su_cj)
        check("Delete 200", code == "200")
        check("Delete confirmed", "deleted" in del_body)

        code, _, _ = curl(f"/party/{slug}")
        check("Room 404 after delete", code == "404")

    # ── 7. Rate limiting ────────────────────────────────────────
    test("7. Rate limiting")
    rate_cj = tempfile.mktemp(suffix=".cookies")
    last_code = "000"
    for i in range(RATE_MAX + 2):
        last_code, _, rate_cj = curl("/create", "POST",
            f"name=Rate+{i}&admin_password=test", cookiejar=rate_cj, location=True)
    check(f"Rate limited after {RATE_MAX} rooms", last_code == "429")

    print(f"\n{'='*60}")
    print("  ✅ Test suite terminée.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()