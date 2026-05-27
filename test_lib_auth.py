"""Unit tests for lib/auth.py — token signing, verification, rate limiter, cookie helpers."""

import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Patch SERVER_SECRET to be deterministic for testing
import lib.config
lib.config.SERVER_SECRET = "test_secret_32_bytes_long!!!!!!"
lib.config.ROOM_TTL = 7200

from lib import auth
from fastapi.responses import RedirectResponse


def chk(name, ok, detail=""):
    status = "✅" if ok else "❌"
    print(f"  {status} {name} {detail}")
    if not ok:
        global FAILURES
        FAILURES += 1
        FAILURES_LIST.append(name)


FAILURES = 0
FAILURES_LIST = []


def test_sign_verify():
    """HMAC token sign + verify roundtrip."""

    token = auth.sign("my-room", "admin")
    chk("Token is non-empty", bool(token), token[:20])
    chk("Token format slug:role:ts:sig", len(token.split(":")) == 4, token)

    # Verify with correct slug+role
    chk("Verify correct slug+role", auth.verify(token, "my-room", "admin"))

    # Verify with 'any' role
    chk("Verify role=any", auth.verify(token, "my-room", "any"))

    # Verify with wrong slug
    chk("Verify wrong slug → False", not auth.verify(token, "other-room", "admin"))

    # Verify with wrong role
    chk("Verify wrong role → False", not auth.verify(token, "my-room", "superadmin"))

    # Forged token
    chk("Forged token → False", not auth.verify("fake:token:12345:xxxx", "my-room", "admin"))

    # Malformed token
    chk("Malformed token → False", not auth.verify("not-even-a-token", "my-room", "admin"))


def test_superadmin():
    """Super-admin token sign + verify."""

    token = auth.sign_superadmin()
    chk("Superadmin token non-empty", bool(token), token[:20])

    # Verify
    chk("Verify superadmin", auth.verify_superadmin(token))

    # Normal admin token should NOT verify as superadmin
    admin_token = auth.sign("my-room", "admin")
    chk("Admin token not superadmin", not auth.verify_superadmin(admin_token))

    # Forged superadmin
    chk("Forged superadmin → False", not auth.verify_superadmin("fake:superadmin:1:xxx"))


def test_ratelimit():
    """Sliding-window rate limiter."""

    # Allow up to 3 hits in a short window
    for i in range(3):
        allowed = auth.ratelimit(f"test_key:{i}", 3)
        chk(f"Ratelimiter allows hit {i+1}", allowed)

    # Over limit
    allowed = auth.ratelimit("test_key_over", 3)
    chk("Ratelimiter allows hit 1 of 3", allowed)
    allowed = auth.ratelimit("test_key_over", 3)
    chk("Ratelimiter allows hit 2 of 3", allowed)
    allowed = auth.ratelimit("test_key_over", 3)
    chk("Ratelimiter allows hit 3 of 3", allowed)
    allowed = auth.ratelimit("test_key_over", 3)
    chk("Ratelimiter blocks hit 4 of 3", not allowed)


def test_is_secure():
    """_is_secure reads X-Forwarded-Proto header."""

    class FakeRequest:
        headers = {}

    req = FakeRequest()
    chk("No header → not secure", not auth.is_secure(req))

    req.headers["X-Forwarded-Proto"] = "https"
    chk("https header → secure", auth.is_secure(req))

    req.headers["X-Forwarded-Proto"] = "http"
    chk("http header → not secure", not auth.is_secure(req))

    chk("None request → not secure", not auth.is_secure(None))


def test_set_auth_cookie():
    """set_auth_cookie sets httponly SameSite cookie."""

    resp = RedirectResponse("/", status_code=303)

    class FakeRequest:
        headers = {"X-Forwarded-Proto": "https"}
        state = type("obj", (object,), {"rid": "test_rid"})()

    auth.set_auth_cookie(resp, "test_cookie", "test_token",
                         request=FakeRequest(), rid="test_rid")
    # Verify it set a cookie header
    chk("Response has set-cookie header",
        bool(resp.headers.get("set-cookie", "")),
        resp.headers.get("set-cookie", "")[:50])


def main():
    print(f"\n{'='*60}")
    print("  Auth Unit Tests")
    print(f"{'='*60}")
    test_sign_verify()
    test_superadmin()
    test_ratelimit()
    test_is_secure()
    test_set_auth_cookie()
    print(f"\n{'='*60}")
    if FAILURES == 0:
        print("  ✅ All auth tests passed")
    else:
        print(f"  ❌ {FAILURES} test(s) FAILED:")
        for f in FAILURES_LIST:
            print(f"     - {f}")
    print(f"{'='*60}")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
