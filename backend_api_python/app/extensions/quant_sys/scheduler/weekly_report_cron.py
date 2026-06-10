"""Weekly report cron — schedules weekly report generation on Saturday at 10:00.

Uses APScheduler (same as backup, pipeline, and index refresh crons).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_scheduler = None


def _start_weekly_report_scheduler():
    """Start APScheduler background scheduler for weekly report (idempotent).

    Schedule: Saturday at 10:00 Asia/Shanghai.
    """
    global _scheduler
    if _scheduler is not None:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; weekly report cron disabled.")
        return

    _scheduler = BackgroundScheduler(daemon=True)

    def _weekly_report_job():
        """Execute weekly report generation."""
        try:
            from app.extensions.quant_sys.scheduler.weekly_report import save_report
            filepath = save_report()
            logger.info("Weekly report generated: %s", filepath)
        except Exception as e:
            logger.error("Weekly report cron failed: %s", e, exc_info=True)

    _scheduler.add_job(
        _weekly_report_job,
        CronTrigger(
            day_of_week="sat",
            hour=10,
            minute=0,
            timezone="Asia/Shanghai",
        ),
        id="weekly_report",
        name="周报生成",
        misfire_grace_time=3600,
    )

    _scheduler.start()
    logger.info(
        "Weekly report scheduler started (Sat 10:00 Asia/Shanghai, "
        "job ID: weekly_report)"
    )


def register_weekly_report_cron():
    """Public entry point — call once during app startup."""
    _start_weekly_report_scheduler()
