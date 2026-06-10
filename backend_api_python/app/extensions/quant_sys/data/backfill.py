"""Historical data backfill — fetch A-shares daily data from tushare for a date
range and store to both PostgreSQL and Parquet.

Ports the core logic from quant_sys/scripts/backfill_and_validate.py.
"""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Default: use the mounted quant_sys data directory in Docker
DEFAULT_DATA_DIR = Path(os.environ.get("QUANT_SYS_DATA_DIR", "/quant_sys_data"))
QUANT_SYS_DB_URL = os.environ.get(
    "QUANT_SYS_DATABASE_URL",
    "postgresql://james@host.docker.internal:5432/investassist",
)

# Progress tracking file (for async backfill monitoring)
PROGRESS_FILE = DEFAULT_DATA_DIR / "backfill_progress.json"


def _get_tushare_token() -> str:
    """Resolve tushare token from env or config file."""
    token = os.environ.get("TUSHARE_TOKEN", "")
    if token:
        return token

    config_paths = [
        "/quant_sys_config/data_sources.local.yaml",
        "/app/data_sources.local.yaml",
    ]
    for cfg_path in config_paths:
        if os.path.exists(cfg_path):
            try:
                import yaml

                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f)
                token = cfg.get("tushare", {}).get("token", "")
                if token:
                    logger.info("Loaded tushare token from %s", cfg_path)
                    return token
            except Exception as e:
                logger.warning("Failed to parse %s: %s", cfg_path, e)

    raise ValueError(
        "Tushare token not found. Set TUSHARE_TOKEN env var or ensure "
        "/quant_sys_config/data_sources.local.yaml is mounted."
    )


def _write_progress(trade_date: str, status: str, rows: int = 0, error: str = ""):
    """Write backfill progress to file for async monitoring."""
    import json

    progress = {}
    if PROGRESS_FILE.exists():
        try:
            progress = json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass

    progress.setdefault("dates", {})[trade_date] = {
        "status": status,
        "rows": rows,
        "error": error,
        "timestamp": datetime.now().isoformat(),
    }

    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


def _get_progress() -> dict:
    """Load current backfill progress."""
    import json

    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {"dates": {}}


def _clear_progress():
    """Clear the backfill progress file."""
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()


def run_backfill(
    start_date: str,
    end_date: str = "",
    market: str = "a_shares",
    clear_progress: bool = True,
) -> dict:
    """Backfill A-shares daily data for a date range from tushare → PG + Parquet.

    Args:
        start_date: Start date YYYYMMDD (required).
        end_date: End date YYYYMMDD (defaults to today).
        market: 'a_shares' (default), 'etf', 'hk_connect'.
        clear_progress: Clear previous progress file before starting.

    Returns:
        Dict with summary: total_dates, successful, failed, dates list.
    """
    from app.extensions.quant_sys.data.fetcher.tushare import TushareFetcher
    from app.extensions.quant_sys.data.store.parquet import ParquetStore
    from app.extensions.quant_sys.data.daily_pipeline import (
        _insert_daily_quote,
        _get_db_connection,
    )

    if not start_date:
        raise ValueError("start_date is required")

    if not end_date:
        end_date = datetime.now().strftime("%Y%m%d")

    if clear_progress:
        _clear_progress()

    logger.info(
        "Starting backfill: market=%s, %s → %s",
        market,
        start_date,
        end_date,
    )

    token = _get_tushare_token()
    fetcher = TushareFetcher(token)
    store = ParquetStore()

    # Generate list of trading dates (weekday check as rough filter)
    dates: list[str] = []
    d = datetime.strptime(start_date, "%Y%m%d")
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    while d <= end_dt:
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)

    logger.info("Generated %d candidate trading dates", len(dates))

    # Check trade calendar if possible (to filter out holidays)
    try:
        cal = fetcher.fetch_trade_cal(start_date, end_date)
        if not cal.empty and "cal_date" in cal.columns:
            valid_dates = set(
                cal[cal["is_open"] == 1]["cal_date"].str.replace("-", "")
            )
            dates = [dt for dt in dates if dt in valid_dates]
            logger.info("After trade_cal filter: %d trading dates", len(dates))
    except Exception as e:
        logger.warning(
            "Trade calendar check failed, using weekday dates: %s", e
        )

    summary = {
        "market": market,
        "start_date": start_date,
        "end_date": end_date,
        "total_dates": len(dates),
        "successful": 0,
        "failed": 0,
        "skipped": 0,
        "dates": [],
    }

    for trade_date in dates:
        try:
            # Determine fetch method and insert function based on market
            if market == "a_shares":
                df = fetcher.fetch_a_shares_daily(trade_date)
                insert_fn = _insert_daily_quote
                parquet_base = "a_shares/daily"
            elif market == "etf":
                df = fetcher.fetch_etf_daily(trade_date)
                from app.extensions.quant_sys.data.daily_pipeline import (
                    _insert_etf_quote,
                )
                insert_fn = _insert_etf_quote
                parquet_base = "etf/daily"
            elif market == "hk_connect":
                df = fetcher.fetch_hk_daily(trade_date)
                from app.extensions.quant_sys.data.daily_pipeline import (
                    _insert_hk_quote,
                )
                insert_fn = _insert_hk_quote
                parquet_base = "hk_connect/daily"
            else:
                raise ValueError(f"Unsupported market: {market}")

            if df.empty:
                logger.info("No data for %s — skipped", trade_date)
                summary["skipped"] += 1
                _write_progress(trade_date, "skipped", 0)
                summary["dates"].append(
                    {"date": trade_date, "status": "skipped", "rows": 0}
                )
                continue

            n_rows = len(df)
            conn = None
            try:
                conn = _get_db_connection()

                # Insert to PG
                n_pg = insert_fn(df, conn)
                logger.info(
                    "PG insert: %d rows for %s", n_pg, trade_date
                )

                # Write to Parquet
                path = f"{parquet_base}/date={trade_date}/data.parquet"
                store.write_raw(df, path)
                logger.info(
                    "Parquet write: %d rows for %s", n_rows, trade_date
                )

                summary["successful"] += 1
                _write_progress(trade_date, "success", n_rows)
                summary["dates"].append(
                    {
                        "date": trade_date,
                        "status": "success",
                        "rows": n_rows,
                    }
                )
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        except Exception as e:
            logger.error("Backfill failed for %s: %s", trade_date, e)
            summary["failed"] += 1
            _write_progress(trade_date, "failed", 0, str(e))
            summary["dates"].append(
                {
                    "date": trade_date,
                    "status": "failed",
                    "error": str(e),
                }
            )

    logger.info(
        "Backfill complete: %d success, %d failed, %d skipped / %d total",
        summary["successful"],
        summary["failed"],
        summary["skipped"],
        summary["total_dates"],
    )
    return summary