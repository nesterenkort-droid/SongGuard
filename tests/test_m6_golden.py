"""M6 golden-path E2E (PLAN.md §13): находка → подтверждение → пакет с архивом →
(ручная отправка) → отслеживание → removed. Real Postgres, liveness monkeypatched.
"""

from datetime import date

import pytest
from sqlalchemy import select

from app.models import (
    ROUTE_DISTRIBUTOR,
    STATUS_CONFIRMED,
    STATUS_PACKET_READY,
    STATUS_REMOVED,
    STATUS_SENT,
    Artist,
    ArtistMember,
    EvidenceArchive,
    Finding,
    PlatformCandidate,
    Track,
    User,
)
from app.services import detection, recheck, takedown
from app.services.normalize import normalize_title


@pytest.mark.asyncio
async def test_golden_lifecycle_confirm_to_removed(db_session, monkeypatch):
    session = db_session

    # --- Setup: 0to8's golden case (HEAVENLY JUMPSTYLE vs. the DistroKid pirate) ---
    user = User(
        tg_user_id=999001, display_name="Rights Owner",
        legal_name="0to8 LLC", legal_address="1 Label Way, City",
        legal_email="legal@0to8.example",
    )
    session.add(user)
    artist = Artist(name="TWXNY")
    session.add(artist)
    await session.flush()
    session.add(ArtistMember(artist_id=artist.id, user_id=user.id, role="owner"))
    track = Track(
        primary_artist_id=artist.id, title="HEAVENLY JUMPSTYLE",
        normalized_title=normalize_title("HEAVENLY JUMPSTYLE"),
        isrc="QZHN52501234", release_date=date(2025, 11, 28), duration_ms=114462,
    )
    session.add(track)
    await session.flush()
    cand = PlatformCandidate(
        platform="spotify", native_id="golden_m6_pirate",
        title="HEAVENLY JUMPSTYLE (Slowed)", normalized_title=normalize_title("HEAVENLY JUMPSTYLE"),
        uploader="TWXNY", parsed_provider="13207436 Records DK",
        parsed_plabel="℗ 2026 13207436 Records DK",
        url="https://open.spotify.com/track/golden_m6_pirate",
    )
    session.add(cand)
    await session.flush()
    finding = Finding(candidate_id=cand.id, track_id=track.id, score=150, band="high")
    session.add(finding)
    await session.flush()

    # --- 1. Confirm -> evidence captured automatically ---
    await detection.transition(session, finding, "confirm", actor_user_id=user.id)
    assert finding.status == STATUS_CONFIRMED
    evidence = await session.scalar(
        select(EvidenceArchive).where(EvidenceArchive.finding_id == finding.id)
    )
    assert evidence is not None
    assert evidence.candidate_snapshot["parsed_plabel"] == cand.parsed_plabel

    # --- 2. Generate a packet (distributor route, since DistroKid was parsed) ---
    routes = takedown.available_routes(cand)
    assert routes[0] == ROUTE_DISTRIBUTOR
    packet = await takedown.generate_packet(session, finding, ROUTE_DISTRIBUTOR, actor_user=user)
    assert finding.status == STATUS_PACKET_READY
    assert packet.evidence_id == evidence.id
    assert "13207436 Records DK" in packet.body_en

    # --- 3. Owner sends it manually, marks it sent ---
    await takedown.mark_sent(session, packet, actor_user_id=user.id)
    assert finding.status == STATUS_SENT
    assert packet.follow_up_at is not None

    # --- 4. Recheck cycle later finds the pirate gone -> removed, celebration queued ---
    async def fake_gone(platform, native_id):
        return False

    monkeypatch.setattr(recheck.liveness, "check_alive", fake_gone)
    outcomes = await recheck.run_liveness_recheck(session)

    assert outcomes.get(STATUS_REMOVED) == 1
    assert finding.status == STATUS_REMOVED
    assert packet.outcome == "removed"

    # The evidence survives even though the live candidate/platform data is gone —
    # that's the whole point of archiving it at confirmation time.
    frozen = await session.scalar(
        select(EvidenceArchive).where(EvidenceArchive.finding_id == finding.id)
    )
    assert frozen.candidate_snapshot["title"] == "HEAVENLY JUMPSTYLE (Slowed)"
