"""Index data refresh cron — schedules daily index data refresh at 17:00 on weekdays.

Uses APScheduler (same as backup cron). Refreshes the last 7 days of index data
(idempotent — uses ON CONFLICT upsert). Equivalent to the Hermes cron job #8
'指数数据每日刷新' (ID: 5671cd09403e) which ran:
    cd /Users/james/workspace/quant_sys && PYTHONPATH=. .venv/bin/python scripts/fetch_index_data.py --today
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_scheduler = None


def _start_index_refresh_scheduler():
    """Start APScheduler background scheduler for index data refresh (idempotent).

    Schedule: Monday–Friday at 17:00 Asia/Shanghai.
    """
    global _scheduler
    if _scheduler is not None:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; index refresh cron disabled.")
        return

    _scheduler = BackgroundScheduler(daemon=True)

    def _refresh_job():
        """Execute the daily index refresh."""
        try:
            from app.extensions.quant_sys.data.index_refresh import (
                run_daily_increment,
            )
            result = run_daily_increment()
            logger.info(
                "Index refresh cron: fetched=%d stored=%d",
                result.get("total_fetched", 0),
                result.get("total_stored", 0),
            )
            if result.get("total_fetched", 0) == 0:
                logger.warning(
                    "Index refresh cron: no data fetched — tushare may be "
                    "rate-limited or today is a non-trading day"
                )
        except Exception as e:
            logger.error("Index refresh cron failed: %s", e, exc_info=True)

    _scheduler.add_job(
        _refresh_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=17,
            minute=0,
            timezone="Asia/Shanghai",
        ),
        id="index_refresh_daily",
        name="指数数据每日刷新",
        misfire_grace_time=3600,
    )

    _scheduler.start()
    logger.info(
        "Index refresh scheduler started (Mon–Fri 17:00 Asia/Shanghai, "
        "job ID: index_refresh_daily)"
    )


def register_index_cron():
    """Public entry point — call once during app startup."""
    _start_index_refresh_scheduler()