"""Disk retention (PLAN.md §12): compress old evidence covers to bound growth.

Candidate query audio is already deleted immediately after a Panako check
(services/detection.py), and candidate covers are hashed in-memory and never
written to disk — so the one real long-term growth source under `data_dir` is
`evidence_archive` cover snapshots, which must be *kept* (legal necessity) but
don't need full resolution once a finding has settled into a terminal state.
This shrinks them in place rather than deleting anything.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    STATUS_CONFIRMED,
    STATUS_COUNTER_NOTICED,
    STATUS_DISMISSED,
    STATUS_REAPPEARED,
    STATUS_REMOVED,
    STATUS_STILL_ALIVE,
    STATUS_TOLERATED,
    EvidenceArchive,
    Finding,
)

logger = logging.getLogger("trackguard.retention")

# Only compress evidence for findings that are settled — never touch anything
# still under active review (a live case might still need the full-res image).
TERMINAL_STATUSES = frozenset(
    {STATUS_DISMISSED, STATUS_TOLERATED, STATUS_CONFIRMED, STATUS_REMOVED,
     STATUS_STILL_ALIVE, STATUS_COUNTER_NOTICED, STATUS_REAPPEARED}
)

EVIDENCE_COMPRESS_AFTER_DAYS = 90
MAX_DIMENSION = 320
JPEG_QUALITY = 60
# A cover already at/under this size is presumed already compressed — skips
# re-processing the same file on every daily run.
ALREADY_COMPRESSED_THRESHOLD_BYTES = 50_000


def compress_cover_file(path: str) -> int | None:
    """Downscale + re-encode one cover file in place. Returns bytes saved, or
    None if the file was missing/already small enough to skip."""
    if not os.path.exists(path):
        return None
    before = os.path.getsize(path)
    if before <= ALREADY_COMPRESSED_THRESHOLD_BYTES:
        return None
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
            img.save(path, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception:  # noqa: BLE001 - one bad image must not stop the batch
        logger.exception("failed to compress evidence cover %s", path)
        return None
    after = os.path.getsize(path)
    return max(0, before - after)


async def compress_old_evidence(session: AsyncSession, *, base_dir: str | None = None) -> dict:
    """Compress evidence covers for terminal-state findings older than the
    retention window. Safe to run repeatedly (re-compressing a small JPEG at
    the same settings saves ~nothing further, so it's cheap even if re-run).

    `base_dir` defaults to `settings.evidence_dir`; overridable for tests.
    """
    base_dir = base_dir or settings.evidence_dir
    cutoff = datetime.now(UTC) - timedelta(days=EVIDENCE_COMPRESS_AFTER_DAYS)
    rows = list(
        await session.execute(
            select(EvidenceArchive, Finding)
            .join(Finding, EvidenceArchive.finding_id == Finding.id)
            .where(
                EvidenceArchive.captured_at < cutoff,
                EvidenceArchive.cover_snapshot_path.isnot(None),
                Finding.status.in_(TERMINAL_STATUSES),
            )
        )
    )
    compressed = 0
    bytes_saved = 0
    for evidence, _finding in rows:
        path = os.path.join(base_dir, evidence.cover_snapshot_path)
        saved = compress_cover_file(path)
        if saved:
            compressed += 1
            bytes_saved += saved
    result = {"compressed": compressed, "bytes_saved": bytes_saved}
    logger.info("evidence retention: %s", result)
    return result
