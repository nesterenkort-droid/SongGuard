"""End-to-end detection on captured data (real Postgres via the db_session fixture).

Proves the M2 acceptance path (PLAN.md §13): the golden pirate — HEAVENLY JUMPSTYLE
(Slowed), released via DistroKid — is detected as a high-band finding against the
original, and dismiss → whitelist → rescan does NOT re-flag it.
"""

from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import (
    STATUS_DISMISSED,
    WL_CHANNEL,
    WL_OWN_LABEL,
    Artist,
    Finding,
    Track,
    WhitelistEntry,
)
from app.scanners.base import RawCandidate
from app.services import detection
from app.services.normalize import normalize_title
from app.services.scoring import normalize_label


async def _setup_artist(session) -> Artist:
    artist = Artist(name="TWXNY", spotify_artist_id="twxnyspotify", apple_artist_id="1718381786")
    session.add(artist)
    await session.flush()
    track = Track(
        primary_artist_id=artist.id,
        title="HEAVENLY JUMPSTYLE",
        normalized_title=normalize_title("HEAVENLY JUMPSTYLE"),
        credit="TWXNY, Sxilwix & Innxcence",
        release_date=date(2025, 11, 28),
        isrc="QZHN52501234",
        duration_ms=114462,
        spotify_track_id="orig_spotify_id",
        apple_track_id=1859638952,
        source="spotify",
    )
    session.add(track)
    # The tenant declares its only legal label.
    session.add(
        WhitelistEntry(
            scope="artist", artist_id=artist.id, entry_type=WL_OWN_LABEL,
            value="0to8", normalized_value=normalize_label("0to8"),
        )
    )
    await session.flush()
    return artist


def _pirate_raw() -> RawCandidate:
    return RawCandidate(
        platform="spotify",
        native_id="pirate_track_1",
        title="HEAVENLY JUMPSTYLE (Slowed)",
        url="https://open.spotify.com/track/pirate_track_1",
        uploader="TWXNY",
        parsed_provider="13207436 Records DK",
        parsed_plabel="℗ 2026 13207436 Records DK",
        isrc="DEXX12600001",
        published_at=date(2026, 7, 13),
        duration_ms=143078,  # 1.25x slowed
    )


@pytest.mark.asyncio
async def test_golden_pirate_detected_and_whitelist_suppresses(db_session):
    session = db_session
    artist = await _setup_artist(session)

    # --- Scan ingests the pirate → a high-band finding is created ---
    summary = await detection.ingest_candidates(
        session, artist, [_pirate_raw()], download_covers=False
    )
    assert summary.new_candidates == 1
    assert summary.findings_created == 1
    assert summary.high == 1

    finding = await session.scalar(select(Finding))
    assert finding is not None
    assert finding.band == "high"
    assert finding.score >= 70
    signal_keys = {s["key"] for s in finding.signals}
    assert {"title_exact", "suffix", "duration_ratio", "pirate_label"} <= signal_keys

    # --- Whitelist the channel from the finding → it is dismissed ---
    await detection.add_whitelist_from_finding(
        session, finding, WL_CHANNEL, actor_user_id=None
    )
    await session.flush()
    assert finding.status == STATUS_DISMISSED

    # --- Rescan: the gated candidate must NOT produce a new active finding ---
    summary2 = await detection.ingest_candidates(
        session, artist, [_pirate_raw()], download_covers=False
    )
    assert summary2.findings_created == 0
    total = await session.scalar(select(func.count(Finding.id)))
    assert total == 1  # still just the one, still dismissed
    refreshed = await session.scalar(select(Finding))
    assert refreshed.status == STATUS_DISMISSED
