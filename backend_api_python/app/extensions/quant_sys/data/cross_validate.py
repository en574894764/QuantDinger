"""Cross-validate PG vs Parquet data consistency.

Compares daily_quote, stock_basic, and financial_indicator tables between
PostgreSQL and Parquet storage. Reports row counts, date ranges, and
missing dates as a JSON summary.
"""

import logging
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import pandas as pd

logger = logging.getLogger(__name__)

# Default: use the mounted quant_sys data directory in Docker
DEFAULT_DATA_DIR = Path(os.environ.get("QUANT_SYS_DATA_DIR", "/quant_sys_data"))
QUANT_SYS_DB_URL = os.environ.get(
    "QUANT_SYS_DATABASE_URL",
    "postgresql://james@host.docker.internal:5432/investassist",
)

# File to persist latest validation results
VALIDATION_RESULT_FILE = DEFAULT_DATA_DIR / "validation_results.json"


def _get_db_connection():
    """Get a PostgreSQL connection to the investassist database."""
    return psycopg2.connect(QUANT_SYS_DB_URL)


def validate_daily_quote(trade_date: str = "") -> dict:
    """Compare PG daily_quote vs Parquet for a given date.

    If trade_date is empty, uses today's date. Returns a dict with
    comparison stats.
    """
    import glob

    if not trade_date:
        trade_date = datetime.now().strftime("%Y%m%d")

    result = {
        "category": "daily_quote",
        "trade_date": trade_date,
        "pg": {"row_count": 0, "symbols": 0, "date_range": None},
        "parquet": {"row_count": 0, "symbols": 0, "date_range": None, "files_found": 0},
        "match": False,
        "issues": [],
    }

    # ── PG check ────────────────────────────────────────────────────────
    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Row count for the specific date
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM daily_quote WHERE trade_date = %s",
            (trade_date,),
        )
        row = cur.fetchone()
        result["pg"]["row_count"] = row["cnt"]

        # Distinct symbols for that date
        cur.execute(
            "SELECT COUNT(DISTINCT ts_code) AS cnt FROM daily_quote WHERE trade_date = %s",
            (trade_date,),
        )
        row = cur.fetchone()
        result["pg"]["symbols"] = row["cnt"]

        # Overall date range
        cur.execute("SELECT MIN(trade_date) AS min_d, MAX(trade_date) AS max_d FROM daily_quote")
        row = cur.fetchone()
        if row["min_d"] and row["max_d"]:
            result["pg"]["date_range"] = {
                "min": str(row["min_d"]),
                "max": str(row["max_d"]),
            }

        cur.close()
    except Exception as e:
        logger.error("PG daily_quote validation failed: %s", e)
        result["issues"].append(f"PG error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    # ── Parquet check ───────────────────────────────────────────────────
    try:
        parquet_dir = DEFAULT_DATA_DIR / "raw" / "a_shares" / "daily"
        if parquet_dir.exists():
            # List all date partitions
            date_dirs = sorted(parquet_dir.glob("date=*"))
            result["parquet"]["files_found"] = len(date_dirs)

            if date_dirs:
                # Date range
                dates = [d.name.replace("date=", "") for d in date_dirs]
                result["parquet"]["date_range"] = {
                    "min": min(dates),
                    "max": max(dates),
                }

                # Row count for the requested date
                target_dir = parquet_dir / f"date={trade_date}"
                target_file = target_dir / "data.parquet"
                if target_file.exists():
                    df = pd.read_parquet(target_file)
                    result["parquet"]["row_count"] = len(df)
                    result["parquet"]["symbols"] = df["ts_code"].nunique() if "ts_code" in df.columns else 0
                else:
                    result["issues"].append(f"Parquet file missing for {trade_date}")
            else:
                result["issues"].append("No Parquet date partitions found")
        else:
            result["issues"].append("Parquet directory not found: %s" % parquet_dir)
    except Exception as e:
        logger.error("Parquet daily_quote validation failed: %s", e)
        result["issues"].append(f"Parquet error: {e}")

    # ── Comparison ──────────────────────────────────────────────────────
    if result["pg"]["row_count"] > 0 and result["parquet"]["row_count"] > 0:
        if result["pg"]["row_count"] == result["parquet"]["row_count"]:
            result["match"] = True
        else:
            diff = abs(result["pg"]["row_count"] - result["parquet"]["row_count"])
            result["issues"].append(
                f"Row count mismatch: PG={result['pg']['row_count']} vs Parquet={result['parquet']['row_count']} (diff={diff})"
            )

    return result


def validate_stocks() -> dict:
    """Compare PG stocks vs Parquet stock_basic.

    Returns a dict with row counts and comparison info.
    """
    result = {
        "category": "stock_basic",
        "pg": {"row_count": 0, "listed": 0, "delisted": 0, "paused": 0},
        "parquet": {"row_count": 0},
        "match": False,
        "issues": [],
    }

    # ── PG check ────────────────────────────────────────────────────────
    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT COUNT(*) AS cnt FROM stocks")
        row = cur.fetchone()
        result["pg"]["row_count"] = row["cnt"]

        # By list status
        for status, key in [("L", "listed"), ("D", "delisted"), ("P", "paused")]:
            try:
                cur.execute(
                    "SELECT COUNT(*) AS cnt FROM stocks WHERE list_status = %s",
                    (status,),
                )
                row = cur.fetchone()
                result["pg"][key] = row["cnt"]
            except Exception:
                pass

        cur.close()
    except Exception as e:
        logger.error("PG stocks validation failed: %s", e)
        result["issues"].append(f"PG error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    # ── Parquet check ───────────────────────────────────────────────────
    try:
        stock_file = DEFAULT_DATA_DIR / "raw" / "a_shares" / "stock_basic.parquet"
        if stock_file.exists():
            df = pd.read_parquet(stock_file)
            result["parquet"]["row_count"] = len(df)

            # Match
            if result["pg"]["row_count"] > 0 and result["parquet"]["row_count"] > 0:
                if abs(result["pg"]["row_count"] - result["parquet"]["row_count"]) <= 5:
                    result["match"] = True
                else:
                    result["issues"].append(
                        f"Row count mismatch: PG={result['pg']['row_count']} vs Parquet={result['parquet']['row_count']}"
                    )
        else:
            result["issues"].append("Parquet stock_basic file not found")
    except Exception as e:
        logger.error("Parquet stocks validation failed: %s", e)
        result["issues"].append(f"Parquet error: {e}")

    return result


def validate_financials() -> dict:
    """Compare PG financial_indicator vs Parquet financials.

    Returns a dict with row counts, date ranges, and comparison info.
    """
    result = {
        "category": "financials",
        "pg": {"row_count": 0, "symbols": 0, "date_range": None},
        "parquet": {"row_count": 0, "date_range": None},
        "match": False,
        "issues": [],
    }

    # ── PG check ────────────────────────────────────────────────────────
    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT COUNT(*) AS cnt FROM financial_indicator")
        row = cur.fetchone()
        result["pg"]["row_count"] = row["cnt"]

        cur.execute(
            "SELECT COUNT(DISTINCT ts_code) AS cnt FROM financial_indicator"
        )
        row = cur.fetchone()
        result["pg"]["symbols"] = row["cnt"]

        cur.execute(
            "SELECT MIN(ann_date) AS min_d, MAX(ann_date) AS max_d FROM financial_indicator"
        )
        row = cur.fetchone()
        if row["min_d"] and row["max_d"]:
            result["pg"]["date_range"] = {
                "min": str(row["min_d"]),
                "max": str(row["max_d"]),
            }

        cur.close()
    except Exception as e:
        logger.error("PG financials validation failed: %s", e)
        result["issues"].append(f"PG error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    # ── Parquet check ───────────────────────────────────────────────────
    try:
        fin_file = DEFAULT_DATA_DIR / "raw" / "a_shares" / "financial_indicator.parquet"
        if fin_file.exists():
            df = pd.read_parquet(fin_file)
            result["parquet"]["row_count"] = len(df)

            if "ann_date" in df.columns:
                result["parquet"]["date_range"] = {
                    "min": str(df["ann_date"].min()),
                    "max": str(df["ann_date"].max()),
                }

            if result["pg"]["row_count"] > 0 and result["parquet"]["row_count"] > 0:
                if abs(result["pg"]["row_count"] - result["parquet"]["row_count"]) <= 5:
                    result["match"] = True
                else:
                    result["issues"].append(
                        f"Row count mismatch: PG={result['pg']['row_count']} vs Parquet={result['parquet']['row_count']}"
                    )
        else:
            result["issues"].append("Parquet financial_indicator file not found")
    except Exception as e:
        logger.error("Parquet financials validation failed: %s", e)
        result["issues"].append(f"Parquet error: {e}")

    return result


def validate_missing_dates(days_back: int = 30) -> dict:
    """Check PG daily_quote for missing trading dates in the recent window.

    Args:
        days_back: Number of calendar days to look back.

    Returns a dict with expected vs found date counts.
    """
    result = {
        "category": "missing_dates",
        "days_back": days_back,
        "expected_dates": 0,
        "found_dates": 0,
        "missing_dates": [],
        "issues": [],
    }

    conn = None
    try:
        conn = _get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back)

        # Get all dates with data in the window
        cur.execute(
            """SELECT DISTINCT trade_date FROM daily_quote
               WHERE trade_date >= %s AND trade_date <= %s
               ORDER BY trade_date""",
            (start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")),
        )
        found = [str(row["trade_date"]) for row in cur.fetchall()]

        # Generate all calendar dates in range
        all_dates = []
        d = start_date
        while d <= end_date:
            all_dates.append(d.strftime("%Y%m%d"))
            d += timedelta(days=1)

        # Weekdays only (Mon=0, Sun=6) — rough approximation
        # Weekends are not expected to have data
        expected = [
            dt
            for dt in all_dates
            if datetime.strptime(dt, "%Y%m%d").weekday() < 5
        ]
        result["expected_dates"] = len(expected)

        found_set = set(found)
        missing = [dt for dt in expected if dt not in found_set]
        result["found_dates"] = len(found)
        result["missing_dates"] = missing

        cur.close()
    except Exception as e:
        logger.error("Missing dates validation failed: %s", e)
        result["issues"].append(f"Error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return result


def validate_all(date: str = "") -> dict:
    """Run all cross-validations and return a summary JSON dict.

    Args:
        date: Trade date for daily_quote validation (YYYYMMDD). Default: today.

    Returns:
        Dict with validation results, timestamp, and overall pass/fail status.
    """
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    logger.info("Running full cross-validation for date=%s", date)

    results = {
        "timestamp": datetime.now().isoformat(),
        "target_date": date,
        "overall_pass": True,
        "checks": {},
    }

    # Daily quote comparison
    try:
        results["checks"]["daily_quote"] = validate_daily_quote(date)
    except Exception as e:
        logger.exception("daily_quote validation crashed")
        results["checks"]["daily_quote"] = {
            "category": "daily_quote",
            "error": str(e),
            "match": False,
        }

    # Stock basic comparison
    try:
        results["checks"]["stock_basic"] = validate_stocks()
    except Exception as e:
        logger.exception("stock_basic validation crashed")
        results["checks"]["stock_basic"] = {
            "category": "stock_basic",
            "error": str(e),
            "match": False,
        }

    # Financials comparison
    try:
        results["checks"]["financials"] = validate_financials()
    except Exception as e:
        logger.exception("financials validation crashed")
        results["checks"]["financials"] = {
            "category": "financials",
            "error": str(e),
            "match": False,
        }

    # Missing dates check
    try:
        results["checks"]["missing_dates"] = validate_missing_dates()
    except Exception as e:
        logger.exception("missing_dates validation crashed")
        results["checks"]["missing_dates"] = {
            "category": "missing_dates",
            "error": str(e),
        }

    # Determine overall pass/fail
    for category, check in results["checks"].items():
        if not check.get("match", True):
            results["overall_pass"] = False
        if check.get("issues"):
            results["overall_pass"] = False

    # Persist results to disk
    try:
        VALIDATION_RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        VALIDATION_RESULT_FILE.write_text(json.dumps(results, indent=2, default=str))
    except Exception as e:
        logger.warning("Failed to persist validation results: %s", e)

    logger.info(
        "Cross-validation complete: overall_pass=%s, checks=%d",
        results["overall_pass"],
        len(results["checks"]),
    )
    return results


def get_latest_validation() -> dict:
    """Load the most recent validation results from disk.

    Returns an empty dict if no results file exists.
    """
    if VALIDATION_RESULT_FILE.exists():
        try:
            return json.loads(VALIDATION_RESULT_FILE.read_text())
        except Exception as e:
            logger.warning("Failed to read validation results: %s", e)
    return {}