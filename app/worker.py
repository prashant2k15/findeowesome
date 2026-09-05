"""The 24x7 worker: one long-lived process that runs every job on schedule.

Deliberately no Celery/Redis - APScheduler in a single supervised container
does the same work here with far fewer moving parts to keep alive on a VPS.
Jobs never overlap (max_instances=1) and a missed run is coalesced, so a
restart or a slow batch cannot pile work up.
"""
from __future__ import annotations

import logging
import signal
import sys

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import jobs
from app.config import settings
from app.db.session import init_db
from app.notify import telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

JOB_DEFS = [
    # (id, callable, trigger)
    ("github_discover", jobs.job_github_discover, lambda s: IntervalTrigger(hours=s.job_github_seeds_hours, jitter=600)),
    ("github_harvest", jobs.job_github_harvest, lambda s: IntervalTrigger(hours=max(1, s.job_github_seeds_hours // 4), jitter=300)),
    ("footprints", jobs.job_footprints, lambda s: IntervalTrigger(hours=s.job_footprint_hours, jitter=600)),
    ("import", jobs.job_import, lambda s: IntervalTrigger(minutes=15)),
    ("check", jobs.job_check, lambda s: IntervalTrigger(hours=s.job_checker_hours, jitter=120)),
    ("export", jobs.job_export, lambda s: IntervalTrigger(hours=6)),
    ("cleanup", jobs.job_cleanup, lambda s: CronTrigger(hour=4, minute=30)),
    ("purge", jobs.job_purge, lambda s: CronTrigger(hour=5, minute=0)),
    ("metrics", jobs.job_metrics, lambda s: IntervalTrigger(hours=6, jitter=300)),
    ("verify_backlinks", jobs.job_verify_backlinks, lambda s: IntervalTrigger(hours=s.job_tracker_hours, jitter=300)),
    ("build_prospects", jobs.job_build_prospects, lambda s: IntervalTrigger(hours=12, jitter=600)),
    ("find_contacts", jobs.job_find_contacts, lambda s: IntervalTrigger(hours=3, jitter=300)),
    ("draft_outreach", jobs.job_draft_outreach, lambda s: IntervalTrigger(hours=6, jitter=300)),
    ("send_outreach", jobs.job_send_outreach, lambda s: IntervalTrigger(hours=2, jitter=600)),
    ("report", jobs.job_report, lambda s: CronTrigger(hour=settings.job_report_hour, minute=0)),
]


def _wrap(name, fn):
    def runner():
        jobs.run_job(name, fn)

    runner.__name__ = f"job_{name}"
    return runner


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    for job_id, fn, trigger in JOB_DEFS:
        scheduler.add_job(_wrap(job_id, fn), trigger(settings), id=job_id, name=job_id)
    return scheduler


def main() -> None:
    init_db()
    log.info("database ready: %s", settings.database_url.split("@")[-1])

    # config -> DB, then a first discovery pass so a fresh install is not idle
    jobs.run_job("sync_config", jobs.job_sync_config)

    scheduler = build_scheduler()
    scheduler.start()
    log.info("scheduler started with %s jobs", len(JOB_DEFS))
    for job in scheduler.get_jobs():
        log.info("  %-16s next run: %s", job.id, job.next_run_time)

    telegram.send("<b>BingLinkFinder worker started</b>", silent=True)

    stopping = False

    def shutdown(signum, _frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        log.info("signal %s received, shutting down", signum)
        scheduler.shutdown(wait=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # keep the main thread alive; APScheduler runs jobs on its own threads
    try:
        signal.pause() if hasattr(signal, "pause") else _sleep_forever()
    except (KeyboardInterrupt, SystemExit):
        shutdown("KeyboardInterrupt", None)


def _sleep_forever() -> None:  # Windows has no signal.pause()
    import time

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
