"""Evidence retention: compress old covers, skip active cases and small files."""

import os
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select

from app.models import Artist, EvidenceArchive, Finding, PlatformCandidate, Track
from app.services import detection, retention
from app.services.normalize import normalize_title


def _make_jpeg(path: str, size: tuple[int, int] = (1200, 1200)) -> None:
    """A random-noise image, so JPEG can't compress it away to nothing — a flat
    color would compress to a few KB regardless of quality, unlike a real photo."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rng = np.random.default_rng(42)
    arr = rng.integers(0, 256, size=(size[1], size[0], 3), dtype=np.uint8)
    Image.fromarray(arr, mode="RGB").save(path, format="JPEG", quality=95)


def test_compress_cover_file_shrinks_large_image(tmp_path):
    path = str(tmp_path / "cover.jpg")
    _make_jpeg(path)
    before = os.path.getsize(path)
    assert before > retention.ALREADY_COMPRESSED_THRESHOLD_BYTES

    saved = retention.compress_cover_file(path)
    after = os.path.getsize(path)
    assert saved is not None
    assert saved > 0
    assert after < before
    with Image.open(path) as img:
        assert max(img.size) <= retention.MAX_DIMENSION


def test_compress_cover_file_skips_already_small(tmp_path):
    path = str(tmp_path / "small.jpg")
    _make_jpeg(path, size=(50, 50))
    assert os.path.getsize(path) <= retention.ALREADY_COMPRESSED_THRESHOLD_BYTES
    assert retention.compress_cover_file(path) is None


def test_compress_cover_file_missing_returns_none():
    assert retention.compress_cover_file("/no/such/file.jpg") is None


async def _confirmed_finding_with_evidence(session, *, captured_at=None):
    artist = Artist(name="RetentionArtist")
    session.add(artist)
    await session.flush()
    track = Track(
        primary_artist_id=artist.id, title="TRACK",
        normalized_title=normalize_title("TRACK"),
        release_date=date(2025, 1, 1), duration_ms=100000,
    )
    session.add(track)
    await session.flush()
    cand = PlatformCandidate(
        platform="spotify", native_id=f"retention_native_{track.id}",
        title="TRACK (Slowed)", normalized_title=normalize_title("TRACK"),
    )
    session.add(cand)
    await session.flush()
    finding = Finding(candidate_id=cand.id, track_id=track.id, score=90, band="high")
    session.add(finding)
    await session.flush()
    await detection.transition(session, finding, "confirm", actor_user_id=None)
    await session.flush()
    if captured_at is not None:
        evidence = await session.scalar(
            select(EvidenceArchive).where(EvidenceArchive.finding_id == finding.id)
        )
        evidence.captured_at = captured_at
        await session.flush()
    return finding


@pytest.mark.asyncio
async def test_compress_old_evidence_processes_old_terminal_findings(db_session, tmp_path):
    session = db_session

    old_date = datetime.now(UTC) - timedelta(days=retention.EVIDENCE_COMPRESS_AFTER_DAYS + 5)
    finding = await _confirmed_finding_with_evidence(session, captured_at=old_date)
    evidence = await session.scalar(
        select(EvidenceArchive).where(EvidenceArchive.finding_id == finding.id)
    )
    filename = "old_cover.jpg"
    evidence.cover_snapshot_path = filename
    _make_jpeg(str(tmp_path / filename))
    await session.flush()

    result = await retention.compress_old_evidence(session, base_dir=str(tmp_path))
    assert result["compressed"] == 1
    assert result["bytes_saved"] > 0


@pytest.mark.asyncio
async def test_compress_old_evidence_skips_recent(db_session, tmp_path):
    session = db_session

    finding = await _confirmed_finding_with_evidence(session)  # captured_at = now
    evidence = await session.scalar(
        select(EvidenceArchive).where(EvidenceArchive.finding_id == finding.id)
    )
    filename = "recent_cover.jpg"
    evidence.cover_snapshot_path = filename
    _make_jpeg(str(tmp_path / filename))
    await session.flush()

    result = await retention.compress_old_evidence(session, base_dir=str(tmp_path))
    assert result["compressed"] == 0
