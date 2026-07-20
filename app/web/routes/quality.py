"""Quality page: detection precision by score band + takedown outcomes (PLAN.md §10).

Admin-only, read-only. Answers two questions an owner actually cares about: "is the
scorer trustworthy?" (band -> confirm/dismiss rate) and "do our complaints work?"
(packet outcomes).
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import require_admin
from app.db import get_session
from app.models import (
    BAND_HIGH,
    BAND_LOW,
    BAND_MID,
    STATUS_DETECTED,
    STATUS_DISMISSED,
    STATUS_PENDING_REVIEW,
    STATUS_REMIX_REVIEW,
    STATUS_TOLERATED,
    Finding,
    TakedownPacket,
    User,
)
from app.web.templating import render

# Statuses that don't yet count as a decision either way (still open, or "allowed"
# rather than right/wrong) — excluded from the precision denominator.
_UNDECIDED_STATUSES = (
    STATUS_DETECTED, STATUS_PENDING_REVIEW, STATUS_REMIX_REVIEW, STATUS_TOLERATED,
)

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("")
async def quality_page(
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    band_stats = []
    for band in (BAND_HIGH, BAND_MID, BAND_LOW):
        total = await session.scalar(select(func.count(Finding.id)).where(Finding.band == band))
        confirmed = await session.scalar(
            select(func.count(Finding.id)).where(
                Finding.band == band,
                Finding.status != STATUS_DISMISSED,
                Finding.status.notin_(_UNDECIDED_STATUSES),
            )
        )
        dismissed = await session.scalar(
            select(func.count(Finding.id)).where(
                Finding.band == band, Finding.status == STATUS_DISMISSED
            )
        )
        decided = confirmed + dismissed
        precision = round(100 * confirmed / decided, 1) if decided else None
        band_stats.append(
            {"band": band, "total": total, "confirmed": confirmed,
             "dismissed": dismissed, "precision": precision}
        )

    packet_total = await session.scalar(select(func.count(TakedownPacket.id)))
    packet_sent = await session.scalar(
        select(func.count(TakedownPacket.id)).where(TakedownPacket.status == "sent")
    )
    outcome_rows = (
        await session.execute(
            select(TakedownPacket.outcome, func.count(TakedownPacket.id))
            .where(TakedownPacket.outcome.isnot(None))
            .group_by(TakedownPacket.outcome)
        )
    ).all()

    return render(
        request,
        "quality.html",
        {
            "band_stats": band_stats,
            "packet_total": packet_total,
            "packet_sent": packet_sent,
            "outcome_rows": outcome_rows,
        },
        user=user,
    )
