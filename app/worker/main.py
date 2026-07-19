"""arq worker entrypoint.

Run with:  arq app.worker.main.WorkerSettings

The pull-based scan scheduler (see PLAN.md §8) will be built on top of this in M4;
for now it just runs the heartbeat cron.
"""

from arq import cron
from arq.connections import RedisSettings

from app.config import settings
from app.services.scheduler import scheduler_tick
from app.worker.tasks import scan_catalog, shutdown, startup, write_heartbeat


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [write_heartbeat, scan_catalog, scheduler_tick]
    cron_jobs = [
        # Every minute at second 0.
        cron(write_heartbeat, second=0, run_at_startup=False),
        # Every N minutes.
        cron(
            scheduler_tick,
            minute=set(range(0, 60, settings.scheduler_interval_minutes)),
            run_at_startup=True,
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
