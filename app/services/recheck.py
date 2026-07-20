"""Recheck cycle + follow-up reminders for confirmed/sent findings (PLAN.md §7, §11).

Two independent jobs, both daily cron (`recheck_tick`):

* Liveness recheck: for every confirmed-or-later finding, ask the platform if the
  candidate still exists. A definite "gone" is a win (removed); still up after a
  packet was sent is `still_alive`; a *new* candidate reappearing at the same pirate
  channel/label after a removal is `reappeared` — recidivism evidence for future
  strikes, chained via a FindingEvent note (PLAN.md §7 "Кросс-триггеры и жизненный
  цикл").
* Follow-up reminders: a packet sent >=14 days ago with no outcome yet gets one
  reminder queued through the existing notify/outbox machinery — never a duplicate,
  `follow_up_sent` is a one-shot flag.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import (
    KIND_ADMIN_ALERT,
    OUTCOME_REMOVED,
    OUTCOME_STILL_ALIVE,
    STATUS_DETECTED,
    STATUS_PENDING_REVIEW,
    STATUS_REAPPEARED,
    STATUS_REMIX_REVIEW,
    STATUS_REMOVED,
    STATUS_SENT,
    STATUS_STILL_ALIVE,
    Artist,
    ArtistMember,
    Finding,
    NotificationOutbox,
    PlatformCandidate,
    TakedownPacket,
    Track,
)
from app.services import liveness

logger = logging.getLogger("trackguard.recheck")

# Recheck every OPEN finding daily — not just those we've already sent a packet for.
# This also catches candidates that are already dead when first detected (taken down
# by someone else, channel deleted, etc.) so they don't sit in the review queue as
# active threats — they auto-move to "removed". Cheap: liveness is videos.list (1 quota
# unit), not a search (100).
RECHECK_STATUSES = frozenset(
    {STATUS_DETECTED, STATUS_PENDING_REVIEW, STATUS_REMIX_REVIEW, STATUS_SENT, STATUS_STILL_ALIVE}
)


async def recheck_finding(session: AsyncSession, finding: Finding) -> str | None:
    """Check one finding's candidate liveness and apply the outcome. Returns the
    new status, or None if liveness couldn't be determined (no change made)."""
    cand = await session.get(PlatformCandidate, finding.candidate_id)
    if cand is None:
        return None
    alive = await liveness.check_alive(cand.platform, cand.native_id)
    if alive is None:
        return None

    packet = await session.scalar(
        select(TakedownPacket)
        .where(TakedownPacket.finding_id == finding.id)
        .order_by(TakedownPacket.created_at.desc())
    )

    if not alive:
        finding.status = STATUS_REMOVED
        # Only celebrate a takedown we actually pursued. A candidate that was already
        # dead when detected (never sent) just moves to "removed" quietly.
        if packet is not None:
            packet.outcome = OUTCOME_REMOVED
            await _notify_removed(session, finding, cand)
        return STATUS_REMOVED

    if finding.status == STATUS_SENT:
        finding.status = STATUS_STILL_ALIVE
        if packet is not None:
            packet.outcome = OUTCOME_STILL_ALIVE
        return STATUS_STILL_ALIVE
    return None


async def _notify_removed(
    session: AsyncSession, finding: Finding, cand: PlatformCandidate
) -> None:
    """Queue a "🎉 removed" ping to the artist's members — the payoff moment."""
    track = await session.get(Track, finding.track_id)
    members = list(
        await session.scalars(
            select(ArtistMember.user_id).where(ArtistMember.artist_id == track.primary_artist_id)
        )
    )
    for user_id in members:
        dedupe_key = f"removed:{finding.id}:user:{user_id}"
        exists = await session.scalar(
            select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == dedupe_key)
        )
        if exists is not None:
            continue
        session.add(
            NotificationOutbox(
                user_id=user_id,
                kind=KIND_ADMIN_ALERT,
                dedupe_key=dedupe_key,
                data={
                    "text": (
                        f"🎉 Победа! Пиратка «{cand.title}» ({cand.platform}) удалена — "
                        f"наш трек «{track.title}» защищён."
                    )
                },
            )
        )


async def check_reappearance(session: AsyncSession, finding: Finding) -> None:
    """After a removal, watch the same pirate entity for a fresh upload of the same
    track — recidivism evidence for future strikes (PLAN.md §7)."""
    cand = await session.get(PlatformCandidate, finding.candidate_id)
    if cand is None or not (cand.parsed_provider or cand.parsed_plabel):
        return
    label = cand.parsed_provider or cand.parsed_plabel
    reappeared = await session.scalar(
        select(Finding)
        .join(PlatformCandidate, Finding.candidate_id == PlatformCandidate.id)
        .where(
            Finding.track_id == finding.track_id,
            Finding.id != finding.id,
            PlatformCandidate.id != cand.id,
            (PlatformCandidate.parsed_provider == label)
            | (PlatformCandidate.parsed_plabel == label),
            Finding.created_at > finding.created_at,
        )
    )
    if reappeared is not None and reappeared.status not in (STATUS_REMOVED, STATUS_REAPPEARED):
        reappeared.status = STATUS_REAPPEARED


async def run_liveness_recheck(session: AsyncSession) -> dict:
    findings = list(
        await session.scalars(select(Finding).where(Finding.status.in_(RECHECK_STATUSES)))
    )
    outcomes: dict[str, int] = {}
    for finding in findings:
        new_status = await recheck_finding(session, finding)
        if new_status:
            outcomes[new_status] = outcomes.get(new_status, 0) + 1
            if new_status == STATUS_REMOVED:
                await check_reappearance(session, finding)
    await session.commit()
    return outcomes


async def run_followup_reminders(session: AsyncSession) -> int:
    now = datetime.now(UTC)
    due = list(
        await session.scalars(
            select(TakedownPacket).where(
                TakedownPacket.status == "sent",
                TakedownPacket.outcome.is_(None),
                TakedownPacket.follow_up_sent.is_(False),
                TakedownPacket.follow_up_at.isnot(None),
                TakedownPacket.follow_up_at <= now,
            )
        )
    )
    sent = 0
    for packet in due:
        finding = await session.get(Finding, packet.finding_id)
        if finding is None:
            continue
        track = await session.get(Track, finding.track_id)
        artist = await session.get(Artist, track.primary_artist_id) if track else None
        cand = await session.get(PlatformCandidate, finding.candidate_id)
        members = list(
            await session.scalars(
                select(ArtistMember.user_id).where(
                    ArtistMember.artist_id == (artist.id if artist else -1)
                )
            )
        )
        for user_id in members:
            session.add(
                NotificationOutbox(
                    user_id=user_id,
                    kind=KIND_ADMIN_ALERT,
                    dedupe_key=f"followup:{packet.id}:user:{user_id}",
                    data={
                        "text": (
                            f"⏰ Прошло 14 дней с отправки жалобы на "
                            f"«{cand.title if cand else '?'}», ответа нет. "
                            f"Проверьте статус и подумайте о повторной подаче."
                        )
                    },
                )
            )
        packet.follow_up_sent = True
        sent += 1
    await session.commit()
    return sent


async def recheck_tick(ctx: dict) -> dict:
    """arq daily cron entrypoint."""
    async with SessionLocal() as session:
        try:
            outcomes = await run_liveness_recheck(session)
            reminders = await run_followup_reminders(session)
            result = {"outcomes": outcomes, "followup_reminders": reminders}
            logger.info("recheck tick: %s", result)
            return result
        except Exception as exc:  # noqa: BLE001 - a tick must never crash the worker
            logger.exception("recheck tick failed")
            return {"error": str(exc)}
