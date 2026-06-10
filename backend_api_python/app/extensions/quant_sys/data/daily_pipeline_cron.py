"""Daily pipeline cron — schedules A-share daily data pipeline at 17:00 on weekdays.

Replaces the quant_sys daemon's daily_pipeline job. Uses APScheduler (same as
backup and index refresh crons).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_scheduler = None


def _start_pipeline_scheduler():
    """Start APScheduler background scheduler for daily pipeline (idempotent).

    Schedule: Monday–Friday at 17:00 Asia/Shanghai.
    Runs run_daily_pipeline() which fetches A-shares daily from tushare,
    writes to PG + Parquet, validates, cleans, and saves pipeline state.
    """
    global _scheduler
    if _scheduler is not None:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; daily pipeline cron disabled.")
        return

    _scheduler = BackgroundScheduler(daemon=True)

    def _pipeline_job():
        """Execute the daily pipeline."""
        try:
            from app.extensions.quant_sys.data.daily_pipeline import (
                run_daily_pipeline,
            )
            result = run_daily_pipeline()
            logger.info(
                "Daily pipeline: status=%s date=%s missing=%s",
                result.get("status"),
                result.get("date"),
                result.get("missing"),
            )
            if result.get("status") == "failed":
                logger.error(
                    "Daily pipeline FAILED: %s", result.get("error", "unknown")
                )
        except Exception as e:
            logger.error("Daily pipeline cron failed: %s", e, exc_info=True)

    _scheduler.add_job(
        _pipeline_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=17,
            minute=5,  # 5 min after index refresh
            timezone="Asia/Shanghai",
        ),
        id="daily_pipeline",
        name="A股日线数据管道",
        misfire_grace_time=3600,
    )

    _scheduler.start()
    logger.info(
        "Daily pipeline scheduler started (Mon–Fri 17:05 Asia/Shanghai, "
        "job ID: daily_pipeline)"
    )


def register_pipeline_cron():
    """Public entry point — call once during app startup."""
    _start_pipeline_scheduler()