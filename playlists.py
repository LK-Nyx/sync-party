"""Playlist data for Sync Party — curated playlists with metadata."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PlaylistTrack:
    title: str
    video_id: str
    artist: str = ""
    mood: str = "unknown"  # calme, chill, dansante
    duration_s: int = 0


@dataclass
class PlaylistDef:
    name: str
    description: str
    playlist_id: str
    url: str
    tracks: list = field(default_factory=list)


# ── Playlist "Soirée dansante" ────────────────────────────────
SOIREE_DANSANTE = PlaylistDef(
    name="Soirée dansante 🌴",
    description="Mix dansant pour l'apéro entre potes",
    playlist_id="PLACXdTD5v0FDkZJ6VA_yPEgnL1UYWH3YO",
    url="https://www.youtube.com/playlist?list=PLACXdTD5v0FDkZJ6VA_yPEgnL1UYWH3YO",
    tracks=[
        PlaylistTrack("O Malhao Malhao", "fTr3RqspAVE", "Linda De Suza", "dansante"),
        PlaylistTrack("Les lacs du Connemara", "bpEmjxobvbY", "Michel Sardou", "dansante"),
        PlaylistTrack("Hey Oh", "QyAUONxHmkE", "Tragédie", "dansante"),
        PlaylistTrack("Mambo No. 5", "EK_LN3XEcnw", "Lou Bega", "dansante"),
        PlaylistTrack("Ma vie au soleil", "TROMPkH6IA8", "Keen' V", "chill"),
    ],
)

# Registry
ALL_PLAYLISTS: dict[str, PlaylistDef] = {
    "soiree-dansante": SOIREE_DANSANTE,
}


def get_playlist(key: str) -> Optional[PlaylistDef]:
    return ALL_PLAYLISTS.get(key)


def list_playlists() -> list[dict]:
    return [
        {"key": k, "name": p.name, "description": p.description,
         "url": p.url, "track_count": len(p.tracks)}
        for k, p in ALL_PLAYLISTS.items()
    ]
