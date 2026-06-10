"""Financial statement fetch — income, balance sheet, cashflow, fina_indicator.

Runs weekly (Sunday) — these are large datasets that don't change daily.
Fetches from tushare and writes to PG + Parquet.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)


def _get_db_conn():
    """Get PostgreSQL connection to investassist."""
    url = os.environ.get(
        "QUANT_SYS_DATABASE_URL",
        "postgresql://james@host.docker.internal:5432/investassist",
    )
    return psycopg2.connect(url)


def _get_tushare_token() -> str:
    """Get tushare token: 1) env TUSHARE_TOKEN 2) config YAML file."""
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


def _insert_financial(df, conn, table: str) -> int:
    """Batch insert financial data. Uses ON CONFLICT DO NOTHING.

    Args:
        df: DataFrame with financial data.
        conn: psycopg2 connection.
        table: Target PG table name.

    Returns:
        Number of rows inserted.
    """
    cur = conn.cursor()

    columns = list(df.columns)
    rows = []
    for _, row in df.iterrows():
        rows.append(tuple(row[col] for col in columns if col in row.index))

    col_str = ", ".join(columns)
    execute_values(
        cur,
        f"INSERT INTO {table} ({col_str}) VALUES %s ON CONFLICT DO NOTHING",
        rows,
        page_size=500,
    )
    conn.commit()
    cur.close()
    return len(rows)


def fetch_financials(date: str = "") -> dict:
    """Fetch financial statements from tushare and write to PG + Parquet.

    Fetches 4 statement types:
      - income (利润表)
      - balance (资产负债表)
      - cashflow (现金流量表)
      - fina_indicator (财务指标)

    Args:
        date: Reference date YYYYMMDD (default: today).

    Returns:
        dict with status, date, and per-statement fetch results.
    """
    from app.extensions.quant_sys.data.fetcher.tushare import TushareFetcher
    from app.extensions.quant_sys.data.store.parquet import ParquetStore

    if not date:
        date = datetime.now().strftime("%Y%m%d")

    token = _get_tushare_token()
    ts = TushareFetcher(token)
    store = ParquetStore()

    # Look back 5 years for full history
    start_date = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y%m%d")

    result: dict = {"status": "ok", "date": date, "fetched": {}}
    conn = None

    try:
        conn = _get_db_conn()

        statements = [
            (
                "income",
                ts.fetch_income,
                "income_stmt",
                "a_shares/fundamental/income.parquet",
            ),
            (
                "balance",
                ts.fetch_balance_sheet,
                "balance_sheet",
                "a_shares/fundamental/balance.parquet",
            ),
            (
                "cashflow",
                ts.fetch_cashflow,
                "cashflow",
                "a_shares/fundamental/cashflow.parquet",
            ),
            (
                "fina_indicator",
                ts.fetch_fina_indicator,
                "financial_indicator",
                "a_shares/fundamental/fina_indicator.parquet",
            ),
        ]

        for name, method, table, parquet_path in statements:
            try:
                logger.info("Fetching %s ...", name)
                if name == "income":
                    df = method(start_date="20010101", end_date=date)
                else:
                    df = method(end_date=date)

                if df is not None and not df.empty:
                    n = _insert_financial(df, conn, table)
                    store.write_raw(df, parquet_path)
                    result["fetched"][name] = {"rows": len(df), "inserted": n}
                    logger.info(
                        "%s: %d rows fetched, %d inserted to %s",
                        name,
                        len(df),
                        n,
                        table,
                    )
                else:
                    result["fetched"][name] = {"rows": 0, "skipped": "empty"}
                    logger.warning("%s: empty result", name)
            except Exception as e:
                result["fetched"][name] = {"error": str(e)}
                logger.error("%s fetch failed: %s", name, e)

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        logger.exception("Financials fetch failed: %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return result