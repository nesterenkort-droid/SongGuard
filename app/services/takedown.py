"""Takedown packets: evidence capture + per-route complaint drafting (PLAN.md §11).

Two responsibilities kept deliberately separate:

* `capture_evidence` freezes what we know about a confirmed candidate — once the
  pirate release is taken down there is nothing left to point at, so this runs the
  moment a finding is confirmed (hooked from `detection.transition`), not later.
* `generate_packet` drafts the complaint text for one route. Generation is blocked
  without the filer's legal name/address/email (PLAN.md §11) — a DMCA-style notice
  without a real identity is not just weak, it can be legally void.

Nothing here ever sends anything. Every route note ends with an explicit reminder
that the destination address/form must be verified before use — PLAN.md itself
flags this ("⚠️ Актуальные URL форм проверить на этапе M6"), and a wrong address in
a real legal complaint is worse than no address, so we don't guess at specifics we
can't verify (only YouTube's copyright webform is a long-stable, well-known URL).
"""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    PACKET_SENT,
    PLATFORM_ITUNES,
    PLATFORM_SPOTIFY,
    PLATFORM_YOUTUBE,
    ROUTE_APPLE,
    ROUTE_DISTRIBUTOR,
    ROUTE_SPOTIFY,
    ROUTE_YOUTUBE,
    STATUS_PACKET_READY,
    STATUS_SENT,
    Artist,
    EvidenceArchive,
    Finding,
    FindingEvent,
    PlatformCandidate,
    TakedownPacket,
    Track,
    TrackArtist,
    User,
)
from app.services import audit, images

ROUTES = (ROUTE_DISTRIBUTOR, ROUTE_YOUTUBE, ROUTE_SPOTIFY, ROUTE_APPLE)

ROUTE_LABELS_RU = {
    ROUTE_DISTRIBUTOR: "Дистрибьютор (приоритетный маршрут)",
    ROUTE_YOUTUBE: "YouTube — форма авторских прав",
    ROUTE_SPOTIFY: "Spotify — форма удаления контента",
    ROUTE_APPLE: "Apple Music — контент-диспут по email",
}


class LegalFieldsMissing(ValueError):
    """Raised when the filer hasn't set legal_name/address/email (blocks generation)."""


class RouteNotApplicable(ValueError):
    """Raised when a route doesn't apply to this candidate (e.g. distributor for a
    candidate with no parsed distributor/label)."""


def available_routes(cand: PlatformCandidate) -> list[str]:
    """Which routes make sense for this candidate. Distributor only if we actually
    parsed a distributor/label name to complain to."""
    routes = []
    if cand.parsed_provider or cand.parsed_plabel:
        routes.append(ROUTE_DISTRIBUTOR)
    platform_route = {
        PLATFORM_YOUTUBE: ROUTE_YOUTUBE,
        PLATFORM_SPOTIFY: ROUTE_SPOTIFY,
        PLATFORM_ITUNES: ROUTE_APPLE,
    }.get(cand.platform)
    if platform_route:
        routes.append(platform_route)
    return routes


async def collab_warning(session: AsyncSession, track: Track) -> str | None:
    """PLAN.md §11: exactly one owner should file per release — a duplicate DMCA from
    two rights-holders invites a counter-notice. Warn (don't block) on collab tracks."""
    credits = list(
        await session.scalars(select(TrackArtist).where(TrackArtist.track_id == track.id))
    )
    if len(credits) <= 1:
        return None
    return (
        "⚠️ Это совместный релиз нескольких артистов. Жалобу должен подавать "
        "только ОДИН правообладатель — дубли от нескольких лиц выглядят подозрительно "
        "для площадки и повышают риск встречного уведомления (counter-notice). "
        "Согласуйте с соавторами, кто отправляет."
    )


def _route_contact_note(route: str, cand: PlatformCandidate) -> str:
    """Where to actually send it. Only a URL we're confident is long-stable is
    given outright; everything else is flagged for manual verification."""
    if route == ROUTE_YOUTUBE:
        return (
            "Отправить через официальную форму YouTube: "
            "https://www.youtube.com/copyright/manage-copyright/webform"
        )
    if route == ROUTE_SPOTIFY:
        return (
            "⚠️ ПРОВЕРЬТЕ АКТУАЛЬНЫЙ АДРЕС: форма/контакт для правообладателей Spotify "
            "(раздел Content Removal в Spotify for Artists / support.spotify.com)."
        )
    if route == ROUTE_APPLE:
        return (
            "⚠️ ПРОВЕРЬТЕ АКТУАЛЬНЫЙ АДРЕС: у Apple Music нет единой публичной формы "
            "для инди-правообладателей — контакт-диспут обычно через вашего дистрибьютора "
            "или email в Apple Legal/iTunes Notices."
        )
    name = cand.parsed_provider or cand.parsed_plabel or "дистрибьютор"
    return (
        f"⚠️ ПРОВЕРЬТЕ АКТУАЛЬНЫЙ АДРЕС: найдите официальную форму/email для жалоб "
        f"на нарушение авторских прав у «{name}» (обычно раздел Support → Copyright/DMCA "
        f"на их сайте) и отправьте текст ниже туда."
    )


def _body_en(
    *, route: str, user: User, artist: Artist, track: Track, cand: PlatformCandidate,
) -> str:
    filer = user.legal_name
    lines = [
        "COPYRIGHT INFRINGEMENT NOTICE",
        "",
        f"I, {filer}, am the rights holder (or authorized agent) for the sound "
        f"recording described below, credited to \"{artist.name}\".",
        "",
        "ORIGINAL WORK:",
        f'  Title: "{track.title}"',
        f"  Artist: {artist.name}",
    ]
    if track.isrc:
        lines.append(f"  ISRC: {track.isrc}")
    if track.release_date:
        lines.append(f"  Release date: {track.release_date.isoformat()}")
    lines += [
        "",
        "INFRINGING MATERIAL:",
        f"  URL: {cand.url or '(see platform id ' + cand.native_id + ')'}",
        f'  Title as listed: "{cand.title}"',
    ]
    if cand.uploader:
        lines.append(f"  Uploaded/credited to: {cand.uploader}")
    if cand.parsed_provider or cand.parsed_plabel:
        lines.append(f"  Provider/label shown: {cand.parsed_provider or cand.parsed_plabel}")
    lines += [
        "",
        "This upload was not authorized by the rights holder and infringes our "
        "copyright in the original sound recording. I have a good faith belief that "
        "use of the copyrighted material described above is not authorized by the "
        "copyright owner, its agent, or the law.",
        "",
        "I swear, under penalty of perjury, that the information in this notice is "
        "accurate and that I am the copyright owner or authorized to act on the "
        "owner's behalf.",
        "",
        "Rights holder / requesting party:",
        f"  Name: {user.legal_name}",
        f"  Address: {user.legal_address}",
        f"  Email: {user.legal_email}",
    ]
    return "\n".join(lines)


async def generate_packet(
    session: AsyncSession, finding: Finding, route: str, *, actor_user: User
) -> TakedownPacket:
    """Draft a takedown packet for one route. Raises LegalFieldsMissing /
    RouteNotApplicable instead of generating a broken/unusable complaint."""
    if not (actor_user.legal_name and actor_user.legal_address and actor_user.legal_email):
        raise LegalFieldsMissing(
            "Заполните юридические данные (имя, адрес, email) в настройках профиля — "
            "без них пакет жалобы не формируется."
        )
    if route not in ROUTES:
        raise ValueError(f"Неизвестный маршрут: {route}")

    cand = await session.get(PlatformCandidate, finding.candidate_id)
    track = await session.get(Track, finding.track_id)
    artist = await session.get(Artist, track.primary_artist_id)
    if route not in available_routes(cand):
        raise RouteNotApplicable(f"Маршрут «{ROUTE_LABELS_RU[route]}» неприменим к этой находке.")

    evidence = await capture_evidence(session, finding)

    body_en = _body_en(route=route, user=actor_user, artist=artist, track=track, cand=cand)
    warning = await collab_warning(session, track)
    note_parts = [
        f"Маршрут: {ROUTE_LABELS_RU[route]}.",
        _route_contact_note(route, cand),
        "Текст жалобы (на английском) сформирован ниже — отправьте его как есть "
        "или адаптируйте под конкретную форму площадки.",
    ]
    if warning:
        note_parts.insert(0, warning)
    note_ru = "\n\n".join(note_parts)

    packet = TakedownPacket(
        finding_id=finding.id,
        evidence_id=evidence.id,
        route=route,
        body_en=body_en,
        note_ru=note_ru,
    )
    session.add(packet)
    finding.status = STATUS_PACKET_READY
    await audit.log(
        session,
        actor_user_id=actor_user.id,
        action="packet.generate",
        entity_type="finding",
        entity_id=finding.id,
        summary=f"Сформирован пакет жалобы ({ROUTE_LABELS_RU[route]}) для «{cand.title}»",
        data={"route": route},
    )
    await session.flush()
    return packet


async def capture_evidence(session: AsyncSession, finding: Finding) -> EvidenceArchive:
    """Idempotent: returns the existing archive if one was already captured."""
    existing = await session.scalar(
        select(EvidenceArchive).where(EvidenceArchive.finding_id == finding.id)
    )
    if existing is not None:
        return existing

    cand = await session.get(PlatformCandidate, finding.candidate_id)
    snapshot = {
        "platform": cand.platform,
        "native_id": cand.native_id,
        "url": cand.url,
        "title": cand.title,
        "uploader": cand.uploader,
        "platform_channel_id": getattr(cand, "platform_channel_id", None),
        "description_raw": cand.description_raw,
        "parsed_provider": cand.parsed_provider,
        "parsed_plabel": cand.parsed_plabel,
        "isrc": cand.isrc,
        "published_at": cand.published_at.isoformat() if cand.published_at else None,
        "duration_ms": cand.duration_ms,
        "thumb_url": cand.thumb_url,
        "cover_phash": cand.cover_phash,
        "raw_json": cand.raw_json,
        "score": finding.score,
        "band": finding.band,
        "signals": finding.signals,
    }

    cover_path = None
    if cand.thumb_url:
        cover_path = await _snapshot_cover(finding.id, cand.thumb_url)

    session.add(
        evidence := EvidenceArchive(
            finding_id=finding.id,
            candidate_snapshot=snapshot,
            cover_snapshot_path=cover_path,
            audio_match_snapshot=getattr(finding, "audio_match", None),
            llm_snapshot=getattr(finding, "llm", None),
        )
    )
    await session.flush()
    return evidence


async def mark_sent(
    session: AsyncSession, packet: TakedownPacket, *, actor_user_id: int | None
) -> None:
    """Record that the owner sent this packet themselves (no auto-send, PLAN.md §11).
    Schedules the 14-day follow-up reminder."""
    now = datetime.now(UTC)
    packet.status = PACKET_SENT
    packet.sent_by_user_id = actor_user_id
    packet.sent_at = now
    packet.follow_up_at = now + timedelta(days=settings.takedown_followup_days)

    finding = await session.get(Finding, packet.finding_id)
    old_status = finding.status
    finding.status = STATUS_SENT
    session.add(
        FindingEvent(
            finding_id=finding.id,
            actor_user_id=actor_user_id,
            action="packet_sent",
            from_status=old_status,
            to_status=STATUS_SENT,
            note=f"Пакет отправлен ({ROUTE_LABELS_RU[packet.route]})",
        )
    )
    await audit.log(
        session,
        actor_user_id=actor_user_id,
        action="packet.mark_sent",
        entity_type="finding",
        entity_id=finding.id,
        summary=f"Отмечено как отправлено: {ROUTE_LABELS_RU[packet.route]}",
    )
    await session.flush()


async def record_outcome(
    session: AsyncSession,
    packet: TakedownPacket,
    outcome: str,
    new_finding_status: str,
    *,
    actor_user_id: int | None = None,
    note: str | None = None,
) -> None:
    """Apply a recheck/manual outcome to both the packet and its finding."""
    packet.outcome = outcome
    finding = await session.get(Finding, packet.finding_id)
    old_status = finding.status
    finding.status = new_finding_status
    session.add(
        FindingEvent(
            finding_id=finding.id,
            actor_user_id=actor_user_id,
            action="outcome",
            from_status=old_status,
            to_status=new_finding_status,
            note=note or outcome,
        )
    )
    await session.flush()


async def _snapshot_cover(finding_id: int, thumb_url: str) -> str | None:
    """Best-effort: copy the cover into the permanent evidence dir so it survives
    the live candidate/cover being deleted after takedown."""
    try:
        async with httpx.AsyncClient() as client:
            content = await images.fetch(client, thumb_url)
        return images.save_cover(content, settings.evidence_dir, f"finding-{finding_id}")
    except Exception:  # noqa: BLE001 - evidence capture must never block confirmation
        return None
