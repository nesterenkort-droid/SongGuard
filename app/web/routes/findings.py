"""Findings dashboard: the feed, per-finding actions, whitelist-in-one-click, scan.

This is the M2 payoff surface (PLAN.md §10): a filterable feed where every finding
shows its explainable signal breakdown and can be confirmed / dismissed / tolerated,
or turned into a whitelist rule in one click. The scan button enqueues an arq job.
"""

from urllib.parse import quote

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
    STATUS_COUNTER_NOTICED,
    STATUS_DETECTED,
    STATUS_DISMISSED,
    STATUS_PACKET_READY,
    STATUS_PENDING_REVIEW,
    STATUS_REAPPEARED,
    STATUS_REMIX_REVIEW,
    STATUS_REMOVED,
    STATUS_SENT,
    STATUS_STILL_ALIVE,
    STATUS_TOLERATED,
    Artist,
    ArtistMember,
    Finding,
    PlatformCandidate,
    TakedownPacket,
    Track,
    User,
)
from app.services import catalog, detection, takedown
from app.web.templating import render

router = APIRouter(prefix="/findings", tags=["findings"])

# Named status filters shown as tabs.
STATUS_GROUPS = {
    "open": [STATUS_DETECTED, STATUS_PENDING_REVIEW, STATUS_REMIX_REVIEW],
    "confirmed": [STATUS_CONFIRMED],
    "packets": [
        STATUS_PACKET_READY, STATUS_SENT, STATUS_STILL_ALIVE,
        STATUS_COUNTER_NOTICED, STATUS_REAPPEARED,
    ],
    "removed": [STATUS_REMOVED],
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
    STATUS_PACKET_READY: "пакет готов",
    STATUS_SENT: "отправлено",
    STATUS_STILL_ALIVE: "всё ещё жива",
    STATUS_COUNTER_NOTICED: "контр-нотис",
    STATUS_REAPPEARED: "вернулась",
    STATUS_REMOVED: "удалена 🎉",
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
        # Muted tracks' findings are hidden everywhere (reversible via unmute).
        .where(Track.is_muted.is_(False))
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

    # Counts per tab (+ total) for the badges — excludes muted tracks, same visibility
    # rules as the feed.
    from sqlalchemy import func

    count_stmt = (
        select(Finding.status, func.count())
        .select_from(Finding)
        .join(Track, Finding.track_id == Track.id)
        .where(Track.is_muted.is_(False))
        .group_by(Finding.status)
    )
    if not user.is_admin:
        count_stmt = (
            count_stmt.join(Artist, Track.primary_artist_id == Artist.id)
            .join(ArtistMember, ArtistMember.artist_id == Artist.id)
            .where(ArtistMember.user_id == user.id)
        )
    status_counts = dict((await session.execute(count_stmt)).all())
    tab_counts = {
        tab: sum(status_counts.get(s, 0) for s in statuses)
        for tab, statuses in STATUS_GROUPS.items()
    }
    total_findings = sum(status_counts.values())

    # Geo-block map: a YouTube video can show "unavailable" in the artist's country
    # while being public everywhere else (region restriction) — that is NOT a takedown.
    # Flag it so the card can say so instead of the user assuming it's gone.
    home = settings.home_country_code
    geo_blocks: dict[int, dict] = {}
    for _f, cand, _t, _a in rows:
        if cand.platform != "youtube" or not cand.raw_json:
            continue
        rr = (cand.raw_json.get("contentDetails") or {}).get("regionRestriction") or {}
        allowed, blocked = rr.get("allowed"), rr.get("blocked")
        if allowed is not None and home not in allowed:
            geo_blocks[cand.id] = {"allowed_count": len(allowed)}
        elif blocked is not None and home in blocked:
            geo_blocks[cand.id] = {"allowed_count": None}

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
            "geo_blocks": geo_blocks,
            "home_country": home,
            "tab_counts": tab_counts,
            "total_findings": total_findings,
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


@router.get("/{finding_id}/packet")
async def packet_page(
    request: Request,
    finding_id: int,
    error: str | None = None,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    finding = await _load_finding_checked(session, user, finding_id)
    if finding is None:
        return render(request, "404.html", user=user, status_code=404)
    ctx = await detection.get_finding_context(session, finding_id)
    _finding, cand, track, artist = ctx
    packets = list(
        await session.scalars(
            select(TakedownPacket)
            .where(TakedownPacket.finding_id == finding_id)
            .order_by(TakedownPacket.created_at.desc())
        )
    )
    routes = takedown.available_routes(cand)
    warning = await takedown.collab_warning(session, track)
    return render(
        request,
        "packet.html",
        {
            "finding": finding, "cand": cand, "track": track, "artist": artist,
            "packets": packets, "routes": routes, "route_labels": takedown.ROUTE_LABELS_RU,
            "collab_warning": warning, "error": error,
        },
        user=user,
    )


@router.post("/{finding_id}/packet")
async def packet_generate(
    finding_id: int,
    route: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    finding = await _load_finding_checked(session, user, finding_id)
    if finding is None:
        return RedirectResponse("/findings", status_code=303)
    try:
        await takedown.generate_packet(session, finding, route, actor_user=user)
        await session.commit()
    except (takedown.LegalFieldsMissing, takedown.RouteNotApplicable, ValueError) as exc:
        return RedirectResponse(
            f"/findings/{finding_id}/packet?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/findings/{finding_id}/packet", status_code=303)


@router.post("/{finding_id}/packet/{packet_id}/sent")
async def packet_mark_sent(
    finding_id: int,
    packet_id: int,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    finding = await _load_finding_checked(session, user, finding_id)
    packet = await session.get(TakedownPacket, packet_id)
    if finding is None or packet is None or packet.finding_id != finding_id:
        return RedirectResponse("/findings", status_code=303)
    await takedown.mark_sent(session, packet, actor_user_id=user.id)
    await session.commit()
    return RedirectResponse(f"/findings/{finding_id}/packet", status_code=303)


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
