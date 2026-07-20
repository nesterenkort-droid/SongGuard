"""iTunes Search API importer (keyless, free).

Uses the `lookup` endpoint with the Apple artist id to pull the artist's discography
(titles, dates, durations, cover art, 30s previews). No ISRC or label — those come
from Spotify. Rate limit is ~20 req/min/IP, but a single lookup returns the whole
catalog, so import is one request.
"""

import asyncio
from datetime import date

import httpx

from app.importers.base import ImportedArtist, ImportedTrack

LOOKUP_URL = "https://itunes.apple.com/lookup"


async def itunes_get(
    client: httpx.AsyncClient, url: str, params: dict, *, retries: int = 4
) -> httpx.Response:
    """GET an iTunes endpoint, retrying on 429 (Apple's ~20 req/min/IP limit) with a
    bounded exponential backoff honoring Retry-After. Other HTTP errors raise at once."""
    resp: httpx.Response | None = None
    delay = 3.0
    for attempt in range(retries):
        resp = await client.get(url, params=params, timeout=30)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        if attempt < retries - 1:
            wait = min(float(resp.headers.get("Retry-After", delay)), 30.0)
            await asyncio.sleep(wait)
            delay *= 2
    assert resp is not None
    resp.raise_for_status()  # exhausted retries -> surface the 429
    return resp


def _upscale(url: str | None) -> str | None:
    if not url:
        return None
    return url.replace("100x100bb.jpg", "600x600bb.jpg").replace(
        "100x100bb.png", "600x600bb.png"
    )


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_lookup(data: dict, apple_artist_id: str) -> ImportedArtist:
    """Parse a raw iTunes lookup response into an ImportedArtist (pure, testable)."""
    results = data.get("results", [])
    artist_name: str | None = None
    tracks: list[ImportedTrack] = []
    for item in results:
        if item.get("wrapperType") == "artist":
            artist_name = item.get("artistName")
            continue
        if item.get("kind") != "song" and item.get("wrapperType") != "track":
            continue
        title = item.get("trackName") or item.get("trackCensoredName")
        if not title:
            continue
        tracks.append(
            ImportedTrack(
                title=title,
                credit=item.get("artistName"),
                release_date=_parse_date(item.get("releaseDate")),
                duration_ms=item.get("trackTimeMillis"),
                apple_track_id=item.get("trackId"),
                apple_collection_id=item.get("collectionId"),
                cover_url=_upscale(item.get("artworkUrl100")),
                preview_url=item.get("previewUrl"),
                source="itunes",
            )
        )
    return ImportedArtist(
        name=artist_name or f"apple:{apple_artist_id}",
        apple_artist_id=str(apple_artist_id),
        tracks=tracks,
    )


async def import_artist(
    apple_artist_id: str, *, limit: int = 200, country: str = "us"
) -> ImportedArtist:
    params = {
        "id": apple_artist_id,
        "entity": "song",
        "limit": limit,
        "country": country,
    }
    async with httpx.AsyncClient() as client:
        resp = await itunes_get(client, LOOKUP_URL, params)
        data = resp.json()
    return parse_lookup(data, apple_artist_id)
