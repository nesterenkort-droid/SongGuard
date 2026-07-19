"""Importer types and artist-link parsing.

An importer turns a platform artist reference into an `ImportedArtist` with its
tracks. The catalog service (services/catalog.py) then upserts them into the DB.
"""

import re
from dataclasses import dataclass, field
from datetime import date


@dataclass
class ImportedTrack:
    title: str
    credit: str | None = None
    release_date: date | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    apple_track_id: int | None = None
    apple_collection_id: int | None = None
    spotify_track_id: str | None = None
    spotify_album_id: str | None = None
    cover_url: str | None = None
    preview_url: str | None = None
    source: str = "manual"


@dataclass
class ImportedArtist:
    name: str
    apple_artist_id: str | None = None
    spotify_artist_id: str | None = None
    tracks: list[ImportedTrack] = field(default_factory=list)


_APPLE_ARTIST_RE = re.compile(r"music\.apple\.com/[^/]+/artist/[^/]+/(\d+)")
_SPOTIFY_URL_RE = re.compile(r"open\.spotify\.com/artist/([A-Za-z0-9]+)")
_SPOTIFY_URI_RE = re.compile(r"spotify:artist:([A-Za-z0-9]+)")


def parse_artist_ref(ref: str) -> tuple[str, str]:
    """Return (platform, external_id) for a pasted artist link or bare id.

    Raises ValueError if the reference can't be understood.
    """
    ref = ref.strip()
    if m := _APPLE_ARTIST_RE.search(ref):
        return "itunes", m.group(1)
    if m := _SPOTIFY_URL_RE.search(ref):
        return "spotify", m.group(1)
    if m := _SPOTIFY_URI_RE.search(ref):
        return "spotify", m.group(1)
    if ref.isdigit():
        return "itunes", ref
    raise ValueError(
        "Не распознал ссылку. Вставьте ссылку на артиста в Apple Music или Spotify "
        "(или числовой Apple artist id)."
    )
