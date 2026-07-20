"""Takedown packet generation: legal-field gate, routes, templates (real Postgres)."""

from datetime import date

import pytest
from sqlalchemy import select

from app.models import (
    PACKET_SENT,
    ROUTE_APPLE,
    ROUTE_DISTRIBUTOR,
    ROUTE_SPOTIFY,
    ROUTE_YOUTUBE,
    STATUS_PACKET_READY,
    STATUS_SENT,
    Artist,
    EvidenceArchive,
    Track,
    TrackArtist,
    User,
)
from app.services import detection, takedown
from app.services.normalize import normalize_title


async def _setup(session, *, with_legal=True) -> tuple[Artist, Track, User]:
    user = User(
        tg_user_id=777001, display_name="Filer",
        legal_name="Jane Filer" if with_legal else None,
        legal_address="1 Main St, City" if with_legal else None,
        legal_email="jane@example.com" if with_legal else None,
    )
    session.add(user)
    artist = Artist(name="TestArtist")
    session.add(artist)
    await session.flush()
    track = Track(
        primary_artist_id=artist.id,
        title="SOME TRACK",
        normalized_title=normalize_title("SOME TRACK"),
        isrc="QZHN52501234",
        release_date=date(2025, 1, 1),
        duration_ms=100000,
    )
    session.add(track)
    await session.flush()
    return artist, track, user


async def _confirmed_finding(session, artist, track, *, platform="spotify", provider="DistroKid"):
    from app.models import Finding, PlatformCandidate

    cand = PlatformCandidate(
        platform=platform, native_id=f"native_{platform}_{track.id}",
        title="SOME TRACK (Slowed)", normalized_title=normalize_title("SOME TRACK"),
        uploader="TestArtist", parsed_provider=provider,
        parsed_plabel="℗ 2026 13207436 Records DK" if provider else None,
        url="https://example.com/track",
    )
    session.add(cand)
    await session.flush()
    finding = Finding(candidate_id=cand.id, track_id=track.id, score=90, band="high")
    session.add(finding)
    await session.flush()
    await detection.transition(session, finding, "confirm", actor_user_id=None)
    await session.flush()
    return finding, cand


@pytest.mark.asyncio
async def test_generate_packet_blocked_without_legal_fields(db_session):
    session = db_session
    artist, track, user = await _setup(session, with_legal=False)
    finding, _cand = await _confirmed_finding(session, artist, track)

    with pytest.raises(takedown.LegalFieldsMissing):
        await takedown.generate_packet(session, finding, ROUTE_DISTRIBUTOR, actor_user=user)


@pytest.mark.asyncio
async def test_confirm_captures_evidence_automatically(db_session):
    session = db_session
    artist, track, _user = await _setup(session)
    finding, cand = await _confirmed_finding(session, artist, track)

    evidence = await session.scalar(
        select(EvidenceArchive).where(EvidenceArchive.finding_id == finding.id)
    )
    assert evidence is not None
    assert evidence.candidate_snapshot["title"] == cand.title
    assert evidence.candidate_snapshot["parsed_provider"] == "DistroKid"


@pytest.mark.asyncio
async def test_available_routes_distributor_first_when_applicable(db_session):
    session = db_session
    artist, track, _user = await _setup(session)
    _finding, cand = await _confirmed_finding(session, artist, track, platform="spotify")
    routes = takedown.available_routes(cand)
    assert routes[0] == ROUTE_DISTRIBUTOR
    assert ROUTE_SPOTIFY in routes


@pytest.mark.asyncio
async def test_available_routes_no_distributor_without_provider(db_session):
    session = db_session
    artist, track, _user = await _setup(session)
    _finding, cand = await _confirmed_finding(session, artist, track, provider=None)
    cand.parsed_plabel = None
    routes = takedown.available_routes(cand)
    assert ROUTE_DISTRIBUTOR not in routes
    assert routes == [ROUTE_SPOTIFY]


@pytest.mark.asyncio
async def test_generate_packet_body_has_legal_and_track_facts(db_session):
    session = db_session
    artist, track, user = await _setup(session)
    finding, cand = await _confirmed_finding(session, artist, track)

    packet = await takedown.generate_packet(session, finding, ROUTE_DISTRIBUTOR, actor_user=user)
    assert packet.body_en is not None
    assert user.legal_name in packet.body_en
    assert user.legal_address in packet.body_en
    assert track.isrc in packet.body_en
    assert cand.title in packet.body_en
    assert finding.status == STATUS_PACKET_READY


@pytest.mark.asyncio
async def test_youtube_route_uses_stable_url(db_session):
    session = db_session
    artist, track, user = await _setup(session)
    finding, _cand = await _confirmed_finding(session, artist, track, platform="youtube")

    packet = await takedown.generate_packet(session, finding, ROUTE_YOUTUBE, actor_user=user)
    assert "youtube.com/copyright" in packet.note_ru


@pytest.mark.asyncio
async def test_apple_route_flags_manual_verification(db_session):
    session = db_session
    artist, track, user = await _setup(session)
    finding, _cand = await _confirmed_finding(session, artist, track, platform="itunes")

    packet = await takedown.generate_packet(session, finding, ROUTE_APPLE, actor_user=user)
    assert "ПРОВЕРЬТЕ" in packet.note_ru


@pytest.mark.asyncio
async def test_collab_track_warns_about_single_owner(db_session):
    session = db_session
    artist, track, user = await _setup(session)
    other_artist = Artist(name="Collab Partner")
    session.add(other_artist)
    await session.flush()
    session.add(TrackArtist(track_id=track.id, artist_id=artist.id))
    session.add(TrackArtist(track_id=track.id, artist_id=other_artist.id))
    await session.flush()

    finding, _cand = await _confirmed_finding(session, artist, track)
    packet = await takedown.generate_packet(session, finding, ROUTE_DISTRIBUTOR, actor_user=user)
    assert "ОДИН правообладатель" in packet.note_ru


@pytest.mark.asyncio
async def test_mark_sent_schedules_followup_and_updates_finding(db_session):
    session = db_session
    artist, track, user = await _setup(session)
    finding, _cand = await _confirmed_finding(session, artist, track)
    packet = await takedown.generate_packet(session, finding, ROUTE_DISTRIBUTOR, actor_user=user)

    await takedown.mark_sent(session, packet, actor_user_id=user.id)
    assert packet.status == PACKET_SENT
    assert packet.sent_at is not None
    assert packet.follow_up_at is not None
    assert finding.status == STATUS_SENT
