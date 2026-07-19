"""Background tasks run by the arq worker.

The heartbeat proves the worker is alive for the health page. `scan_catalog` runs an
M2 DSP scan for one artist; the pull-based scheduler that decides *when* to scan every
track arrives in M4 (PLAN.md §8) — for now scans are triggered from the dashboard.
"""

import logging
from datetime import UTC, datetime

from app.config import settings
from app.db import SessionLocal
from app.models import Artist
from app.services import detection

logger = logging.getLogger("trackguard.worker")


async def scan_catalog(ctx: dict, artist_id: int, actor_user_id: int | None = None) -> dict:
    """Run a Tier 0 DSP scan for one artist. Resilient: partial failures are logged,
    not raised, so a missing Spotify key or a flaky network never kills the worker."""
    async with SessionLocal() as session:
        artist = await session.get(Artist, artist_id)
        if artist is None:
            logger.warning("scan_catalog: artist %s not found", artist_id)
            return {"error": "artist_not_found", "artist_id": artist_id}
        try:
            summary = await detection.run_scan_for_artist(
                session, artist, actor_user_id=actor_user_id
            )
            logger.info("scan_catalog done: %s", summary.as_dict())
            return summary.as_dict()
        except Exception as exc:  # noqa: BLE001 - a scan must never crash the worker
            logger.exception("scan_catalog failed for artist %s", artist_id)
            return {"error": str(exc), "artist_id": artist_id}


async def write_heartbeat(ctx: dict) -> None:
    """Stamp `heartbeat:worker` in Redis with the current UTC time."""
    redis = ctx["redis"]
    now = datetime.now(UTC).isoformat()
    # TTL a bit above the freshness window so a dead worker's key eventually expires.
    await redis.set("heartbeat:worker", now, ex=settings.heartbeat_ttl_seconds * 2)
    logger.debug("worker heartbeat written: %s", now)


async def startup(ctx: dict) -> None:
    logger.info("worker starting up")
    # Fire once immediately so the health page shows the worker without waiting for cron.
    await write_heartbeat(ctx)


async def shutdown(ctx: dict) -> None:
    logger.info("worker shutting down")
