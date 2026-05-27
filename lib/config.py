"""Sync Party — configuration constants loaded from environment."""

import os
import secrets
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent

# Server
SERVER_SECRET = secrets.token_hex(32)
SUPER_ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PWD", "changeme_superadmin")
ROOM_TTL = int(os.environ.get("ROOM_TTL", "7200"))
MAX_ROOMS = int(os.environ.get("MAX_ROOMS", "50"))

# Rate limiting
RATE_WINDOW = 60
RATE_MAX_CREATE = 5
RATE_MAX_LOGIN = 15

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_RING_SIZE = int(os.environ.get("LOG_RING_SIZE", "500"))

# App
APP_TITLE = "Sync Party"
TEMPLATES_DIR = BASE_DIR / "templates"
