"""Internal scheduler (APScheduler) so the app can run as a systemd service."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import get_settings
from .profiles import run_enabled_profiles

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _job() -> None:
    log.info("scheduled sync starting")
    try:
        runs = run_enabled_profiles()
        for run in runs:
            log.info(
                "scheduled sync #%s finished: status=%s new=%d",
                run.id,
                run.status.value,
                run.new,
            )
    except Exception:  # pragma: no cover
        log.exception("scheduled sync failed")


def _transit_job() -> None:
    from .transit import sync_transit

    log.info("transit sync starting")
    try:
        result = sync_transit()
        log.info(result.summary())
    except Exception:  # pragma: no cover
        log.exception("transit sync failed")


def _has_transit_data() -> bool:
    try:
        from .transit import available_routes

        return bool(available_routes())
    except Exception:  # pragma: no cover
        return False


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        log.info("scheduler disabled")
        return None
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _job,
        trigger=IntervalTrigger(minutes=settings.sync_interval_minutes),
        id="sync",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    if settings.sync_on_startup:
        _scheduler.add_job(_job, id="sync_startup", max_instances=1)
    if settings.bods_api_key:
        _scheduler.add_job(
            _transit_job,
            trigger=IntervalTrigger(hours=max(1, settings.bods_refresh_hours)),
            id="transit",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        # Populate route/stop data on first run without blocking startup.
        if not _has_transit_data():
            _scheduler.add_job(_transit_job, id="transit_startup", max_instances=1)
    _scheduler.start()
    log.info(
        "scheduler started: every %d minutes", settings.sync_interval_minutes
    )
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
