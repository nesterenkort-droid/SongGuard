"""Background tasks run by the arq worker.

In M0 the only job is a heartbeat that proves the worker is alive and lets the
health page report on it. Real scan/scoring tasks arrive in M2+.
"""

import logging
from datetime import UTC, datetime

from app.config import settings

logger = logging.getLogger("trackguard.worker")


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
