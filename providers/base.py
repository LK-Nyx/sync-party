"""Provider interface for Sync Party sources."""

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class ProviderInfo:
    """Metadata about a provider."""
    name: str
    key: str
    icon: str
    requires_auth: bool
    description: str


class Provider(Protocol):
    """Interface for all music/video providers."""

    @property
    def info(self) -> ProviderInfo:
        ...

    async def resolve_url(self, url: str) -> dict:
        """Convert a source URL/ID to playable content."""
        ...

    async def search(self, query: str, page_token: Optional[str] = None) -> list[dict]:
        """Search for tracks/playlists."""
        ...

    async def get_playlist_id_from_url(self, url: str) -> str:
        """Extract provider-native playlist ID from URL."""
        ...

    def player_vars(self, playlist_id: str) -> dict:
        """Return provider-specific player initialization variables."""
        ...


# Registry of all available providers
_providers: dict[str, Provider] = {}


def register(provider: Provider) -> None:
    _providers[provider.info.key] = provider


def get(key: str) -> Optional[Provider]:
    return _providers.get(key)


def list_all() -> list[ProviderInfo]:
    return [p.info for p in _providers.values()]
