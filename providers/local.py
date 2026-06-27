"""Local audio files provider for Sync Party — plays downloaded FLAC/MP3 files.

Architecture
~~~~~~~~~~~~
- Scans a configurable directory for audio files (FLAC, MP3, M4A, OGG, WAV).
- Indexes them by title + artist (parsed from filename or metadata).
- When a YouTube playlist URL is set, checks if local copies exist.
- If found → serves the local file via a static HTTP endpoint.
- If not found → returns empty result, letting the frontend fall back to YouTube.

Usage
~~~~~
Set LOCAL_MUSIC_DIR env var to point to your music folder.
The provider auto-registers as "local" in the provider registry.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from providers.base import ProviderInfo, register

# ── Config ─────────────────────────────────────────────────────
MUSIC_DIR = os.environ.get("LOCAL_MUSIC_DIR", os.path.expanduser("~/Music"))
CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "local_music_index.json")

# ── Index builder ──────────────────────────────────────────────

def _build_index() -> dict[str, dict]:
    """Scan MUSIC_DIR and build a searchable index of local audio files.

    Returns {normalized_title: {path, title, artist, duration, format}}
    """
    index = {}
    audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".opus"}

    if not os.path.isdir(MUSIC_DIR):
        return index

    for fpath in Path(MUSIC_DIR).rglob("*"):
        if fpath.suffix.lower() not in audio_exts:
            continue
        stem = fpath.stem  # filename without extension

        # Try to parse "Artist - Title" pattern
        artist, title = _parse_artist_title(stem)
        normalized = _normalize(title or stem)

        index[normalized] = {
            "path": str(fpath),
            "title": title or stem,
            "artist": artist or "Unknown",
            "format": fpath.suffix[1:].lower(),
            "size": fpath.stat().st_size,
        }

    return index


def _parse_artist_title(stem: str) -> tuple[Optional[str], Optional[str]]:
    """Try to extract Artist - Title from filename."""
    # Common patterns: "Artist - Title", "Artist – Title", "Artist — Title"
    m = re.match(r"^(.+?)\s*[–—-]\s*(.+)$", stem)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def _normalize(s: str) -> str:
    """Normalize a string for fuzzy matching: lowercase, strip accents, collapse spaces."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ── Provider ──────────────────────────────────────────────────

class LocalProvider:
    """Provider that serves audio files from a local directory.

    When a YouTube playlist URL is set, checks if local copies exist.
    The frontend can switch to "local" mode to play downloaded files.
    """

    def __init__(self):
        self._index: dict[str, dict] = {}
        self._last_scan: float = 0
        self._scan_interval = 60  # rescans every 60s

    def _ensure_index(self) -> dict[str, dict]:
        """Lazy-load or refresh the file index."""
        now = time.time()
        if now - self._last_scan > self._scan_interval:
            self._index = _build_index()
            self._last_scan = now
        return self._index

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="Local Files",
            key="local",
            icon="💿",
            requires_auth=False,
            description=f"Fichiers audio locaux ({MUSIC_DIR})",
        )

    @property
    def is_available(self) -> bool:
        return os.path.isdir(MUSIC_DIR)

    async def resolve_url(self, url: str) -> dict:
        """Resolve a URL to a local file if available.

        Returns a dict with provider='local' and file info, or
        provider='local' with empty result if not found locally.
        """
        index = self._ensure_index()
        if not index:
            return {"provider": "local", "found": False, "reason": "no_index"}

        # Extract video title from URL (YouTube video ID or title)
        # For now, try to match by the last segment of the URL
        title = _normalize(url.rsplit("/", 1)[-1].rsplit("?", 1)[0])

        # Direct match
        if title in index:
            entry = index[title]
            return {
                "provider": "local",
                "found": True,
                "path": entry["path"],
                "title": entry["title"],
                "artist": entry["artist"],
                "format": entry["format"],
            }

        # Fuzzy match: check if any indexed title contains the query or vice versa
        for norm, entry in index.items():
            if title in norm or norm in title:
                return {
                    "provider": "local",
                    "found": True,
                    "path": entry["path"],
                    "title": entry["title"],
                    "artist": entry["artist"],
                    "format": entry["format"],
                }

        return {"provider": "local", "found": False, "reason": "not_found"}

    async def search(self, query: str, page_token: Optional[str] = None) -> list[dict]:
        """Search local files by query string."""
        index = self._ensure_index()
        if not index:
            return []

        q = _normalize(query)
        results = []
        for norm, entry in index.items():
            if q in norm or norm in q:
                results.append({
                    "id": entry["path"],
                    "title": entry["title"],
                    "channel": entry["artist"],
                    "url": f"local://{entry['path']}",
                    "format": entry["format"],
                })
                if len(results) >= 10:
                    break
        return results

    async def get_playlist_id_from_url(self, url: str) -> str:
        """For local provider, the 'playlist ID' is just the directory path."""
        return MUSIC_DIR

    def player_vars(self, playlist_id: str) -> dict:
        """Return player variables for local playback."""
        return {
            "provider": "local",
            "music_dir": MUSIC_DIR,
            "file_count": len(self._ensure_index()),
        }

    def get_stats(self) -> dict:
        """Return statistics about the local music collection."""
        index = self._ensure_index()
        formats: dict[str, int] = {}
        for entry in index.values():
            fmt = entry["format"]
            formats[fmt] = formats.get(fmt, 0) + 1
        return {
            "total_files": len(index),
            "music_dir": MUSIC_DIR,
            "formats": formats,
        }


# Auto-register
register(LocalProvider())
