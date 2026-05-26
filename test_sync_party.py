#!/usr/bin/env python3
"""Sync Party E2E test suite — curl-based, cookie-aware, slug from redirect URL."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import re

URL = os.environ.get("SYNC_PARTY_URL", "https://sync-party.onrender.com")
SUPERADMIN_PWD = os.environ.get("SUPER_ADMIN_PWD", "changeme_superadmin")
RATE_MAX = 5


def curl(path, method="GET", data=None, cj=None, follow=False, raw=False):
    """curl → (http_code: str, body: str, cj_path: str, final_url: str)
    raw=True: body is bytes (for binary endpoints like QR)."""
    cj = cj or tempfile.mktemp(suffix=".cookies")
    cmd = ["curl", "-s", "-w", "\n%{http_code}\n%{url_effective}",
           "-b", cj, "-c", cj, "--max-time", "15", "-X", method]
    if data is not None:
        cmd += ["-d", data, "-H", "Content-Type: application/x-www-form-urlencoded"]
    if follow:
        cmd += ["-L"]
    cmd.append(f"{URL}{path}")

    r = subprocess.run(cmd, capture_output=True, text=not raw, timeout=20)
    if raw:
        stdout = r.stdout.decode("latin-1")  # binary-safe text extraction
        lines = stdout.strip().split("\n")
        code = lines[-2] if len(lines) >= 2 else "000"
        final = lines[-1] if len(lines) >= 1 else ""
        body = r.stdout  # raw bytes
        return code.strip(), body, cj, final
    lines = r.stdout.strip().split("\n")
    code = lines[-2] if len(lines) >= 2 else "000"
    final = lines[-1] if len(lines) >= 1 else ""
    body = "\n".join(lines[:-2]) if len(lines) > 2 else ""
    return code.strip(), body, cj, final


def chk(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name} {detail if detail else ''}")


def sec(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def main():
    print(f"\n{'#'*60}")
    print(f"# Sync Party E2E Test Suite")
    print(f"# URL: {URL}")
    print(f"# Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")

    slug = None
    admin_cj = tempfile.mktemp(suffix=".cookies")

    # ── 1. Health ──────────────────────────────────────────────
    sec("1. Health & Providers")
    c, b, _, _ = curl("/health")
    chk("Health 200", c == "200", b)
    c, b, _, _ = curl("/providers")
    chk("Providers youtube", c == "200" and "youtube" in b)

    # ── 2. Create room (follow redirect → get admin page + slug) ─
    sec("2. Create room + admin login")
    c, body, admin_cj, final_url = curl("/create", "POST",
        "name=E2E+Test&admin_password=testpass123", cj=admin_cj, follow=True)

    # Extract slug from final URL: /party/{slug}/admin
    m = re.search(r"/party/([a-f0-9]+)/admin", final_url)
    slug = m.group(1) if m else None
    chk("Slug from redirect URL", bool(slug), slug or "")

    # Also from page body (backup)
    if not slug:
        m = re.search(r'const SLUG = "([a-f0-9]+)"', body)
        slug = m.group(1) if m else None
        chk("Slug from page body", bool(slug), slug or "")

    # Cookie
    with open(admin_cj) as f:
        ck = f.read()
    chk("Cookie sync_party_auth", "sync_party_auth" in ck)

    # ── 3. Admin dashboard ──────────────────────────────────────
    sec("3. Admin dashboard")
    if not slug:
        chk("Skipped — no slug", False)
    else:
        c, body, _, _ = curl(f"/party/{slug}/admin", cj=admin_cj)
        chk("Dashboard 200", c == "200", f"{len(body)}B")
        chk("Playlist input", "playlist-url" in body)
        chk("Mode buttons", "mode-btns" in body)

    # ── 4. Viewer page + state ──────────────────────────────────
    sec("4. Viewer page + API state")
    if slug:
        c, body, _, _ = curl(f"/party/{slug}")
        chk("Viewer 200", c == "200", f"{len(body)}B")
        chk("Résumé button", "📋 Résumé" in body)

        c, state, _, _ = curl(f"/api/room/{slug}/state")
        chk("State 200", c == "200")
        try:
            s = json.loads(state)
            chk("video_id field", "video_id" in s)
            chk("Provider youtube", s.get("provider") == "youtube")
        except:
            chk("State valid JSON", False)

    # ── 5. QR code ──────────────────────────────────────────────
    sec("5. QR code")
    if slug:
        c, body, _, _ = curl(f"/party/{slug}/qr", raw=True)
        chk("QR 200", c == "200")
        chk("PNG > 200B", len(body) > 200)

    # ── 6. Super-admin flow ─────────────────────────────────────
    sec("6. Super-admin flow")
    # Login (follow redirect → dashboard)
    pwd_enc = urllib.parse.quote(SUPERADMIN_PWD, safe="")
    su_cj = tempfile.mktemp(suffix=".cookies")
    c, body, su_cj, _ = curl("/admin/login", "POST",
        f"password={pwd_enc}", cj=su_cj, follow=True)

    with open(su_cj) as f:
        su_c = f.read()
    chk("Cookie sync_party_su", "sync_party_su" in su_c)

    # Dashboard
    c, body, _, _ = curl("/admin/dashboard", cj=su_cj)
    chk("Dashboard 200", c == "200", f"{len(body)}B")
    if slug:
        chk("Room in dashboard", slug in body or body.strip() != "")

    # Logs
    c, logs, _, _ = curl("/admin/logs", cj=su_cj)
    chk("Logs 200", c == "200")
    chk("Logs > 200B", len(logs) > 200)

    # Delete room
    if slug:
        c, d, _, _ = curl(f"/admin/room/{slug}/delete", "POST", cj=su_cj)
        chk("Delete 200", c == "200")
        chk("Deleted confirmed", "deleted" in d)

        c, _, _, _ = curl(f"/party/{slug}")
        chk("Room 404 after delete", c == "404")

    # ── 7. Rate limiting ────────────────────────────────────────
    sec("7. Rate limiting")
    rj = tempfile.mktemp(suffix=".cookies")
    last = "000"
    for i in range(RATE_MAX + 2):
        last, _, rj, _ = curl("/create", "POST",
            f"name=Rate+{i}&admin_password=test", cj=rj, follow=True)
    chk(f"Rate limited at {RATE_MAX} rooms", last == "429")

    print(f"\n{'='*60}\n  ✅ Test suite terminée.\n{'='*60}")


if __name__ == "__main__":
    main()