"""Macro data fetch — SHIBOR, LPR, PMI, CPI, M2, 10Y bond yield.

Runs weekly (Sunday) — macro data changes monthly, weekly refresh keeps cache warm.
Fetches from akshare and writes to Parquet.
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Macro indicator definitions: (name, fetcher_method, parquet_path, label)
MACRO_INDICATORS = [
    (
        "shibor",
        "fetch_shibor",
        "macro/shibor.parquet",
        "SHIBOR利率",
    ),
    (
        "lpr",
        "fetch_lpr",
        "macro/lpr.parquet",
        "LPR贷款利率",
    ),
    (
        "pmi",
        "fetch_pmi",
        "macro/pmi.parquet",
        "PMI采购经理指数",
    ),
    (
        "cpi",
        "fetch_cpi",
        "macro/cpi.parquet",
        "CPI居民消费价格指数",
    ),
    (
        "money_supply",
        "fetch_money_supply",
        "macro/money_supply.parquet",
        "货币供应量",
    ),
    (
        "bond_yield_10y",
        "fetch_bond_yield_10y",
        "macro/bond_yield_10y.parquet",
        "10年期国债收益率",
    ),
]


def fetch_macro(date: str = "") -> dict:
    """Fetch all 6 macroeconomic indicators from akshare and write to Parquet.

    Indicators:
      1. SHIBOR (上海银行间同业拆放利率)
      2. LPR (贷款市场报价利率)
      3. PMI (采购经理指数)
      4. CPI (居民消费价格指数)
      5. Money supply (货币供应量 M0/M1/M2)
      6. 10-year bond yield (10年期国债收益率)

    Args:
        date: Reference date YYYYMMDD (default: today).

    Returns:
        dict with status, date, and per-indicator fetch results.
    """
    from app.extensions.quant_sys.data.fetcher.akshare import AkshareFetcher
    from app.extensions.quant_sys.data.store.parquet import ParquetStore

    if not date:
        date = datetime.now().strftime("%Y%m%d")

    fetcher = AkshareFetcher()
    store = ParquetStore()

    result: dict = {"status": "ok", "date": date, "fetched": {}}

    for name, method_name, parquet_path, label in MACRO_INDICATORS:
        try:
            logger.info("Fetching %s (%s) ...", label, name)
            method = getattr(fetcher, method_name)
            df = method()

            if df is not None and not df.empty:
                store.write_raw(df, parquet_path)
                result["fetched"][name] = {
                    "rows": len(df),
                    "columns": len(df.columns),
                }
                logger.info(
                    "%s (%s): %d rows, %d columns → %s",
                    label,
                    name,
                    len(df),
                    len(df.columns),
                    parquet_path,
                )
            else:
                result["fetched"][name] = {"rows": 0, "skipped": "empty"}
                logger.warning("%s (%s): empty result", label, name)
        except Exception as e:
            result["fetched"][name] = {"error": str(e)}
            logger.error("%s (%s) fetch failed: %s", label, name, e)

    # Determine overall status
    errors = sum(
        1 for v in result["fetched"].values() if "error" in v
    )
    if errors == len(MACRO_INDICATORS):
        result["status"] = "error"
        result["error"] = "All macro indicators failed"
    elif errors > 0:
        result["status"] = "partial"
        result["error"] = f"{errors}/{len(MACRO_INDICATORS)} indicators failed"

    logger.info(
        "Macro fetch complete: status=%s, %d/6 indicators fetched",
        result["status"],
        len(MACRO_INDICATORS) - errors,
    )
    return result