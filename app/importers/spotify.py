"""Spotify Web API importer (Client Credentials), with rate-limit-aware throttling.

Pulls an artist's albums/singles and their tracks, including ISRC (via the full
track objects). Requires SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET; until those are
set this importer raises a clear error and the iTunes importer is used instead.

Rate limiting (see PLAN.md + Spotify docs): the API measures usage over a rolling
30-second window and returns 429 with a Retry-After header when exceeded. We space
requests by `spotify_min_interval_seconds`, always honor Retry-After, and publish a
short cooldown in Redis so every caller/worker backs off together. Batch endpoints
(/albums?ids=, /tracks?ids=) are used to minimize request count.

Note: preview_url was removed from the Spotify API for new apps (Nov 2024), so audio
previews come from iTunes, not here.
"""

import asyncio
import base64
import time
from datetime import date

import httpx

from app.config import settings
from app.importers.base import ImportedArtist, ImportedTrack
from app.redis_client import redis_client

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
_TOKEN_KEY = "spotify:token"
_COOLDOWN_KEY = "spotify:cooldown_until"

_throttle_lock = asyncio.Lock()
_last_request_monotonic = 0.0


class SpotifyNotConfigured(RuntimeError):
    pass


def _parse_release_date(raw: str | None) -> date | None:
    if not raw:
        return None
    parts = raw.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return date(year, month, day)
    except (ValueError, IndexError):
        return None


async def _respect_rate_limit() -> None:
    """Serialize + space out requests, and wait out any active cooldown."""
    global _last_request_monotonic
    async with _throttle_lock:
        cooldown = await redis_client.get(_COOLDOWN_KEY)
        if cooldown:
            wait = float(cooldown) - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 60))
        gap = settings.spotify_min_interval_seconds - (time.monotonic() - _last_request_monotonic)
        if gap > 0:
            await asyncio.sleep(gap)
        _last_request_monotonic = time.monotonic()


async def _request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs
) -> httpx.Response:
    """Throttled request that retries on 429, honoring Retry-After."""
    resp: httpx.Response | None = None
    for _ in range(settings.spotify_max_retries):
        await _respect_rate_limit()
        resp = await client.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        retry_after = int(resp.headers.get("Retry-After", "5"))
        # Publish a cooldown so concurrent callers back off too.
        await redis_client.set(
            _COOLDOWN_KEY, str(time.time() + retry_after), ex=retry_after + 5
        )
        await asyncio.sleep(retry_after)
    assert resp is not None
    return resp


async def _get_token(client: httpx.AsyncClient) -> str:
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        raise SpotifyNotConfigured(
            "Spotify не настроен: задайте SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET."
        )
    cached = await redis_client.get(_TOKEN_KEY)
    if cached:
        return cached
    auth = base64.b64encode(
        f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
    ).decode()
    resp = await _request(
        client,
        "POST",
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {auth}"},
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    await redis_client.set(_TOKEN_KEY, token, ex=max(60, payload.get("expires_in", 3600) - 60))
    return token


async def _get(
    client: httpx.AsyncClient, token: str, path: str, params: dict | None = None
) -> dict:
    resp = await _request(
        client, "GET", f"{API}{path}", params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        print(f"Spotify API Error response: {resp.text}")
        raise
    return resp.json()


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def import_artist(spotify_artist_id: str, *, market: str = "US") -> ImportedArtist:
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_token(client)

        artist = await _get(client, token, f"/artists/{spotify_artist_id}")
        artist_name = artist.get("name", f"spotify:{spotify_artist_id}")

        # Collect album ids (albums + singles), paginating via `next`.
        album_ids: list[str] = []
        params = {
            "include_groups": "album,single",
            "limit": settings.spotify_albums_page_limit,
            "market": market,
        }
        data = await _get(client, token, f"/artists/{spotify_artist_id}/albums", params)
        while True:
            album_ids.extend(a["id"] for a in data.get("items", []))
            nxt = data.get("next")
            if not nxt:
                break
            resp = await _request(
                client, "GET", nxt, headers={"Authorization": f"Bearer {token}"}
            )
            resp.raise_for_status()
            data = resp.json()

        # Full album objects (singular endpoint to avoid 403) -> cover, date, tracks.
        album_cover: dict[str, str | None] = {}
        album_release: dict[str, date | None] = {}
        track_stub: dict[str, dict] = {}
        for aid in album_ids:
            album = await _get(
                client, token, f"/albums/{aid}", {"market": market}
            )
            imgs = album.get("images") or []
            album_cover[aid] = imgs[0]["url"] if imgs else None
            album_release[aid] = _parse_release_date(album.get("release_date"))
            for t in album.get("tracks", {}).get("items", []):
                track_stub[t["id"]] = {
                    "name": t["name"],
                    "duration_ms": t.get("duration_ms"),
                    "album_id": aid,
                    "credit": ", ".join(a["name"] for a in t.get("artists", [])),
                }

        # Full track objects (singular endpoint to avoid 403 Forbidden) -> ISRC.
        isrc_by_track: dict[str, str | None] = {}
        for tid in track_stub.keys():
            t = await _get(
                client, token, f"/tracks/{tid}", {"market": market}
            )
            if t:
                isrc_by_track[t["id"]] = (t.get("external_ids") or {}).get("isrc")

        imported = ImportedArtist(
            name=artist_name, spotify_artist_id=spotify_artist_id, tracks=[]
        )
        for tid, stub in track_stub.items():
            aid = stub["album_id"]
            imported.tracks.append(
                ImportedTrack(
                    title=stub["name"],
                    credit=stub["credit"],
                    release_date=album_release.get(aid),
                    duration_ms=stub["duration_ms"],
                    isrc=isrc_by_track.get(tid),
                    spotify_track_id=tid,
                    spotify_album_id=aid,
                    cover_url=album_cover.get(aid),
                    source="spotify",
                )
            )
        return imported
