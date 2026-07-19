"""arq worker entrypoint.

Run with:  arq app.worker.main.WorkerSettings

The pull-based scan scheduler (see PLAN.md §8) will be built on top of this in M4;
for now it just runs the heartbeat cron.
"""

from arq import cron
from arq.connections import RedisSettings

from app.config import settings
from app.worker.tasks import scan_catalog, shutdown, startup, write_heartbeat


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [write_heartbeat, scan_catalog]
    cron_jobs = [
        # Every minute at second 0.
        cron(write_heartbeat, second=0, run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown
