"""Index daily data refresh — fetch key A-share indices from tushare → PG.

Backfill:  POST /api/quant/data/index/refresh?mode=backfill&start=20100101&end=20250606
Increment: POST /api/quant/data/index/refresh?mode=today  (last 7 days, idempotent)

Data is stored in the investassist PG database in the `index_daily` table.
Follows the same pattern as the original quant_sys scripts/fetch_index_data.py.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
QUANT_SYS_CONFIG_DIR = os.environ.get(
    "QUANT_SYS_CONFIG_DIR",
    "/Users/james/workspace/quant_sys/config",
)

QUANT_SYS_DB_URL = os.environ.get(
    "QUANT_SYS_DATABASE_URL",
    "postgresql://james@host.docker.internal:5432/investassist",
)

# Key indices — same as original fetch_index_data.py
KEY_INDICES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000016.SH": "上证50",
    "399005.SZ": "中小100",
    "000852.SH": "中证1000",
}

# Track last fetch timestamps for status reporting
_last_refresh: dict[str, str] = {}  # code -> ISO timestamp


# ---------------------------------------------------------------------------
# Tushare connection
# ---------------------------------------------------------------------------
def _get_tushare_pro():
    """Get tushare pro_api client from config file."""
    cfg_path = Path(QUANT_SYS_CONFIG_DIR) / "data_sources.local.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Tushare config not found at {cfg_path}. "
            f"Check QUANT_SYS_CONFIG_DIR env var."
        )

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    import tushare as ts
    ts.set_token(cfg["tushare"]["token"])
    return ts.pro_api()


def _get_pg_conn():
    """Get a psycopg2 connection to the investassist PG database."""
    return psycopg2.connect(QUANT_SYS_DB_URL)


# ---------------------------------------------------------------------------
# Schema management
# ---------------------------------------------------------------------------
INDEX_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS index_daily (
    id          BIGSERIAL PRIMARY KEY,
    symbol      VARCHAR(16)  NOT NULL,
    name        VARCHAR(32),
    trade_date  DATE         NOT NULL,
    open        NUMERIC(18,4),
    high        NUMERIC(18,4),
    low         NUMERIC(18,4),
    close       NUMERIC(18,4),
    pre_close   NUMERIC(18,4),
    change      NUMERIC(18,4),
    pct_chg     NUMERIC(18,4),
    volume      NUMERIC(24,2),
    amount      NUMERIC(24,2),
    created_at  TIMESTAMPTZ  DEFAULT now(),

    UNIQUE (symbol, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_index_daily_date
    ON index_daily (trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_index_daily_symbol
    ON index_daily (symbol);
"""


def _ensure_table():
    """Create index_daily table if it doesn't exist."""
    conn = _get_pg_conn()
    try:
        cur = conn.cursor()
        cur.execute(INDEX_DAILY_DDL)
        conn.commit()
        cur.close()
        logger.info("index_daily table ready")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fetch + store
# ---------------------------------------------------------------------------
def _fetch_single_index(pro, code: str, name: str,
                        start: str, end: str) -> pd.DataFrame:
    """Fetch one index's daily data from tushare."""
    df = pro.index_daily(ts_code=code, start_date=start, end_date=end)
    if df.empty:
        return df

    df["name"] = name
    df = df.rename(columns={
        "ts_code": "symbol",
        "trade_date": "trade_date",
        "vol": "volume",
        "amount": "amount",
        "pct_chg": "pct_chg",
    })
    cols = [
        "symbol", "name", "trade_date",
        "open", "high", "low", "close",
        "pre_close", "change", "pct_chg",
        "volume", "amount",
    ]
    return df[[c for c in cols if c in df.columns]]


def _upsert_index_data(df: pd.DataFrame) -> int:
    """Upsert index data into PG index_daily table (ON CONFLICT update)."""
    if df.empty:
        return 0

    # Convert trade_date to date objects
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

    conn = _get_pg_conn()
    inserted = 0
    try:
        cur = conn.cursor()
        for _, row in df.iterrows():
            cur.execute("""
                INSERT INTO index_daily
                    (symbol, name, trade_date, open, high, low, close,
                     pre_close, change, pct_chg, volume, amount)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, trade_date)
                DO UPDATE SET
                    name      = EXCLUDED.name,
                    open      = EXCLUDED.open,
                    high      = EXCLUDED.high,
                    low       = EXCLUDED.low,
                    close     = EXCLUDED.close,
                    pre_close = EXCLUDED.pre_close,
                    change    = EXCLUDED.change,
                    pct_chg   = EXCLUDED.pct_chg,
                    volume    = EXCLUDED.volume,
                    amount    = EXCLUDED.amount,
                    created_at = now()
            """, (
                row["symbol"], row["name"], row["trade_date"],
                float(row["open"]) if pd.notna(row["open"]) else None,
                float(row["high"]) if pd.notna(row["high"]) else None,
                float(row["low"]) if pd.notna(row["low"]) else None,
                float(row["close"]) if pd.notna(row["close"]) else None,
                float(row["pre_close"]) if pd.notna(row["pre_close"]) else None,
                float(row["change"]) if pd.notna(row["change"]) else None,
                float(row["pct_chg"]) if pd.notna(row["pct_chg"]) else None,
                float(row["volume"]) if pd.notna(row["volume"]) else None,
                float(row["amount"]) if pd.notna(row["amount"]) else None,
            ))
            inserted += 1
        conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return inserted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_backfill(start: str, end: str) -> dict:
    """Fetch index data for a date range and store in PG.

    Args:
        start: Start date as YYYYMMDD string.
        end: End date as YYYYMMDD string.

    Returns:
        Dict with status, per-index counts, and totals.
    """
    pro = _get_tushare_pro()
    _ensure_table()

    results: dict[str, dict] = {}
    all_frames: list[pd.DataFrame] = []

    for code, name in KEY_INDICES.items():
        key = f"{code} ({name})"
        try:
            df = _fetch_single_index(pro, code, name, start, end)
            if df.empty:
                results[key] = {"status": "no_data", "rows": 0}
                continue
            all_frames.append(df)
            results[key] = {"status": "fetched", "rows": len(df)}
        except Exception as e:
            results[key] = {"status": "error", "error": str(e)}
            logger.error("Error fetching %s: %s", code, e)

    if not all_frames:
        return {
            "success": True,
            "mode": "backfill",
            "start": start,
            "end": end,
            "total_fetched": 0,
            "total_stored": 0,
            "indices": results,
            "note": "No data fetched — tushare may be rate-limited or date range has no data",
        }

    combined = pd.concat(all_frames, ignore_index=True)
    stored = _upsert_index_data(combined)

    now_ts = pd.Timestamp.now(tz="Asia/Shanghai").isoformat()
    for code in KEY_INDICES:
        _last_refresh[code] = now_ts

    logger.info(
        "Index backfill complete: %d rows fetched, %d stored to PG",
        len(combined), stored,
    )

    return {
        "success": True,
        "mode": "backfill",
        "start": start,
        "end": end,
        "total_fetched": len(combined),
        "total_stored": stored,
        "indices": results,
    }


def run_daily_increment() -> dict:
    """Fetch the last 7 days of index data (idempotent — overwrites existing)."""
    today_s = date.today().strftime("%Y%m%d")
    week_ago_s = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
    result = run_backfill(week_ago_s, today_s)
    result["mode"] = "daily_increment"
    return result


def get_index_status() -> dict:
    """Return status of index data in PG: latest dates, row counts."""
    conn = _get_pg_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    indices_status: list[dict] = []
    total_rows = 0

    for code, name in KEY_INDICES.items():
        try:
            cur.execute(
                "SELECT COUNT(*) as cnt, MAX(trade_date) as last_date "
                "FROM index_daily WHERE symbol = %s",
                [code],
            )
            row = cur.fetchone()
            total_rows += row["cnt"] or 0
            indices_status.append({
                "symbol": code,
                "name": name,
                "count": row["cnt"] or 0,
                "last_date": str(row["last_date"]) if row["last_date"] else None,
                "last_refresh": _last_refresh.get(code),
            })
        except Exception as e:
            # Table might not exist yet
            indices_status.append({
                "symbol": code,
                "name": name,
                "count": 0,
                "last_date": None,
                "error": str(e),
            })

    cur.close()
    conn.close()

    return {
        "total_rows": total_rows,
        "indices": indices_status,
        "table": "index_daily",
    }