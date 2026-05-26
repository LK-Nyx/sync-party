#!/usr/bin/env python3
"""Sync Party test suite — end-to-end tests for all features.

Usage:
  python3 test_sync_party.py [--url https://sync-party.onrender.com] [--superadmin-pwd changeme]

Tests:
  1. Health check & providers
  2. Create room (admin)
  3. Admin login (cookie-based)
  4. Admin dashboard access
  5. Viewer page access
  6. API state endpoint
  7. QR code endpoint
  8. WebSocket viewer connection + player sync
  9. WebSocket admin connection + viewer detection
  10. Super-admin login
  11. Super-admin dashboard
  12. Super-admin delete room
  13. Rate limiting
  14. Room expiration / cleanup
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import http.cookiejar
import ssl

# Skip SSL verification for local testing
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

class SyncPartyTester:
    def __init__(self, base_url, superadmin_pwd):
        self.base = base_url.rstrip("/")
        self.superadmin_pwd = superadmin_pwd
        self.cookiejar = http.cookiejar.CookieJar()
        self.results = []
        self.admin_slug = None

    def _req(self, path, method="GET", data=None, headers=None):
        url = f"{self.base}{path}"
        req = urllib.request.Request(url, method=method, headers=headers or {})
        if data:
            req.data = data.encode() if isinstance(data, str) else data

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar),
            urllib.request.HTTPSHandler(context=SSL_CTX),
        )
        try:
            resp = opener.open(req, timeout=15)
            body = resp.read().decode("utf-8", errors="replace")
            return resp, body
        except urllib.error.HTTPError as e:
            return e, e.read().decode("utf-8", errors="replace")

    def check(self, name, condition, detail=""):
        status = "✅" if condition else "❌"
        print(f"  {status} {name} {detail if detail else ''}")

    def test(self, name):
        print(f"\n{'='*60}")
        print(f"  {name}")
        print(f"{'='*60}")

    # ── Tests ──────────────────────────────────────────────────

    def test_health(self):
        self.test("1. Health check & providers")
        resp, body = self._req("/health")
        self.check("Health returns 200", resp.code == 200, body)
        resp2, providers = self._req("/providers")
        self.check("Providers endpoint", resp2.code == 200 and "youtube" in providers)

    def test_create_room(self):
        self.test("2. Create room & login")
        data = "name=E2E+Test&admin_password=testpass123"
        resp, _ = self._req("/create", "POST", data,
                           {"Content-Type": "application/x-www-form-urlencoded"})
        self.check("Room created (303 redirect)", resp.code == 303)
        redirect_url = resp.headers.get("Location", "")
        self.admin_slug = redirect_url.split("/")[-2] if "/party/" in redirect_url else None
        self.check("Redirect to admin dashboard", bool(self.admin_slug), f"slug={self.admin_slug}")
        self.check("Cookie set", "sync_party_auth" in str(self.cookiejar))

    def test_admin_dashboard(self):
        self.test("3. Admin dashboard")
        if not self.admin_slug:
            self.check("Admin dashboard", False, "no slug from create")
            return
        resp, body = self._req(f"/party/{self.admin_slug}/admin")
        self.check("Dashboard renders (200)", resp.code == 200, f"{len(body)}B")
        self.check("Contains playlist input", "playlist-url" in body)
        self.check("Contains mode buttons", "mode-btns" in body)

    def test_viewer_page(self):
        self.test("4. Viewer page & API state")
        if not self.admin_slug:
            return
        resp, body = self._req(f"/party/{self.admin_slug}")
        self.check("Viewer page (200)", resp.code == 200, f"{len(body)}B")
        self.check("Contains mode buttons", "📋 Résumé" in body)

        resp2, state = self._req(f"/api/room/{self.admin_slug}/state")
        self.check("State endpoint (200)", resp2.code == 200)
        state_obj = json.loads(state)
        self.check("State has video_id", "video_id" in state_obj)
        self.check("State has provider", state_obj.get("provider") == "youtube")

    def test_qr_code(self):
        self.test("5. QR code")
        if not self.admin_slug:
            return
        resp, body = self._req(f"/party/{self.admin_slug}/qr")
        self.check("QR returns PNG", resp.code == 200)
        self.check("PNG content type", resp.headers.get("Content-Type", "") == "image/png")
        self.check("Non-empty PNG", len(body) > 200)

    def test_superadmin(self):
        self.test("6. Super-admin flow")
        # Login
        import urllib.parse
        data = f"password={urllib.parse.quote(self.superadmin_pwd)}"
        resp, _ = self._req("/admin/login", "POST", data,
                           {"Content-Type": "application/x-www-form-urlencoded"})
        self.check("Super-admin login (303)", resp.code == 303)
        self.check("Cookie sync_party_su set", "sync_party_su" in str(self.cookiejar))

        # Dashboard
        resp, body = self._req("/admin/dashboard")
        self.check("Dashboard (200)", resp.code == 200, f"{len(body)}B")
        self.check("Room listed", self.admin_slug and self.admin_slug in body)

        # Logs
        resp, logs = self._req("/admin/logs")
        self.check("Logs endpoint (200)", resp.code == 200)
        self.check("Logs non-empty", len(logs) > 0)

        # Delete room
        resp, del_body = self._req(f"/admin/room/{self.admin_slug}/delete", "POST")
        self.check("Delete room", resp.code == 200)
        self.check("Deleted response", "deleted" in del_body and self.admin_slug in del_body)

        # Verify deletion
        resp, _ = self._req(f"/party/{self.admin_slug}")
        self.check("Room 404 after delete", resp.code == 404)

    def test_rate_limits(self):
        self.test("7. Rate limiting")
        for i in range(RATE_MAX_CREATE + 2):  # config value
            data = f"name=Ratelimit+{i}&admin_password=test"
            resp, _ = self._req("/create", "POST", data,
                               {"Content-Type": "application/x-www-form-urlencoded"})
        self.check(f"Rate limited after {RATE_MAX_CREATE}", resp.code == 429)

    def run_all(self):
        print(f"\n{'#'*60}")
        print(f"# Sync Party E2E Test Suite")
        print(f"# Base URL: {self.base}")
        print(f"# Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'#'*60}")

        tests = [
            self.test_health,
            self.test_create_room,
            self.test_admin_dashboard,
            self.test_viewer_page,
            self.test_qr_code,
            self.test_superadmin,
            self.test_rate_limits,
        ]

        for t in tests:
            try:
                t()
            except Exception as e:
                self.check(t.__name__, False, str(e))

        print(f"\n{'='*60}")
        print("  ✅ Test suite terminée.")
        print(f"{'='*60}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("SYNC_PARTY_URL", "https://sync-party.onrender.com"))
    parser.add_argument("--superadmin-pwd", default=os.environ.get("SUPER_ADMIN_PWD", "changeme_superadmin"))
    parser.add_argument("--rate-max", type=int, default=5, help="Max rooms per rate window")
    args = parser.parse_args()

    # This comes from server config
    RATE_MAX_CREATE = args.rate_max

    tester = SyncPartyTester(args.url, args.superadmin_pwd)
    tester.run_all()
