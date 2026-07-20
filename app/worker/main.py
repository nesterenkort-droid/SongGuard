"""arq worker entrypoint.

Run with:  arq app.worker.main.WorkerSettings

The pull-based scan scheduler (see PLAN.md §8) will be built on top of this in M4;
for now it just runs the heartbeat cron.
"""

from arq import cron
from arq.connections import RedisSettings

from app.config import settings
from app.services.recheck import recheck_tick
from app.services.scheduler import scheduler_tick
from app.worker.tasks import (
    run_dead_man_ping,
    run_retention,
    run_ytdlp_maintenance,
    scan_catalog,
    shutdown,
    startup,
    write_heartbeat,
)


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [
        write_heartbeat, scan_catalog, scheduler_tick, recheck_tick,
        run_retention, run_dead_man_ping, run_ytdlp_maintenance,
    ]
    cron_jobs = [
        # Every minute at second 0.
        cron(write_heartbeat, second=0, run_at_startup=False),
        # Every N minutes.
        cron(
            scheduler_tick,
            minute=set(range(0, 60, settings.scheduler_interval_minutes)),
            run_at_startup=True,
        ),
        # Daily: liveness recheck of confirmed/sent findings + follow-up reminders.
        cron(recheck_tick, hour=3, minute=0, run_at_startup=False),
        # Daily: shrink old evidence covers to bound disk growth.
        cron(run_retention, hour=4, minute=0, run_at_startup=False),
        # Every 5 minutes: external dead-man switch ping (no-op if not configured).
        cron(run_dead_man_ping, minute=set(range(0, 60, 5)), run_at_startup=True),
        # Weekly (Sunday 05:00): yt-dlp self-update + canary extraction test.
        cron(run_ytdlp_maintenance, weekday=6, hour=5, minute=0, run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown
