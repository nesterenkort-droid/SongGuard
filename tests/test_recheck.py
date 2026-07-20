"""Liveness recheck + follow-up reminders (real Postgres; liveness is monkeypatched
so tests never touch a live platform)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    OUTCOME_REMOVED,
    OUTCOME_STILL_ALIVE,
    ROUTE_DISTRIBUTOR,
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
    User,
)
from app.services import detection, recheck, takedown
from app.services.normalize import normalize_title


async def _setup_sent_finding(session) -> tuple[Finding, TakedownPacket, User]:
    user = User(
        tg_user_id=888001, display_name="Filer",
        legal_name="Jane Filer", legal_address="1 Main St", legal_email="jane@x.com",
    )
    session.add(user)
    artist = Artist(name="RecheckArtist")
    session.add(artist)
    await session.flush()
    session.add(ArtistMember(artist_id=artist.id, user_id=user.id, role="owner"))
    track = Track(
        primary_artist_id=artist.id, title="TRACK",
        normalized_title=normalize_title("TRACK"),
        release_date=date(2025, 1, 1), duration_ms=100000,
    )
    session.add(track)
    await session.flush()
    cand = PlatformCandidate(
        platform="spotify", native_id=f"recheck_native_{track.id}",
        title="TRACK (Slowed)", normalized_title=normalize_title("TRACK"),
        parsed_provider="DistroKid", url="https://example.com/t",
    )
    session.add(cand)
    await session.flush()
    finding = Finding(candidate_id=cand.id, track_id=track.id, score=90, band="high")
    session.add(finding)
    await session.flush()
    await detection.transition(session, finding, "confirm", actor_user_id=None)
    packet = await takedown.generate_packet(session, finding, ROUTE_DISTRIBUTOR, actor_user=user)
    await takedown.mark_sent(session, packet, actor_user_id=user.id)
    await session.flush()
    return finding, packet, user


@pytest.mark.asyncio
async def test_recheck_marks_removed_when_gone(db_session, monkeypatch):
    session = db_session
    finding, packet, _user = await _setup_sent_finding(session)

    async def fake_alive(platform, native_id):
        return False

    monkeypatch.setattr(recheck.liveness, "check_alive", fake_alive)
    new_status = await recheck.recheck_finding(session, finding)

    assert new_status == STATUS_REMOVED
    assert finding.status == STATUS_REMOVED
    assert packet.outcome == OUTCOME_REMOVED


@pytest.mark.asyncio
async def test_recheck_marks_still_alive_when_up(db_session, monkeypatch):
    session = db_session
    finding, packet, _user = await _setup_sent_finding(session)

    async def fake_alive(platform, native_id):
        return True

    monkeypatch.setattr(recheck.liveness, "check_alive", fake_alive)
    new_status = await recheck.recheck_finding(session, finding)

    assert new_status == STATUS_STILL_ALIVE
    assert finding.status == STATUS_STILL_ALIVE
    assert packet.outcome == OUTCOME_STILL_ALIVE


@pytest.mark.asyncio
async def test_recheck_unknown_liveness_makes_no_change(db_session, monkeypatch):
    session = db_session
    finding, packet, _user = await _setup_sent_finding(session)

    async def fake_alive(platform, native_id):
        return None

    monkeypatch.setattr(recheck.liveness, "check_alive", fake_alive)
    new_status = await recheck.recheck_finding(session, finding)

    assert new_status is None
    assert finding.status == STATUS_SENT
    assert packet.outcome is None


@pytest.mark.asyncio
async def test_removed_queues_celebration_notification(db_session, monkeypatch):
    session = db_session
    finding, _packet, user = await _setup_sent_finding(session)

    async def fake_alive(platform, native_id):
        return False

    monkeypatch.setattr(recheck.liveness, "check_alive", fake_alive)
    await recheck.recheck_finding(session, finding)
    await session.flush()

    rows = list(
        await session.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.user_id == user.id,
                NotificationOutbox.dedupe_key == f"removed:{finding.id}:user:{user.id}",
            )
        )
    )
    assert len(rows) == 1
    assert "удалена" in rows[0].data["text"]


@pytest.mark.asyncio
async def test_followup_reminder_sent_once(db_session):
    session = db_session
    finding, packet, user = await _setup_sent_finding(session)
    packet.follow_up_at = datetime.now(UTC) - timedelta(days=1)
    await session.flush()

    sent = await recheck.run_followup_reminders(session)
    assert sent == 1
    assert packet.follow_up_sent is True

    # Running again must not duplicate the reminder.
    sent2 = await recheck.run_followup_reminders(session)
    assert sent2 == 0
    total = await session.scalar(
        select(NotificationOutbox).where(
            NotificationOutbox.dedupe_key == f"followup:{packet.id}:user:{user.id}"
        )
    )
    assert total is not None
