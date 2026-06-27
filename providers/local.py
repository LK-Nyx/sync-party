"""Local media provider for Sync Party — plays downloaded video/audio files.

Architecture
~~~~~~~~~~~~
- Scans a configurable directory for video (.mp4, .webm, .mkv) and audio files.
- Indexes by YouTube video ID (extracted from filename) AND normalized title.
- Priority matching: video ID first (exact), then normalized title (fuzzy).
- Serves files via HTTP Range requests for seekable video playback.
- Supports yt-dlp download directly from the admin UI.

Filename convention (yt-dlp output template):
    %(id)s-%(title)s.%(ext)s
    Example: dQw4w9WgXcQ-Never Gonna Give You Up.mp4

Usage
~~~~~
Set LOCAL_MEDIA_DIR env var to point to your media folder.
The provider auto-registers as "local" in the provider registry.
"""

import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Optional

from providers.base import ProviderInfo, register

# ── Config ─────────────────────────────────────────────────────
MEDIA_DIR = os.environ.get("LOCAL_MEDIA_DIR",
                os.environ.get("LOCAL_MUSIC_DIR",
                    os.path.expanduser("~/sync-party-media")))

# Supported extensions
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov"}
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".opus"}
ALL_EXTS = VIDEO_EXTS | AUDIO_EXTS

# Mood mapping from ID3 genre tags
GENRE_TO_MOOD = {
    # Calme
    "ambient": "calme", "classical": "calme", "lounge": "calme",
    "chillout": "calme", "downtempo": "calme", "new age": "calme",
    "meditation": "calme", "soundtrack": "calme", "instrumental": "calme",
    "piano": "calme", "acoustic": "calme",
    # Chill
    "lofi": "chill", "lo-fi": "chill", "trip-hop": "chill",
    "rnb": "chill", "r&b": "chill", "soul": "chill", "reggae": "chill",
    "jazz": "chill", "blues": "chill", "funk": "chill",
    "indie": "chill", "folk": "chill", "pop": "chill",
    "hip-hop": "chill", "rap": "chill",
    # Dansante
    "house": "dansante", "techno": "dansante", "disco": "dansante",
    "edm": "dansante", "drum and bass": "dansante", "dnb": "dansante",
    "dubstep": "dansante", "trance": "dansante", "electronic": "dansante",
    "electro": "dansante", "dance": "dansante", "club": "dansante",
    "hardstyle": "dansante", "garage": "dansante", "breakbeat": "dansante",
}


# ── Index builder ──────────────────────────────────────────────

def _extract_video_id(stem: str) -> Optional[str]:
    """Extract YouTube video ID from filename if it follows yt-dlp convention.

    yt-dlp --output "%(id)s-%(title)s.%(ext)s"
    → stem = "dQw4w9WgXcQ-Never Gonna Give You Up"
    → returns "dQw4w9WgXcQ"
    """
    m = re.match(r"^([a-zA-Z0-9_-]{11})-(.+)$", stem)
    if m:
        return m.group(1)
    return None


def _parse_artist_title(stem: str) -> tuple[Optional[str], Optional[str]]:
    """Try to extract Artist - Title from filename."""
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


def _guess_mood(filepath: str, title: str, artist: str) -> str:
    """Guess mood from filename keywords or ID3 tags (if mutagen available)."""
    # Try ID3 tags first
    try:
        import mutagen
        m = mutagen.File(filepath, easy=True)
        if m and "genre" in m:
            genre = str(m["genre"][0]).lower().strip()
            for key, mood in GENRE_TO_MOOD.items():
                if key in genre:
                    return mood
    except (ImportError, Exception):
        pass

    # Fallback: keywords in title/artist
    combined = f"{title} {artist}".lower()
    calm_keywords = {"ambient", "calm", "peaceful", "lullaby", "meditation",
                     "rain", "piano", "acoustic", "soft", "slow", "lofi"}
    chill_keywords = {"chill", "lofi", "soul", "rnb", "jazz", "blues",
                      "sunset", "vibes", "groove", "smooth", "mellow"}
    dance_keywords = {"dance", "remix", "club", "techno", "house", "edm",
                      "drop", "bass", "party", "night", "disco"}

    for kw in calm_keywords:
        if kw in combined:
            return "calme"
    for kw in chill_keywords:
        if kw in combined:
            return "chill"
    for kw in dance_keywords:
        if kw in combined:
            return "dansante"

    return "unknown"


def _build_index() -> dict:
    """Scan MEDIA_DIR and build a dual-index of local media files.

    Returns:
        by_id: {video_id: entry}  — exact YouTube ID match
        by_title: {normalized_title: entry}  — fuzzy title match
        all_entries: [entry, ...]  — full list for search/browse
    """
    by_id: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    all_entries: list[dict] = []

    if not os.path.isdir(MEDIA_DIR):
        return {"by_id": by_id, "by_title": by_title, "all": all_entries}

    for fpath in sorted(Path(MEDIA_DIR).rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if fpath.suffix.lower() not in ALL_EXTS:
            continue
        if fpath.name.startswith("."):
            continue

        stem = fpath.stem
        video_id = _extract_video_id(stem)
        artist, title = _parse_artist_title(stem)
        display_title = title or stem
        display_artist = artist or "Unknown"
        mood = _guess_mood(str(fpath), display_title, display_artist)
        is_video = fpath.suffix.lower() in VIDEO_EXTS
        mime_type, _ = mimetypes.guess_type(str(fpath))
        if not mime_type:
            mime_type = "video/mp4" if is_video else "audio/mpeg"

        entry = {
            "path": str(fpath),
            "title": display_title,
            "artist": display_artist,
            "video_id": video_id or "",
            "format": fpath.suffix[1:].lower(),
            "size": fpath.stat().st_size,
            "is_video": is_video,
            "mime": mime_type,
            "mood": mood,
        }

        all_entries.append(entry)

        # Index by video ID (exact match)
        if video_id:
            by_id[video_id] = entry

        # Index by normalized title (fuzzy match)
        norm = _normalize(display_title)
        if norm:
            by_title[norm] = entry

    return {"by_id": by_id, "by_title": by_title, "all": all_entries}


# ── Provider ──────────────────────────────────────────────────

class LocalProvider:
    """Provider that serves video/audio files from a local directory.

    Priority matching:
    1. YouTube video ID (exact, from filename)
    2. Normalized title (fuzzy)
    3. Fallback to YouTube stream
    """

    def __init__(self):
        self._index: dict = {}
        self._last_scan: float = 0
        self._scan_interval = 60

    def _ensure_index(self) -> dict:
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
            description=f"Fichiers locaux ({MEDIA_DIR})",
        )

    @property
    def is_available(self) -> bool:
        return os.path.isdir(MEDIA_DIR)

    async def resolve_url(self, url: str) -> dict:
        """Resolve a URL to a local file. Checks video ID first, then title."""
        idx = self._ensure_index()
        if not idx["by_id"] and not idx["by_title"]:
            return {"provider": "local", "found": False, "reason": "no_index"}

        # Step 1: Extract YouTube video ID from URL
        vid_match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
        video_id = vid_match.group(1) if vid_match else None

        # Step 2: Try exact video ID match
        if video_id and video_id in idx["by_id"]:
            entry = idx["by_id"][video_id]
            return {
                "provider": "local",
                "found": True,
                "match": "video_id",
                "path": entry["path"],
                "title": entry["title"],
                "artist": entry["artist"],
                "video_id": entry["video_id"],
                "format": entry["format"],
                "is_video": entry["is_video"],
                "mime": entry["mime"],
                "mood": entry["mood"],
                "size": entry["size"],
            }

        # Step 3: Try normalized title match
        title = _normalize(url.rsplit("/", 1)[-1].rsplit("?", 1)[0])
        if title in idx["by_title"]:
            entry = idx["by_title"][title]
            return {
                "provider": "local",
                "found": True,
                "match": "title",
                "path": entry["path"],
                "title": entry["title"],
                "artist": entry["artist"],
                "video_id": entry["video_id"],
                "format": entry["format"],
                "is_video": entry["is_video"],
                "mime": entry["mime"],
                "mood": entry["mood"],
                "size": entry["size"],
            }

        # Step 4: Fuzzy title match
        for norm, entry in idx["by_title"].items():
            if title in norm or norm in title:
                return {
                    "provider": "local",
                    "found": True,
                    "match": "fuzzy",
                    "path": entry["path"],
                    "title": entry["title"],
                    "artist": entry["artist"],
                    "video_id": entry["video_id"],
                    "format": entry["format"],
                    "is_video": entry["is_video"],
                    "mime": entry["mime"],
                    "mood": entry["mood"],
                    "size": entry["size"],
                }

        return {"provider": "local", "found": False, "reason": "not_found",
                "video_id": video_id or "", "title": title}

    async def search(self, query: str, page_token: Optional[str] = None) -> list[dict]:
        """Search local files by query string."""
        idx = self._ensure_index()
        if not idx["all"]:
            return []

        q = _normalize(query)
        results = []
        for entry in idx["all"]:
            norm = _normalize(f"{entry['title']} {entry['artist']}")
            if q in norm or norm in q:
                results.append({
                    "id": entry["path"],
                    "title": entry["title"],
                    "channel": entry["artist"],
                    "url": f"local://{entry['path']}",
                    "format": entry["format"],
                    "is_video": entry["is_video"],
                    "mood": entry["mood"],
                    "video_id": entry["video_id"],
                })
                if len(results) >= 20:
                    break
        return results

    async def get_playlist_id_from_url(self, url: str) -> str:
        return MEDIA_DIR

    def player_vars(self, playlist_id: str) -> dict:
        idx = self._ensure_index()
        return {
            "provider": "local",
            "media_dir": MEDIA_DIR,
            "file_count": len(idx["all"]),
        }

    def get_stats(self) -> dict:
        idx = self._ensure_index()
        formats: dict[str, int] = {}
        moods: dict[str, int] = {}
        video_count = 0
        audio_count = 0
        for entry in idx["all"]:
            fmt = entry["format"]
            formats[fmt] = formats.get(fmt, 0) + 1
            mood = entry["mood"]
            moods[mood] = moods.get(mood, 0) + 1
            if entry["is_video"]:
                video_count += 1
            else:
                audio_count += 1
        return {
            "total_files": len(idx["all"]),
            "media_dir": MEDIA_DIR,
            "video_count": video_count,
            "audio_count": audio_count,
            "formats": formats,
            "moods": moods,
            "by_id_count": len(idx["by_id"]),
        }

    def get_by_mood(self, mood: str) -> list[dict]:
        """Return all entries matching a given mood."""
        idx = self._ensure_index()
        return [e for e in idx["all"] if e["mood"] == mood]


# Auto-register
register(LocalProvider())
