"""iTunes / Apple Music scanners: artist-page diff (Tier 0) and search (Tier 1).

iTunes Search is keyless but gives no ISRC or label; the ℗ label is scraped from the
Apple Music album web page best-effort (services/apple_label.py). One `lookup` returns
the whole discography, so the Tier 0 diff is a single cheap request.
"""

from datetime import date

import httpx

from app.importers.itunes import LOOKUP_URL, _upscale
from app.scanners.base import RawCandidate

PLATFORM = "itunes"
SEARCH_URL = "https://itunes.apple.com/search"


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _song_to_candidate(item: dict) -> RawCandidate | None:
    title = item.get("trackName") or item.get("trackCensoredName")
    track_id = item.get("trackId")
    if not title or not track_id:
        return None
    return RawCandidate(
        platform=PLATFORM,
        native_id=str(track_id),
        title=title,
        url=item.get("trackViewUrl") or item.get("collectionViewUrl"),
        uploader=item.get("artistName"),
        published_at=_parse_date(item.get("releaseDate")),
        duration_ms=item.get("trackTimeMillis"),
        thumb_url=_upscale(item.get("artworkUrl100")),
        cover_url=_upscale(item.get("artworkUrl100")),
        raw_json={
            "collection_id": item.get("collectionId"),
            "collection_view_url": item.get("collectionViewUrl"),
        },
    )


def parse_scan(data: dict, known_apple_ids: set[str]) -> list[RawCandidate]:
    """Pure: unknown songs in a lookup/search response become RawCandidates."""
    out: list[RawCandidate] = []
    for item in data.get("results", []):
        if item.get("wrapperType") == "artist":
            continue
        if item.get("kind") != "song" and item.get("wrapperType") != "track":
            continue
        if str(item.get("trackId")) in known_apple_ids:
            continue
        cand = _song_to_candidate(item)
        if cand:
            out.append(cand)
    return out


async def scan_artist_page(
    apple_artist_id: str,
    known_apple_ids: set[str],
    *,
    country: str = "us",
    limit: int = 200,
) -> list[RawCandidate]:
    """Tier 0: diff the artist's Apple discography against our known Apple track ids."""
    params = {"id": apple_artist_id, "entity": "song", "limit": limit, "country": country}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(LOOKUP_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    return parse_scan(data, known_apple_ids)


async def search_tracks(
    query: str, *, country: str = "us", limit: int = 15
) -> list[RawCandidate]:
    """Tier 1: term search (rotate markets at the call site for regional releases)."""
    params = {"term": query, "media": "music", "entity": "song", "limit": limit, "country": country}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    return parse_scan(data, set())
