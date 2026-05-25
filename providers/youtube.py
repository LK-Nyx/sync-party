"""YouTube provider for Sync Party."""

import os
import pickle
import re
from typing import Optional

from providers.base import Provider, ProviderInfo, register


class YouTubeProvider:
    """YouTube Music/Video provider."""

    def __init__(self, token_dir: str):
        self._token_dir = token_dir

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="YouTube",
            key="youtube",
            icon="🎬",
            requires_auth=True,
            description="YouTube Music & Videos",
        )

    @property
    def _token_path(self) -> str:
        return os.path.join(self._token_dir, "token.pickle")

    @property
    def _client_secret_path(self) -> str:
        return os.path.join(self._token_dir, "client_secret.json")

    def _get_service(self):
        """Lazy-load YouTube API service."""
        from googleapiclient.discovery import build

        if not os.path.exists(self._token_path):
            return None

        with open(self._token_path, "rb") as f:
            creds = pickle.load(f)

        return build("youtube", "v3", credentials=creds)

    async def resolve_url(self, url: str) -> dict:
        """Parse a YouTube URL into playable format."""
        # Extract playlist ID
        m = re.search(r"[&?]list=([a-zA-Z0-9_-]+)", url)
        playlist_id = m.group(1) if m else None
        # Extract video ID
        m = re.search(r"(?:youtu\.be/|watch\?v=)([a-zA-Z0-9_-]+)", url)
        video_id = m.group(1) if m else None

        return {
            "provider": "youtube",
            "playlist_id": playlist_id,
            "video_id": video_id,
        }

    async def search(self, query: str, page_token: Optional[str] = None) -> list[dict]:
        """Search YouTube for videos."""
        yt = self._get_service()
        if not yt:
            return []

        resp = yt.search().list(
            q=query,
            part="id,snippet",
            type="video",
            maxResults=5,
            pageToken=page_token,
        ).execute()

        return [
            {
                "id": item["id"]["videoId"],
                "title": item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "url": f"https://youtube.com/watch?v={item['id']['videoId']}",
            }
            for item in resp.get("items", [])
        ]

    async def get_playlist_id_from_url(self, url: str) -> str:
        """Extract playlist ID from YouTube URL."""
        m = re.search(r"[&?]list=([a-zA-Z0-9_-]+)", url)
        if m:
            return m.group(1)
        # If it's already an ID (starts with PL)
        if re.match(r"^PL[a-zA-Z0-9_-]{16,}$", url):
            return url
        return url

    def player_vars(self, playlist_id: str) -> dict:
        """YouTube iframe player variables."""
        return {
            "listType": "playlist",
            "list": playlist_id,
            "autoplay": 0,
            "controls": 1,
        }


# Auto-register
register(YouTubeProvider(token_dir=os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))
