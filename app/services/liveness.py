"""Is a platform candidate still live? Used by the recheck cycle (PLAN.md §7, §11).

Three outcomes, deliberately not just a bool: `True` (still up), `False` (gone —
confirms a takedown worked), `None` (couldn't tell — network hiccup, unconfigured
platform, unexpected response). Only a definite `False` should ever flip a finding
to "removed"; `None` must never be mistaken for it.
"""

import httpx

from app.importers import itunes as itunes_importer
from app.importers.spotify import SpotifyNotConfigured, _get_token
from app.models import PLATFORM_ITUNES, PLATFORM_SPOTIFY, PLATFORM_YOUTUBE
from app.scanners.youtube_scan import _fetch_video_details, _get_youtube_api_key


async def _spotify_alive(native_id: str) -> bool | None:
    try:
        async with httpx.AsyncClient() as client:
            token = await _get_token(client)
            resp = await client.get(
                f"https://api.spotify.com/v1/tracks/{native_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
    except SpotifyNotConfigured:
        return None
    except Exception:  # noqa: BLE001 - liveness checks must degrade to "unknown"
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    return None


async def _itunes_alive(native_id: str) -> bool | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                itunes_importer.LOOKUP_URL, params={"id": native_id}, timeout=20
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    return bool(data.get("results"))


async def _youtube_alive(native_id: str) -> bool | None:
    """`_fetch_video_details` silently omits ids YouTube doesn't return an item for
    (deleted/private) — so absence from the result *is* "removed", not "unknown".
    But that only holds if a key was actually used; no key also yields `{}`, which
    must stay "unknown", not be mistaken for a takedown.
    """
    if not await _get_youtube_api_key():
        return None
    try:
        async with httpx.AsyncClient() as client:
            details = await _fetch_video_details(client, [native_id])
    except Exception:  # noqa: BLE001 - liveness checks must degrade to "unknown"
        return None
    return native_id in details


_CHECKERS = {
    PLATFORM_SPOTIFY: _spotify_alive,
    PLATFORM_ITUNES: _itunes_alive,
    PLATFORM_YOUTUBE: _youtube_alive,
}


async def check_alive(platform: str, native_id: str) -> bool | None:
    checker = _CHECKERS.get(platform)
    if checker is None:
        return None
    return await checker(native_id)
