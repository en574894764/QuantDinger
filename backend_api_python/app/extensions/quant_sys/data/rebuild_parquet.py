"""Rebuild Parquet files from PostgreSQL data.

Reads all daily_quote data from PG and rewrites it as date-partitioned
Parquet files under the raw storage directory. Supports incremental mode
(only dates not already present).
"""

import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Default: use the mounted quant_sys data directory in Docker
DEFAULT_DATA_DIR = Path(os.environ.get("QUANT_SYS_DATA_DIR", "/quant_sys_data"))
QUANT_SYS_DB_URL = os.environ.get(
    "QUANT_SYS_DATABASE_URL",
    "postgresql://james@host.docker.internal:5432/investassist",
)


def _get_db_connection():
    """Get a PostgreSQL connection to the investassist database."""
    return psycopg2.connect(QUANT_SYS_DB_URL)


def rebuild_a_shares_daily(
    incremental: bool = True,
    batch_size: int = 10,
    dates: Optional[list[str]] = None,
) -> dict:
    """Rebuild a_shares/daily Parquet partitions from PG daily_quote.

    Args:
        incremental: If True, only rebuild dates that don't already have
            Parquet files.
        batch_size: How many dates to read per PG query (to avoid OOM).
        dates: Optional explicit list of dates (YYYYMMDD) to rebuild.
               If None, all dates in PG are rebuilt.

    Returns:
        Dict with summary: total_dates, rebuilt, skipped, errors.
    """
    from app.extensions.quant_sys.data.store.parquet import ParquetStore

    store = ParquetStore()
    summary = {
        "category": "a_shares_daily",
        "total_dates": 0,
        "rebuilt": 0,
        "skipped": 0,
        "errors": 0,
        "details": [],
    }

    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get all dates from PG
        cur.execute(
            "SELECT DISTINCT trade_date FROM daily_quote ORDER BY trade_date"
        )
        pg_dates = [str(row["trade_date"]) for row in cur.fetchall()]

        if dates:
            pg_dates = [d for d in pg_dates if d in set(dates)]

        summary["total_dates"] = len(pg_dates)
        logger.info(
            "Found %d distinct trade dates in PG daily_quote", len(pg_dates)
        )

        # Check existing Parquet dates for incremental mode
        existing_dates: set[str] = set()
        if incremental:
            parquet_dir = DEFAULT_DATA_DIR / "raw" / "a_shares" / "daily"
            if parquet_dir.exists():
                for d in parquet_dir.glob("date=*"):
                    existing_dates.add(d.name.replace("date=", ""))
            logger.info(
                "Incremental mode: %d existing Parquet partitions",
                len(existing_dates),
            )

        # Process dates in batches
        for i in range(0, len(pg_dates), batch_size):
            batch = pg_dates[i : i + batch_size]

            for trade_date in batch:
                if incremental and trade_date in existing_dates:
                    summary["skipped"] += 1
                    logger.debug("Skipping %s (already exists)", trade_date)
                    continue

                try:
                    # Fetch data for this date
                    cur.execute(
                        """SELECT ts_code, trade_date, open, high, low, close,
                                  vol, amount, pre_close, change, pct_chg
                           FROM daily_quote
                           WHERE trade_date = %s""",
                        (trade_date,),
                    )
                    rows = cur.fetchall()

                    if not rows:
                        logger.debug("No rows for %s", trade_date)
                        summary["skipped"] += 1
                        continue

                    df = pd.DataFrame(rows)
                    n_rows = len(df)

                    # Write Parquet
                    parquet_path = (
                        f"a_shares/daily/date={trade_date}/data.parquet"
                    )
                    store.write_raw(df, parquet_path)

                    summary["rebuilt"] += 1
                    summary["details"].append(
                        {
                            "date": trade_date,
                            "rows": n_rows,
                            "status": "ok",
                        }
                    )
                    logger.info(
                        "Rebuilt %s: %d rows", trade_date, n_rows
                    )

                except Exception as e:
                    logger.error(
                        "Failed to rebuild %s: %s", trade_date, e
                    )
                    summary["errors"] += 1
                    summary["details"].append(
                        {
                            "date": trade_date,
                            "status": "error",
                            "error": str(e),
                        }
                    )

        cur.close()

    except Exception as e:
        logger.exception("Rebuild a_shares_daily failed: %s", e)
        summary["errors"] = -1
        summary["error"] = str(e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    logger.info(
        "Rebuild complete: %d rebuilt, %d skipped, %d errors / %d total",
        summary["rebuilt"],
        summary["skipped"],
        summary["errors"],
        summary["total_dates"],
    )
    return summary


def rebuild_stock_basic() -> dict:
    """Rebuild stock_basic Parquet from PG stocks table.

    Returns:
        Dict with summary: rows, status.
    """
    from app.extensions.quant_sys.data.store.parquet import ParquetStore

    store = ParquetStore()
    result = {"category": "stock_basic", "rows": 0, "status": "error"}

    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT * FROM stocks ORDER BY ts_code")
        rows = cur.fetchall()

        if not rows:
            result["status"] = "empty"
            return result

        df = pd.DataFrame(rows)
        result["rows"] = len(df)

        store.write_raw(df, "a_shares/stock_basic.parquet")
        result["status"] = "ok"

        cur.close()
    except Exception as e:
        logger.exception("rebuild_stock_basic failed: %s", e)
        result["error"] = str(e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return result


def rebuild_financials() -> dict:
    """Rebuild financial_indicator Parquet from PG financial_indicator table.

    Returns:
        Dict with summary: rows, status.
    """
    from app.extensions.quant_sys.data.store.parquet import ParquetStore

    store = ParquetStore()
    result = {"category": "financials", "rows": 0, "status": "error"}

    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT * FROM financial_indicator ORDER BY ann_date DESC")
        rows = cur.fetchall()

        if not rows:
            result["status"] = "empty"
            return result

        df = pd.DataFrame(rows)
        result["rows"] = len(df)

        store.write_raw(df, "a_shares/financial_indicator.parquet")
        result["status"] = "ok"

        cur.close()
    except Exception as e:
        logger.exception("rebuild_financials failed: %s", e)
        result["error"] = str(e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return result


def rebuild_all(category: str = "a_shares", **kwargs) -> dict:
    """Rebuild Parquet for a given data category.

    Args:
        category: One of 'a_shares', 'stock_basic', 'financials', 'all'.
        **kwargs: Passed to the individual rebuild function.

    Returns:
        Combined result dict.
    """
    logger.info("Starting Parquet rebuild: category=%s", category)

    if category == "a_shares":
        return rebuild_a_shares_daily(**kwargs)
    elif category == "stock_basic":
        return rebuild_stock_basic()
    elif category == "financials":
        return rebuild_financials()
    elif category == "all":
        results = {
            "a_shares": rebuild_a_shares_daily(**kwargs),
            "stock_basic": rebuild_stock_basic(),
            "financials": rebuild_financials(),
        }
        return results
    else:
        raise ValueError(
            f"Unknown category: {category}. "
            "Use: a_shares, stock_basic, financials, all"
        )