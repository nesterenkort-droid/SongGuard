"""Findings dashboard: the feed, per-finding actions, whitelist-in-one-click, scan.

This is the M2 payoff surface (PLAN.md §10): a filterable feed where every finding
shows its explainable signal breakdown and can be confirmed / dismissed / tolerated,
or turned into a whitelist rule in one click. The scan button enqueues an arq job.
"""

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_user
from app.config import settings
from app.db import get_session
from app.models import (
    STATUS_CONFIRMED,
    STATUS_DETECTED,
    STATUS_DISMISSED,
    STATUS_PENDING_REVIEW,
    STATUS_REMIX_REVIEW,
    STATUS_TOLERATED,
    Artist,
    ArtistMember,
    Finding,
    PlatformCandidate,
    Track,
    User,
)
from app.services import catalog, detection
from app.web.templating import render

router = APIRouter(prefix="/findings", tags=["findings"])

# Named status filters shown as tabs.
STATUS_GROUPS = {
    "open": [STATUS_DETECTED, STATUS_PENDING_REVIEW, STATUS_REMIX_REVIEW],
    "confirmed": [STATUS_CONFIRMED],
    "dismissed": [STATUS_DISMISSED],
    "tolerated": [STATUS_TOLERATED],
}

STATUS_LABELS = {
    STATUS_DETECTED: "новая",
    STATUS_PENDING_REVIEW: "на проверке",
    STATUS_REMIX_REVIEW: "ремикс?",
    STATUS_CONFIRMED: "пиратка",
    STATUS_DISMISSED: "ложное",
    STATUS_TOLERATED: "разрешено",
}


@router.get("")
async def findings_list(
    request: Request,
    status: str = "open",
    band: str | None = None,
    platform: str | None = None,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Finding, PlatformCandidate, Track, Artist)
        .join(PlatformCandidate, Finding.candidate_id == PlatformCandidate.id)
        .join(Track, Finding.track_id == Track.id)
        .join(Artist, Track.primary_artist_id == Artist.id)
        .order_by(desc(Finding.score), desc(Finding.created_at))
    )
    if not user.is_admin:
        stmt = stmt.join(ArtistMember, ArtistMember.artist_id == Artist.id).where(
            ArtistMember.user_id == user.id
        )
    if status in STATUS_GROUPS:
        stmt = stmt.where(Finding.status.in_(STATUS_GROUPS[status]))
    if band:
        stmt = stmt.where(Finding.band == band)
    if platform:
        stmt = stmt.where(PlatformCandidate.platform == platform)

    rows = (await session.execute(stmt.limit(200))).all()

    # Artists the user can scan (for the scan dropdown).
    artists = await catalog.list_artists_for_user(session, user)

    # Статистика квот, очередей и импорта каталогов
    from sqlalchemy import func

    from app.models import ScanJob
    from app.services.budgeter import get_remaining_youtube_searches
    from app.services.scheduler import get_import_progress

    # Получаем остаток квот YouTube
    remaining_youtube_searches = await get_remaining_youtube_searches()
    keys_count = len([k for k in (settings.youtube_api_key or "").split(",") if k.strip()])
    total_youtube_searches = keys_count * settings.youtube_search_quota_daily

    # Считаем количество задач в очереди
    pending_jobs = await session.scalar(
        select(func.count(ScanJob.id)).where(ScanJob.status == "pending")
    )
    running_jobs = await session.scalar(
        select(func.count(ScanJob.id)).where(ScanJob.status == "running")
    )

    # Прогресс импорта по каждому артисту пользователя
    import_progresses = []
    for artist, _ in artists:
        prog = await get_import_progress(session, artist.id)
        if prog["progress"] < 100.0:
            import_progresses.append({
                "artist_name": artist.name,
                "progress": prog["progress"],
                "scanned": prog["scanned"],
                "total": prog["total"],
                "eta_seconds": prog["eta_seconds"]
            })

    return render(
        request,
        "findings_list.html",
        {
            "rows": rows,
            "status": status,
            "band": band,
            "platform": platform,
            "artists": artists,
            "status_labels": STATUS_LABELS,
            "remaining_youtube_searches": remaining_youtube_searches,
            "total_youtube_searches": total_youtube_searches,
            "pending_jobs": pending_jobs,
            "running_jobs": running_jobs,
            "import_progresses": import_progresses,
        },
        user=user,
    )


async def _load_finding_checked(session, user, finding_id) -> Finding | None:
    finding = await session.get(Finding, finding_id)
    if finding is None:
        return None
    track = await session.get(Track, finding.track_id)
    if track is None or not await catalog.user_can_access_artist(
        session, user, track.primary_artist_id
    ):
        return None
    return finding


@router.post("/{finding_id}/action")
async def finding_action(
    finding_id: int,
    action: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    finding = await _load_finding_checked(session, user, finding_id)
    if finding is None:
        return RedirectResponse("/findings", status_code=303)
    try:
        await detection.transition(session, finding, action, actor_user_id=user.id)
        await session.commit()
    except ValueError:
        pass
    return RedirectResponse(_back(action), status_code=303)


@router.post("/{finding_id}/whitelist")
async def finding_whitelist(
    finding_id: int,
    entry_type: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    finding = await _load_finding_checked(session, user, finding_id)
    if finding is None:
        return RedirectResponse("/findings", status_code=303)
    await detection.add_whitelist_from_finding(
        session, finding, entry_type, actor_user_id=user.id
    )
    await session.commit()
    return RedirectResponse("/findings", status_code=303)


@router.post("/scan")
async def trigger_scan(
    artist_id: int = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    if not await catalog.user_can_access_artist(session, user, artist_id):
        return RedirectResponse("/findings", status_code=303)
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        await pool.enqueue_job("scan_catalog", artist_id, user.id)
    finally:
        await pool.aclose()
    return RedirectResponse("/findings?scan=queued", status_code=303)


def _back(action: str) -> str:
    # After confirming/dismissing, jump to the relevant tab so the user sees the move.
    return {
        "confirm": "/findings?status=confirmed",
        "dismiss": "/findings?status=dismissed",
        "tolerate": "/findings?status=tolerated",
    }.get(action, "/findings")
