"""Catalog routes: list artists, import, artist detail, pin, upload originals."""

import os

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_user
from app.config import settings
from app.db import get_session
from app.models import AUDIO_REF_FULL, Track, User
from app.services import audit, catalog, panako
from app.web.templating import render

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("")
async def catalog_list(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    artists = await catalog.list_artists_for_user(session, user)
    return render(request, "catalog_list.html", {"artists": artists}, user=user)


@router.post("/import")
async def catalog_import(
    request: Request,
    ref: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await catalog.import_artist_catalog(session, actor_user=user, ref=ref)
    except ValueError as exc:
        artists = await catalog.list_artists_for_user(session, user)
        return render(
            request,
            "catalog_list.html",
            {"artists": artists, "error": str(exc)},
            user=user,
        )
    return RedirectResponse(f"/catalog/artist/{result['artist_id']}", status_code=303)


@router.get("/artist/{artist_id}")
async def artist_detail(
    request: Request,
    artist_id: int,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    if not await catalog.user_can_access_artist(session, user, artist_id):
        return render(request, "403.html", user=user, status_code=403)
    from app.models import Artist

    artist = await session.get(Artist, artist_id)
    if artist is None:
        return render(request, "404.html", user=user, status_code=404)
    tracks = await catalog.get_artist_tracks(session, artist_id)
    # "Оригинал загружен" != "Panako реально проиндексировал его" — check the
    # actual fingerprint file so the badge doesn't lie about audio-match readiness.
    panako_indexed = {
        t.id for t in tracks
        if os.path.exists(os.path.join(panako.ORIGINALS_DIR, f"{t.id}_1.00.wav"))
    }
    return render(
        request,
        "artist.html",
        {"artist": artist, "tracks": tracks, "panako_indexed": panako_indexed},
        user=user,
    )


async def _load_track_checked(session, user, track_id) -> Track | None:
    track = await session.get(Track, track_id)
    if track is None:
        return None
    if not await catalog.user_can_access_artist(session, user, track.primary_artist_id):
        return None
    return track


@router.post("/track/{track_id}/pin")
async def toggle_pin(
    track_id: int,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    track = await _load_track_checked(session, user, track_id)
    if track is None:
        return RedirectResponse("/catalog", status_code=303)
    track.is_hot_pinned = not track.is_hot_pinned
    await audit.log(
        session,
        actor_user_id=user.id,
        action="track.pin",
        entity_type="track",
        entity_id=track.id,
        summary=f"{'Закреплён' if track.is_hot_pinned else 'Откреплён'} трек «{track.title}»",
    )
    await session.commit()
    return RedirectResponse(f"/catalog/artist/{track.primary_artist_id}", status_code=303)


@router.post("/track/{track_id}/upload")
async def upload_original(
    track_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    track = await _load_track_checked(session, user, track_id)
    if track is None:
        return RedirectResponse("/catalog", status_code=303)

    os.makedirs(settings.audio_dir, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1].lower() or ".bin"
    filename = f"track-{track.id}{ext}"
    dest = os.path.join(settings.audio_dir, filename)
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)

    track.original_audio_path = filename
    track.audio_ref_status = AUDIO_REF_FULL

    # Feed Panako so audio matching actually has something to compare against —
    # without this, uploads were saved but the fingerprint database stayed empty
    # and every candidate silently skipped the audio-match step. Best-effort: a
    # Panako/ffmpeg hiccup must never block the upload itself from succeeding.
    try:
        panako_ok = await panako.store_reference(track.id, dest)
    except Exception:  # noqa: BLE001
        panako_ok = False

    await audit.log(
        session,
        actor_user_id=user.id,
        action="track.upload_original",
        entity_type="track",
        entity_id=track.id,
        summary=f"Загружен оригинал для «{track.title}» ({len(content)} байт)",
        data={"filename": filename, "size": len(content), "panako_indexed": panako_ok},
    )
    await session.commit()
    return RedirectResponse(f"/catalog/artist/{track.primary_artist_id}", status_code=303)
