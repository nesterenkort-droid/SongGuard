"""Outbox enqueue on a new finding (real Postgres via db_session fixture)."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from app.models import (
    MODE_DAILY,
    Artist,
    ArtistMember,
    NotificationOutbox,
    Subscription,
    Track,
    User,
)
from app.scanners.base import RawCandidate
from app.services import detection
from app.services.normalize import normalize_title


async def _setup(session, *, mode: str | None = None) -> tuple[Artist, User]:
    user = User(tg_user_id=555001, display_name="Member")
    session.add(user)
    await session.flush()

    artist = Artist(name="Test Artist")
    session.add(artist)
    await session.flush()

    session.add(ArtistMember(artist_id=artist.id, user_id=user.id, role="owner"))
    if mode:
        session.add(Subscription(user_id=user.id, artist_id=artist.id, mode=mode))

    track = Track(
        primary_artist_id=artist.id,
        title="SOME TRACK",
        normalized_title=normalize_title("SOME TRACK"),
        release_date=date(2025, 1, 1),
        duration_ms=100000,
    )
    session.add(track)
    await session.flush()
    return artist, user


def _pirate() -> RawCandidate:
    return RawCandidate(
        platform="spotify", native_id="notify_pirate_1",
        title="SOME TRACK (Slowed)", uploader="Test Artist",
        published_at=date(2026, 1, 1), duration_ms=125000,
    )


@pytest.mark.asyncio
async def test_new_finding_enqueues_outbox_for_member(db_session):
    session = db_session
    artist, user = await _setup(session)

    summary = await detection.ingest_candidates(session, artist, [_pirate()], download_covers=False)
    assert summary.findings_created == 1

    rows = list(
        await session.scalars(
            select(NotificationOutbox).where(NotificationOutbox.user_id == user.id)
        )
    )
    assert len(rows) == 1
    assert rows[0].dedupe_key.startswith("finding:")
    assert rows[0].status == "pending"
    # Default (no Subscription row) is instant -> scheduled roughly now.
    assert (datetime.now(UTC) - rows[0].scheduled_for.replace(tzinfo=UTC)).total_seconds() < 5


@pytest.mark.asyncio
async def test_rescan_does_not_duplicate_outbox_row(db_session):
    session = db_session
    artist, user = await _setup(session)

    await detection.ingest_candidates(session, artist, [_pirate()], download_covers=False)
    await detection.ingest_candidates(session, artist, [_pirate()], download_covers=False)

    total = await session.scalar(
        select(func.count(NotificationOutbox.id)).where(NotificationOutbox.user_id == user.id)
    )
    assert total == 1


@pytest.mark.asyncio
async def test_daily_subscription_schedules_for_future_slot(db_session):
    session = db_session
    artist, user = await _setup(session, mode=MODE_DAILY)

    await detection.ingest_candidates(session, artist, [_pirate()], download_covers=False)

    row = await session.scalar(
        select(NotificationOutbox).where(NotificationOutbox.user_id == user.id)
    )
    assert row is not None
    assert row.scheduled_for.replace(tzinfo=UTC) > datetime.now(UTC)
