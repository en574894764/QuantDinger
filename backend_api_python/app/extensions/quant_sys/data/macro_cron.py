"""Macro weekly cron — schedules macro data fetch every Sunday at 05:00.

Runs after financials (04:00). Uses APScheduler (same pattern as other crons).
Calls fetch_macro() which fetches 6 macroeconomic indicators from akshare → Parquet.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_scheduler = None


def _start_macro_scheduler():
    """Start APScheduler background scheduler for weekly macro fetch (idempotent).

    Schedule: Sunday at 05:00 Asia/Shanghai (after financials at 04:00).
    Calls fetch_macro() which fetches SHIBOR, LPR, PMI, CPI, M2, and
    10Y bond yield from akshare → Parquet.
    """
    global _scheduler
    if _scheduler is not None:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; macro cron disabled.")
        return

    _scheduler = BackgroundScheduler(daemon=True)

    def _macro_job():
        """Execute the weekly macro data fetch."""
        try:
            from app.extensions.quant_sys.data.macro_fetch import (
                fetch_macro,
            )

            result = fetch_macro()
            logger.info(
                "Macro cron: status=%s date=%s",
                result.get("status"),
                result.get("date"),
            )
            for name, info in result.get("fetched", {}).items():
                if "error" in info:
                    logger.error(
                        "Macro cron: %s FAILED — %s", name, info["error"]
                    )
                elif "skipped" in info:
                    logger.warning(
                        "Macro cron: %s skipped (%s)", name, info["skipped"]
                    )
                else:
                    logger.info(
                        "Macro cron: %s — %d rows, %d cols",
                        name,
                        info.get("rows", 0),
                        info.get("columns", 0),
                    )
        except Exception as e:
            logger.error("Macro cron failed: %s", e, exc_info=True)

    _scheduler.add_job(
        _macro_job,
        CronTrigger(
            day_of_week="sun",
            hour=5,
            minute=0,
            timezone="Asia/Shanghai",
        ),
        id="macro_fetch",
        name="宏观数据周度拉取",
        misfire_grace_time=7200,
    )

    _scheduler.start()
    logger.info(
        "Macro scheduler started (Sun 05:00 Asia/Shanghai, "
        "job ID: macro_fetch)"
    )


def register_macro_cron():
    """Public entry point — call once during app startup."""
    _start_macro_scheduler()