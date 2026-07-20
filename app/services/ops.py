"""Operational watchdogs (PLAN.md §12): external dead-man switch + yt-dlp canary.

Both exist because the *app itself* can't be trusted to report its own failure
modes: if the whole VPS dies, nothing running on it can say so (hence the external
healthchecks.io ping); if YouTube changes its page structure, yt-dlp silently stops
extracting audio and every mid-band candidate loses its audio signal without any
error anyone would notice (hence a weekly canary extraction + a visible health tile).
"""

import asyncio
import logging
import subprocess
import sys
from datetime import UTC, datetime

import httpx

from app.config import settings
from app.redis_client import redis_client

logger = logging.getLogger("trackguard.ops")

YTDLP_DEGRADED_KEY = "ytdlp:degraded_since"


async def dead_man_ping() -> None:
    """Ping the external watchdog. No-op (and no error) if not configured — this
    must never be the thing that makes the worker crash-loop."""
    if not settings.healthchecks_ping_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(settings.healthchecks_ping_url)
    except Exception:  # noqa: BLE001 - a missed ping just means healthchecks.io
        # notices we're late; a raised exception here would be strictly worse.
        logger.warning("dead-man ping failed to send", exc_info=True)


def attempt_ytdlp_selfupdate() -> tuple[bool, str]:
    """Best-effort weekly `pip install -U yt-dlp` (PLAN.md §12: yt-dlp needs
    frequent updates as sites change, independent of full app redeploys).

    Runs in the current venv; effect lasts until the container is next rebuilt
    from the pinned pyproject.toml version, which is fine — this bridges the gap
    between deploys, it doesn't replace bumping the pin.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        ok = result.returncode == 0
        return ok, (result.stdout + result.stderr)[-2000:]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


async def run_ytdlp_canary() -> tuple[bool, str]:
    """Test-extract a known-good video (no download) to confirm yt-dlp's YouTube
    extractor still works. Skipped (treated as pass) if no canary URL is set."""
    if not settings.ytdlp_canary_url:
        return True, "канарейка не настроена (YTDLP_CANARY_URL пуст) — пропущено"

    import yt_dlp

    def _extract() -> tuple[bool, str]:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(settings.ytdlp_canary_url, download=False)
            if not info:
                return False, "extract_info вернул пусто"
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:500]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract)


async def run_ytdlp_maintenance() -> dict:
    """Weekly cron: self-update, then canary. Tracks degradation onset in Redis
    so the health page can show "аудио деградировано с даты X" (PLAN.md §12)."""
    update_ok, update_log = attempt_ytdlp_selfupdate()
    logger.info("yt-dlp self-update: ok=%s", update_ok)

    canary_ok, canary_msg = await run_ytdlp_canary()
    if canary_ok:
        cleared = await redis_client.delete(YTDLP_DEGRADED_KEY)
        if cleared:
            logger.info("yt-dlp canary recovered")
    else:
        # Keep the ORIGINAL failure timestamp if already degraded, so the health
        # tile shows how long it's been broken, not just "since the last check".
        existing = await redis_client.get(YTDLP_DEGRADED_KEY)
        if not existing:
            await redis_client.set(YTDLP_DEGRADED_KEY, datetime.now(UTC).isoformat())
        logger.warning("yt-dlp canary failed: %s", canary_msg)

    return {
        "update_ok": update_ok, "update_log": update_log,
        "canary_ok": canary_ok, "canary_msg": canary_msg,
    }
