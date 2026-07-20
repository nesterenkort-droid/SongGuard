"""Global (scope=global) whitelist entries — as created by /settings — must apply
to every artist automatically, with no per-artist duplication (real Postgres)."""

from datetime import date

import pytest
from sqlalchemy import select

from app.models import (
    WL_CHANNEL,
    WL_OWN_LABEL,
    WL_SCOPE_GLOBAL,
    Artist,
    Track,
    WhitelistEntry,
)
from app.services import detection
from app.services.normalize import normalize_title
from app.services.scoring import normalize_label


async def _artist_with_track(session) -> tuple[Artist, Track]:
    artist = Artist(name="GlobalWhitelistArtist")
    session.add(artist)
    await session.flush()
    track = Track(
        primary_artist_id=artist.id, title="TRACK",
        normalized_title=normalize_title("TRACK"),
        release_date=date(2025, 1, 1), duration_ms=100000,
    )
    session.add(track)
    await session.flush()
    return artist, track


@pytest.mark.asyncio
async def test_global_own_label_applies_to_any_artist(db_session):
    session = db_session
    artist, _track = await _artist_with_track(session)

    session.add(
        WhitelistEntry(
            scope=WL_SCOPE_GLOBAL, artist_id=None, entry_type=WL_OWN_LABEL,
            value="0to8", normalized_value=normalize_label("0to8"),
        )
    )
    await session.flush()

    ctx = await detection.build_context(session, artist)
    assert normalize_label("0to8") in ctx.own_labels


@pytest.mark.asyncio
async def test_global_channel_applies_to_any_artist(db_session):
    session = db_session
    artist, _track = await _artist_with_track(session)

    session.add(
        WhitelistEntry(
            scope=WL_SCOPE_GLOBAL, artist_id=None, entry_type=WL_CHANNEL,
            value="TWXNY - Topic", normalized_value=normalize_label("TWXNY - Topic"),
        )
    )
    await session.flush()

    ctx = await detection.build_context(session, artist)
    assert normalize_label("TWXNY - Topic") in ctx.whitelist_channels


@pytest.mark.asyncio
async def test_channel_entry_keeps_its_url(db_session):
    """channel_url is display-only (not read by detection), but must round-trip —
    it's how a reviewer confirms the whitelisted channel is the right one."""
    session = db_session
    session.add(
        WhitelistEntry(
            scope=WL_SCOPE_GLOBAL, artist_id=None, entry_type=WL_CHANNEL,
            value="TWXNY - Topic", normalized_value=normalize_label("TWXNY - Topic"),
            channel_url="https://youtube.com/@twxny",
        )
    )
    await session.flush()

    entry = await session.scalar(
        select(WhitelistEntry).where(WhitelistEntry.value == "TWXNY - Topic")
    )
    assert entry.channel_url == "https://youtube.com/@twxny"


@pytest.mark.asyncio
async def test_global_whitelist_not_scoped_to_one_artist_only(db_session):
    """A second, unrelated artist also sees the same global entries — that's the
    whole point of scope=global vs. duplicating per-artist rows."""
    session = db_session
    artist_a, _t = await _artist_with_track(session)
    artist_b = Artist(name="AnotherArtist")
    session.add(artist_b)
    await session.flush()

    session.add(
        WhitelistEntry(
            scope=WL_SCOPE_GLOBAL, artist_id=None, entry_type=WL_OWN_LABEL,
            value="SharedLabel", normalized_value=normalize_label("SharedLabel"),
        )
    )
    await session.flush()

    ctx_a = await detection.build_context(session, artist_a)
    ctx_b = await detection.build_context(session, artist_b)
    assert normalize_label("SharedLabel") in ctx_a.own_labels
    assert normalize_label("SharedLabel") in ctx_b.own_labels
