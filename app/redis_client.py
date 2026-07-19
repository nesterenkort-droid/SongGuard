"""Shared async Redis client.

Used for heartbeats, the YouTube quota ledger, scan locks and the arq queue
(arq manages its own connection, but reads/writes of shared keys go through here).
`decode_responses=True` so callers get `str` values back, not bytes.
"""

from redis.asyncio import Redis, from_url

from app.config import settings

redis_client: Redis = from_url(
    settings.redis_url,
    encoding="utf-8",
    decode_responses=True,
)
