"""Financials weekly cron — schedules financial statement fetch every Sunday at 04:00.

Runs after backup (03:00) and before market open. Uses APScheduler (same pattern
as daily_pipeline_cron and index_cron).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_scheduler = None


def _start_financials_scheduler():
    """Start APScheduler background scheduler for weekly financials fetch (idempotent).

    Schedule: Sunday at 04:00 Asia/Shanghai.
    Calls fetch_financials() which fetches income, balance sheet, cashflow,
    and fina_indicator from tushare → PG + Parquet.
    """
    global _scheduler
    if _scheduler is not None:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; financials cron disabled.")
        return

    _scheduler = BackgroundScheduler(daemon=True)

    def _financials_job():
        """Execute the weekly financials fetch."""
        try:
            from app.extensions.quant_sys.data.financials_fetch import (
                fetch_financials,
            )

            result = fetch_financials()
            logger.info(
                "Financials cron: status=%s date=%s",
                result.get("status"),
                result.get("date"),
            )
            for name, info in result.get("fetched", {}).items():
                if "error" in info:
                    logger.error(
                        "Financials cron: %s FAILED — %s", name, info["error"]
                    )
                elif "skipped" in info:
                    logger.warning(
                        "Financials cron: %s skipped (%s)", name, info["skipped"]
                    )
                else:
                    logger.info(
                        "Financials cron: %s — %d rows (%d inserted)",
                        name,
                        info.get("rows", 0),
                        info.get("inserted", 0),
                    )
        except Exception as e:
            logger.error("Financials cron failed: %s", e, exc_info=True)

    _scheduler.add_job(
        _financials_job,
        CronTrigger(
            day_of_week="sun",
            hour=4,
            minute=0,
            timezone="Asia/Shanghai",
        ),
        id="financials_fetch",
        name="财务数据周度拉取",
        misfire_grace_time=7200,
    )

    _scheduler.start()
    logger.info(
        "Financials scheduler started (Sun 04:00 Asia/Shanghai, "
        "job ID: financials_fetch)"
    )


def register_financials_cron():
    """Public entry point — call once during app startup."""
    _start_financials_scheduler()