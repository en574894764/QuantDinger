"""Quant System data pipeline — A-share data access, K-line, fundamentals, macro."""

import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta

# Connect to quant_sys investassist DB (separate from QuantDinger's own DB)
QUANT_SYS_DB_URL = os.environ.get(
    "QUANT_SYS_DATABASE_URL",
    "postgresql://james@host.docker.internal:5432/investassist"
)


def _get_conn():
    return psycopg2.connect(QUANT_SYS_DB_URL)


def get_pipeline_status():
    """Return data pipeline status: table row counts, last trade date."""
    conn = _get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    tables = {
        "stock_basic": "stocks",
        "stock_daily": "daily_quote",
        "financials": "financial_indicator",
    }
    result = {}
    for key, table in tables.items():
        try:
            cur.execute(f"SELECT COUNT(*) as cnt FROM {table}")
            row = cur.fetchone()
            result[key] = {"count": row["cnt"]}
        except Exception:
            result[key] = {"count": 0}

        if key == "stock_daily":
            try:
                cur.execute(f"SELECT MAX(trade_date) as last_date FROM {table}")
                row = cur.fetchone()
                if row and row["last_date"]:
                    result[key]["last_date"] = str(row["last_date"])
            except Exception:
                pass

    # Row count for stocks (listed only)
    try:
        cur.execute("SELECT COUNT(*) as cnt FROM stocks WHERE list_status = 'L'")
        row = cur.fetchone()
        result["stock_basic"]["listed"] = row["cnt"]
    except Exception:
        pass

    cur.close()
    conn.close()
    return result


def get_kline_data(symbol: str = "", timeframe: str = "1D",
                   start: str = "", end: str = ""):
    """Get OHLCV K-line data for a symbol."""
    if not symbol:
        return {"error": "symbol required", "data": []}

    # Normalize ts_code
    if not symbol.endswith((".SH", ".SZ", ".BJ")):
        if symbol.startswith(("60", "68")):
            ts_code = f"{symbol}.SH"
        elif symbol.startswith(("00", "30")):
            ts_code = f"{symbol}.SZ"
        elif symbol.startswith("8"):
            ts_code = f"{symbol}.BJ"
        else:
            ts_code = symbol
    else:
        ts_code = symbol

    conn = _get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    query = """
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               change, pct_chg, vol, amount
        FROM daily_quote
        WHERE ts_code = %s
    """
    params = [ts_code]
    if start:
        query += " AND trade_date >= %s"
        params.append(start)
    if end:
        query += " AND trade_date <= %s"
        params.append(end)
    query += " ORDER BY trade_date ASC LIMIT 500"

    cur.execute(query, params)
    rows = cur.fetchall()

    result = []
    for r in rows:
        result.append({
            "ts_code": r["ts_code"],
            "trade_date": str(r["trade_date"]),
            "open": float(r["open"]) if r["open"] else None,
            "high": float(r["high"]) if r["high"] else None,
            "low": float(r["low"]) if r["low"] else None,
            "close": float(r["close"]) if r["close"] else None,
            "pre_close": float(r["pre_close"]) if r["pre_close"] else None,
            "change": float(r["change"]) if r["change"] else None,
            "pct_chg": float(r["pct_chg"]) if r["pct_chg"] else None,
            "vol": float(r["vol"]) if r["vol"] else None,
            "amount": float(r["amount"]) if r["amount"] else None,
        })

    cur.close()
    conn.close()
    return {"symbol": symbol, "ts_code": ts_code, "count": len(result), "data": result}


def get_fundamentals(symbol: str = ""):
    """Get latest financial indicators."""
    if not symbol:
        return {"error": "symbol required"}

    if not symbol.endswith((".SH", ".SZ", ".BJ")):
        if symbol.startswith(("60", "68")):
            ts_code = f"{symbol}.SH"
        elif symbol.startswith(("00", "30")):
            ts_code = f"{symbol}.SZ"
        else:
            ts_code = symbol
    else:
        ts_code = symbol

    conn = _get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT ts_code, ann_date, report_year, roe, roa,
               grossprofit_margin, debt_to_assets, current_ratio,
               netprofit_yoy, or_yoy, basic_eps, bps
        FROM financial_indicator
        WHERE ts_code = %s
        ORDER BY ann_date DESC LIMIT 4
    """, [ts_code])
    rows = cur.fetchall()
    result = []
    for r in rows:
        result.append({
            "ts_code": r["ts_code"],
            "ann_date": str(r["ann_date"]) if r["ann_date"] else None,
            "report_year": r["report_year"],
            "roe": float(r["roe"]) if r["roe"] else None,
            "roa": float(r["roa"]) if r["roa"] else None,
            "grossprofit_margin": float(r["grossprofit_margin"]) if r["grossprofit_margin"] else None,
            "debt_to_assets": float(r["debt_to_assets"]) if r["debt_to_assets"] else None,
            "current_ratio": float(r["current_ratio"]) if r["current_ratio"] else None,
            "netprofit_yoy": float(r["netprofit_yoy"]) if r["netprofit_yoy"] else None,
            "or_yoy": float(r["or_yoy"]) if r["or_yoy"] else None,
            "basic_eps": float(r["basic_eps"]) if r["basic_eps"] else None,
            "bps": float(r["bps"]) if r["bps"] else None,
        })
    cur.close()
    conn.close()
    return {"symbol": symbol, "count": len(result), "data": result}


def get_macro_data(data_type: str = ""):
    """Get macroeconomic indicators (stub — no macro table in investassist yet)."""
    return {"count": 0, "data": [], "note": "macro_data table not available in investassist"}