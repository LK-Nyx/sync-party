"""Spotify provider for Sync Party."""

import os
from typing import Optional
from providers.base import ProviderInfo, register


class SpotifyProvider:
    """Spotify Music provider."""

    def __init__(self, credentials_path: Optional[str] = None):
        self._credentials_path = credentials_path
        self._sp = None

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="Spotify",
            key="spotify",
            icon="🎵",
            requires_auth=True,
            description="Spotify Music — playlists, albums, tracks",
        )

    @property
    def is_available(self) -> bool:
        return self._sp is not None or (self._credentials_path and os.path.exists(self._credentials_path))

    async def resolve_url(self, url: str) -> dict:
        """Parse a Spotify URL into playable format."""
        import re

        m = re.search(r"spotify\.com/(playlist|album|track)/([a-zA-Z0-9]+)", url)
        if not m:
            return {"provider": "spotify", "error": "Invalid URL"}

        kind = m.group(1)
        spotify_id = m.group(2)

        return {
            "provider": "spotify",
            "kind": kind,
            "spotify_id": spotify_id,
        }

    async def search(self, query: str, page_token: Optional[str] = None) -> list[dict]:
        """Search Spotify (stub — needs OAuth)."""
        return []

    async def get_playlist_id_from_url(self, url: str) -> str:
        import re
        m = re.search(r"spotify\.com/playlist/([a-zA-Z0-9]+)", url)
        return m.group(1) if m else url

    def player_vars(self, playlist_id: str) -> dict:
        return {"spotify_playlist_id": playlist_id}

    def setup_oauth(self, client_id: str, client_secret: str) -> str:
        """Generate OAuth URL for user authorization."""
        import urllib.parse

        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": "http://localhost:8888/callback",
            "scope": "playlist-read-private playlist-read-collaborative playlist-modify-public playlist-modify-private",
        }
        return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)


register(SpotifyProvider())
