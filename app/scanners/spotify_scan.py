"""Spotify scanners: artist-page diff (Tier 0) and track search (Tier 1).

Tier 0 is the cheapest and strongest signal (PLAN.md §7): a pirate who keeps the
artist name usually lands on the *same* Spotify artist page, so any album on
`/artists/{id}/albums` that isn't in our catalog is a high-prior candidate — caught
even if the title changed completely. We also read the album `label` and ℗ copyright,
which is where the DistroKid autolabel ("NNNN Records DK") shows up.

Parsing is split into pure functions (`parse_album`) so it can be tested with captured
JSON without hitting the network; the `scan_*` coroutines add the live API calls,
reusing the throttled client from app.importers.spotify.
"""

from dataclasses import dataclass
from datetime import date

import httpx

from app.config import settings
from app.importers.spotify import (
    _get,
    _get_token,
    _parse_release_date,
    _request,
)
from app.scanners.base import RawCandidate

PLATFORM = "spotify"


@dataclass
class ParsedAlbum:
    album_id: str
    name: str
    label: str | None
    plabel: str | None  # ℗ copyright text
    release_date: date | None
    cover_url: str | None
    tracks: list[dict]  # [{native_id, title, duration_ms, credit}]


def _plabel(album: dict) -> str | None:
    for c in album.get("copyrights") or []:
        if c.get("type") == "P" and c.get("text"):
            return c["text"]
    # Fall back to any copyright text.
    for c in album.get("copyrights") or []:
        if c.get("text"):
            return c["text"]
    return None


def parse_album(album: dict) -> ParsedAlbum:
    """Pure parse of a Spotify full-album object into the facts we score on."""
    imgs = album.get("images") or []
    tracks = []
    for t in album.get("tracks", {}).get("items", []):
        tracks.append(
            {
                "native_id": t["id"],
                "title": t["name"],
                "duration_ms": t.get("duration_ms"),
                "credit": ", ".join(a["name"] for a in t.get("artists", [])),
            }
        )
    return ParsedAlbum(
        album_id=album["id"],
        name=album.get("name", ""),
        label=album.get("label"),
        plabel=_plabel(album),
        release_date=_parse_release_date(album.get("release_date")),
        cover_url=imgs[0]["url"] if imgs else None,
        tracks=tracks,
    )


def album_to_candidates(
    parsed: ParsedAlbum, known_track_ids: set[str]
) -> list[RawCandidate]:
    """Turn the unknown tracks of a parsed album into RawCandidates."""
    out: list[RawCandidate] = []
    for t in parsed.tracks:
        if t["native_id"] in known_track_ids:
            continue
        out.append(
            RawCandidate(
                platform=PLATFORM,
                native_id=t["native_id"],
                title=t["title"],
                url=f"https://open.spotify.com/track/{t['native_id']}",
                uploader=t["credit"],
                parsed_provider=parsed.label,
                parsed_plabel=parsed.plabel,
                published_at=parsed.release_date,
                duration_ms=t["duration_ms"],
                thumb_url=parsed.cover_url,
                cover_url=parsed.cover_url,
                raw_json={"album_id": parsed.album_id, "album_name": parsed.name},
            )
        )
    return out


async def _enrich_isrc(client: httpx.AsyncClient, token: str, cand: RawCandidate) -> None:
    """Fetch the track's ISRC (singular endpoint avoids the 403 batch issue)."""
    try:
        t = await _get(client, token, f"/tracks/{cand.native_id}")
        cand.isrc = (t.get("external_ids") or {}).get("isrc")
    except Exception:  # noqa: BLE001 - ISRC is best-effort enrichment
        pass


async def scan_artist_page(
    spotify_artist_id: str,
    known_track_ids: set[str],
    *,
    market: str = "US",
    enrich_isrc: bool = True,
) -> list[RawCandidate]:
    """Tier 0: diff the artist's Spotify albums against our known track ids."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_token(client)

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
            resp = await _request(client, "GET", nxt, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            data = resp.json()

        candidates: list[RawCandidate] = []
        for aid in album_ids:
            album = await _get(client, token, f"/albums/{aid}", {"market": market})
            parsed = parse_album(album)
            candidates.extend(album_to_candidates(parsed, known_track_ids))

        if enrich_isrc:
            for cand in candidates:
                await _enrich_isrc(client, token, cand)
        return candidates


async def search_tracks(query: str, *, market: str = "US", limit: int = 10) -> list[RawCandidate]:
    """Tier 1: a single combined-query track search."""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _get_token(client)
        data = await _get(
            client, token, "/search",
            {"q": query, "type": "track", "limit": limit, "market": market},
        )
        out: list[RawCandidate] = []
        for t in data.get("tracks", {}).get("items", []):
            album = t.get("album") or {}
            imgs = album.get("images") or []
            out.append(
                RawCandidate(
                    platform=PLATFORM,
                    native_id=t["id"],
                    title=t["name"],
                    url=f"https://open.spotify.com/track/{t['id']}",
                    uploader=", ".join(a["name"] for a in t.get("artists", [])),
                    isrc=(t.get("external_ids") or {}).get("isrc"),
                    published_at=_parse_release_date(album.get("release_date")),
                    duration_ms=t.get("duration_ms"),
                    thumb_url=imgs[0]["url"] if imgs else None,
                    cover_url=imgs[0]["url"] if imgs else None,
                    raw_json={"album_id": album.get("id"), "album_name": album.get("name")},
                )
            )
        return out
