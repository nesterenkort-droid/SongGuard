"""Catalog service: apply imports and expose per-user catalog queries.

Importing upserts an Artist, ensures the acting user is a member, then upserts each
track (idempotent by platform id, so re-imports don't duplicate). Covers are fetched
and perceptually hashed best-effort. Collab tracks already owned by another artist
get an additional track_artists link instead of a duplicate row.
"""

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.importers import itunes, spotify
from app.importers.base import ImportedArtist, ImportedTrack, parse_artist_ref
from app.models import (
    ROLE_OWNER,
    Artist,
    ArtistMember,
    Track,
    TrackArtist,
    User,
)
from app.services import audit, images, normalize


async def _run_importer(platform: str, external_id: str) -> ImportedArtist:
    if platform == "itunes":
        return await itunes.import_artist(external_id)
    if platform == "spotify":
        return await spotify.import_artist(external_id)
    raise ValueError(f"Неизвестная платформа: {platform}")


async def _get_or_create_artist(
    session: AsyncSession, imported: ImportedArtist
) -> tuple[Artist, bool]:
    artist: Artist | None = None
    if imported.apple_artist_id:
        artist = await session.scalar(
            select(Artist).where(Artist.apple_artist_id == imported.apple_artist_id)
        )
    if artist is None and imported.spotify_artist_id:
        artist = await session.scalar(
            select(Artist).where(Artist.spotify_artist_id == imported.spotify_artist_id)
        )
    if artist is not None:
        if imported.apple_artist_id:
            artist.apple_artist_id = imported.apple_artist_id
        if imported.spotify_artist_id:
            artist.spotify_artist_id = imported.spotify_artist_id
        return artist, False
    artist = Artist(
        name=imported.name,
        apple_artist_id=imported.apple_artist_id,
        spotify_artist_id=imported.spotify_artist_id,
    )
    session.add(artist)
    await session.flush()
    return artist, True


async def _ensure_membership(session: AsyncSession, artist: Artist, user: User) -> None:
    exists = await session.scalar(
        select(ArtistMember).where(
            ArtistMember.artist_id == artist.id, ArtistMember.user_id == user.id
        )
    )
    if exists is None:
        session.add(ArtistMember(artist_id=artist.id, user_id=user.id, role=ROLE_OWNER))


async def _find_track(session: AsyncSession, it: ImportedTrack) -> Track | None:
    if it.apple_track_id:
        found = await session.scalar(
            select(Track).where(Track.apple_track_id == it.apple_track_id)
        )
        if found:
            return found
    if it.spotify_track_id:
        found = await session.scalar(
            select(Track).where(Track.spotify_track_id == it.spotify_track_id)
        )
        if found:
            return found
    return None


async def _attach_cover(client: httpx.AsyncClient, track: Track, cover_url: str) -> None:
    """Best-effort: download the cover, hash it, store the file. Never fails import."""
    try:
        content = await images.fetch(client, cover_url)
        phash, dhash = images.hash_bytes(content)
        filename = images.save_cover(content, settings.cover_dir, f"track-{track.id}")
        track.cover_path = filename
        track.cover_phash = phash
        track.cover_dhash = dhash
    except Exception:  # noqa: BLE001
        pass


async def import_artist_catalog(
    session: AsyncSession, *, actor_user: User, ref: str
) -> dict:
    platform, external_id = parse_artist_ref(ref)
    imported = await _run_importer(platform, external_id)
    return await apply_imported_artist(
        session,
        actor_user=actor_user,
        imported=imported,
        platform=platform,
        external_id=external_id,
    )


async def apply_imported_artist(
    session: AsyncSession,
    *,
    actor_user: User,
    imported: ImportedArtist,
    platform: str,
    external_id: str,
    download_covers: bool = True,
) -> dict:
    """Upsert an already-fetched ImportedArtist into the DB.

    Split out from import_artist_catalog so the fetch and the persistence can be
    exercised independently (e.g. offline verification with real captured data).
    `download_covers=False` skips the (network) cover fetch.
    """
    artist, _created = await _get_or_create_artist(session, imported)
    await _ensure_membership(session, artist, actor_user)

    created = updated = 0
    async with httpx.AsyncClient() as client:
        for it in imported.tracks:
            track = await _find_track(session, it)
            is_new = track is None
            if is_new:
                track = Track(primary_artist_id=artist.id)
                session.add(track)
                created += 1
            else:
                updated += 1

            is_variant, variant_label = normalize.detect_variant(it.title)
            track.title = it.title
            track.normalized_title = normalize.normalize_title(it.title)
            track.credit = it.credit
            track.release_date = it.release_date
            track.duration_ms = it.duration_ms
            track.isrc = it.isrc or track.isrc
            track.source = it.source
            track.is_variant = is_variant
            track.variant_label = variant_label
            track.preview_url = it.preview_url or track.preview_url
            track.cover_url = it.cover_url or track.cover_url
            if it.apple_track_id:
                track.apple_track_id = it.apple_track_id
            if it.apple_collection_id:
                track.apple_collection_id = it.apple_collection_id
            if it.spotify_track_id:
                track.spotify_track_id = it.spotify_track_id
            if it.spotify_album_id:
                track.spotify_album_id = it.spotify_album_id
            await session.flush()

            link = await session.scalar(
                select(TrackArtist).where(
                    TrackArtist.track_id == track.id, TrackArtist.artist_id == artist.id
                )
            )
            if link is None:
                session.add(TrackArtist(track_id=track.id, artist_id=artist.id))

            if download_covers and it.cover_url and (is_new or not track.cover_phash):
                await _attach_cover(client, track, it.cover_url)

    await audit.log(
        session,
        actor_user_id=actor_user.id,
        action="artist.import",
        entity_type="artist",
        entity_id=artist.id,
        summary=(
            f"Импорт каталога «{artist.name}» ({platform}): "
            f"добавлено {created}, обновлено {updated}"
        ),
        data={
            "platform": platform,
            "external_id": external_id,
            "created": created,
            "updated": updated,
        },
    )
    await session.commit()
    return {
        "artist_id": artist.id,
        "artist_name": artist.name,
        "created": created,
        "updated": updated,
        "total": len(imported.tracks),
        "platform": platform,
    }


# --- Query helpers -------------------------------------------------------------

async def list_artists_for_user(session: AsyncSession, user: User) -> list[tuple[Artist, int]]:
    """Return (artist, track_count) visible to the user (all if admin, else member)."""
    stmt = (
        select(Artist, func.count(Track.id))
        .outerjoin(Track, Track.primary_artist_id == Artist.id)
        .group_by(Artist.id)
        .order_by(Artist.name)
    )
    if not user.is_admin:
        stmt = stmt.join(ArtistMember, ArtistMember.artist_id == Artist.id).where(
            ArtistMember.user_id == user.id
        )
    rows = await session.execute(stmt)
    return [(a, c) for a, c in rows.all()]


async def user_can_access_artist(session: AsyncSession, user: User, artist_id: int) -> bool:
    if user.is_admin:
        return True
    member = await session.scalar(
        select(ArtistMember).where(
            ArtistMember.artist_id == artist_id, ArtistMember.user_id == user.id
        )
    )
    return member is not None


async def get_artist_tracks(session: AsyncSession, artist_id: int) -> list[Track]:
    rows = await session.scalars(
        select(Track)
        .where(Track.primary_artist_id == artist_id)
        .order_by(Track.release_date.desc().nullslast(), Track.title)
    )
    return list(rows)
