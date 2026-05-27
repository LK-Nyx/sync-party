"""Logging infrastructure — structured logger, ring buffer, request-ID middleware."""

import datetime
import logging
import sys
import time
import uuid

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from lib.config import LOG_LEVEL, LOG_RING_SIZE


class StructuredFormatter(logging.Formatter):
    """Emit structured log lines: ts=ISO8601 level=LVL rid=ID key=value msg=..."""

    def format(self, record: logging.LogRecord) -> str:
        now = datetime.datetime.utcnow()
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        fields = [f"ts={ts}", f"level={record.levelname}"]
        for attr in ("rid", "slug", "role", "ip", "method", "path", "status", "ms"):
            val = getattr(record, attr, None)
            if val is not None:
                fields.append(f"{attr}={val}")
        fields.append(f"msg={record.getMessage()}")
        return " ".join(fields)


# NOTE: Never use reserved LogRecord attribute names as keys in extra={}.
# Reserved names include: msg, name, args, created, relativeCreated,
# exc_info, exc_text, stack_info, lineno, funcName, pathname, thread,
# threadName, process, processName, levelname, levelno, message, msecs,
# taskName.
#
# Currently used extra keys: rid, slug, role, ip, method, path, status,
# ms, room_name, action, count, reason, dead, total, secure, has_token,
# slug_mode, viewers, url, target, by — all safe.

# In-memory ring buffer for the last N log messages
_log_ring: list[str] = []


class RingBufferHandler(logging.Handler):
    """Captures formatted log records into a fixed-size ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        _log_ring.append(self.format(record))
        if len(_log_ring) > LOG_RING_SIZE:
            _log_ring.pop(0)


def get_log_ring() -> list[str]:
    """Return the in-memory ring buffer (for /admin/logs endpoint)."""
    return _log_ring


def setup_logger(name: str = "sync-party") -> logging.Logger:
    """Configure and return the application logger with structured output and ring buffer."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Stream handler (stdout)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(StructuredFormatter())
    logger.handlers = [stream_handler]

    # Ring buffer handler
    ring_handler = RingBufferHandler()
    ring_handler.setFormatter(StructuredFormatter())
    logger.addHandler(ring_handler)

    return logger


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Inject a short request-ID (X-Request-ID header or random) and log every request."""

    def __init__(self, app, logger: logging.Logger):
        super().__init__(app)
        self._logger = logger

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
        request.state.rid = rid
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            try:
                self._logger.warning(
                    f"req method={request.method} path={request.url.path} status=500 ms={elapsed_ms} err={e}",
                    extra={
                        "rid": rid, "ip": request.client.host if request.client else "-",
                        "method": request.method, "path": request.url.path,
                        "status": 500, "ms": elapsed_ms,
                        "slug": request.path_params.get("slug", "-"),
                    },
                )
            except Exception:
                pass
            raise
        elapsed_ms = int((time.monotonic() - start) * 1000)
        try:
            self._logger.info(
                f"req method={request.method} path={request.url.path} status={response.status_code} ms={elapsed_ms}",
                extra={
                    "rid": rid, "ip": request.client.host if request.client else "-",
                    "method": request.method, "path": request.url.path,
                    "status": response.status_code, "ms": elapsed_ms,
                    "slug": request.path_params.get("slug", "-"),
                },
            )
        except Exception:
            pass
        return response
