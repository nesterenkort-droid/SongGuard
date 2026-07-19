"""Seed baseline data.

Idempotent: safe to run on every deploy. In M0 it just records the app version
and an initialization marker in system_info, which also gives the health page a
row to prove the schema is live.

Run with:  python -m scripts.seed
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import SystemInfo


async def _upsert(session, key: str, value: str) -> None:
    existing = await session.scalar(select(SystemInfo).where(SystemInfo.key == key))
    if existing:
        existing.value = value
    else:
        session.add(SystemInfo(key=key, value=value))


async def main() -> None:
    async with SessionLocal() as session:
        await _upsert(session, "app_version", settings.app_version)
        await _upsert(session, "initialized_at", datetime.now(UTC).isoformat())
        await session.commit()
    print("seed: done")


if __name__ == "__main__":
    asyncio.run(main())
