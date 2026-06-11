"""
Stock financial data from local PostgreSQL.

Routes:
  GET /api/stock/financials?symbol=600519    → PE/PB/ROE/ROA + growth rates
  GET /api/stock/income?symbol=600519        → latest income statement
  GET /api/stock/balance?symbol=600519       → latest balance sheet
  GET /api/stock/cashflow?symbol=600519      → latest cashflow statement
  GET /api/market/indices                    → A-share index snapshot
"""
from __future__ import annotations

import os
import traceback
from typing import Any, Dict, List, Optional

from flask import jsonify, request
from flask_smorest import Blueprint

from app.utils.logger import get_logger

logger = get_logger(__name__)

stock_financials_blp = Blueprint("stock_financials", __name__)

_LOCAL_DB_URL = os.environ.get(
    "QUANT_SYS_DATABASE_URL",
    "postgresql://james@host.docker.internal:5432/investassist",
)


def _get_pg_conn():
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(_LOCAL_DB_URL)


def _normalize_ts_code(symbol: str) -> tuple[str, str]:
    """Normalize symbol to ts_code suffix. Returns (ts_code, bare_code)."""
    s = symbol.strip().upper()
    if "." in s:
        bare = s.split(".")[0]
        suffix = s.split(".")[1].lower()
        if suffix in ("sh", "sz", "bj"):
            return s, bare
    # Guess suffix for 6-digit codes
    if len(bare := s) == 6:
        if bare.startswith(("6", "9")):
            return f"{bare}.SH", bare
        elif bare.startswith(("0", "2", "3")):
            return f"{bare}.SZ", bare
        elif bare.startswith(("4", "8")):
            return f"{bare}.BJ", bare
    return s, s


def _decimal_to_float(v) -> Optional[float]:
    if v is None:
        return None
    return round(float(v), 4)


@stock_financials_blp.route("/financials", methods=["GET"])
def get_financials():
    """Get latest financial indicators for a stock.

    Query: symbol=600519  (bare code or ts_code like 600519.SH)
    Returns: latest PE/PB/ROE/ROA, YoY growth, historical series.
    """
    try:
        symbol = (request.args.get("symbol") or "").strip()
        if not symbol:
            return jsonify({"code": 0, "msg": "symbol required", "data": None}), 400

        ts_code, bare = _normalize_ts_code(symbol)
        limit = min(int(request.args.get("limit") or 8), 20)

        conn = _get_pg_conn()
        cur = conn.cursor()
        try:
            # Latest financial_indicator
            cur.execute(
                """
                SELECT ann_date, report_year, report_type,
                       roe, roa, netprofit_yoy, or_yoy, tr_yoy,
                       grossprofit_margin, debt_to_assets, current_ratio,
                       basic_eps, bps, npta
                FROM financial_indicator
                WHERE ts_code = %s AND ann_date IS NOT NULL
                ORDER BY ann_date DESC
                LIMIT %s
                """,
                (ts_code, max(limit, 1)),
            )
            rows = cur.fetchall()
            # Convert to dicts
            cols = [
                "ann_date", "report_year", "report_type",
                "roe", "roa", "netprofit_yoy", "or_yoy", "tr_yoy",
                "grossprofit_margin", "debt_to_assets", "current_ratio",
                "basic_eps", "bps", "npta"
            ]
            history = []
            for row in rows:
                item = {}
                for i, col in enumerate(cols):
                    val = row[i]
                    if isinstance(val, (float, int)) and col not in ("report_year",):
                        val = _decimal_to_float(val)
                    elif hasattr(val, "isoformat"):
                        val = val.isoformat()
                    item[col] = val
                history.append(item)

            latest = history[0] if history else None
        finally:
            cur.close()
            conn.close()

        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {
                "symbol": bare,
                "ts_code": ts_code,
                "latest": latest,
                "history": history,
            },
        })
    except Exception as e:
        logger.error(f"stock_financials failed: {e}\n{traceback.format_exc()}")
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@stock_financials_blp.route("/income", methods=["GET"])
def get_income():
    """Get latest income statement for a stock."""
    try:
        symbol = (request.args.get("symbol") or "").strip()
        if not symbol:
            return jsonify({"code": 0, "msg": "symbol required", "data": None}), 400

        ts_code, bare = _normalize_ts_code(symbol)

        conn = _get_pg_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT ann_date, f_ann_date, report_year, report_type,
                       total_revenue, revenue, total_cogs, operate_profit,
                       total_profit, income_tax, n_income
                FROM income
                WHERE ts_code = %s AND f_ann_date IS NOT NULL
                ORDER BY f_ann_date DESC
                LIMIT 4
                """,
                (ts_code,),
            )
            rows = cur.fetchall()
            cols = [
                "ann_date", "f_ann_date", "report_year", "report_type",
                "total_revenue", "revenue", "total_cogs", "operate_profit",
                "total_profit", "income_tax", "n_income",
            ]
            records = []
            for row in rows:
                item = {}
                for i, col in enumerate(cols):
                    val = row[i]
                    if hasattr(val, "isoformat"):
                        val = val.isoformat()
                    elif isinstance(val, (float, int)):
                        val = _decimal_to_float(val)
                    item[col] = val
                records.append(item)
        finally:
            cur.close()
            conn.close()

        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {"symbol": bare, "ts_code": ts_code, "income": records},
        })
    except Exception as e:
        logger.error(f"stock_income failed: {e}\n{traceback.format_exc()}")
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@stock_financials_blp.route("/balance", methods=["GET"])
def get_balance():
    """Get latest balance sheet for a stock."""
    try:
        symbol = (request.args.get("symbol") or "").strip()
        if not symbol:
            return jsonify({"code": 0, "msg": "symbol required", "data": None}), 400

        ts_code, bare = _normalize_ts_code(symbol)

        conn = _get_pg_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT ann_date, f_ann_date, report_year, report_type,
                       total_assets, total_liab, total_hldr_eqy_exc_min_int,
                       total_cur_assets, total_cur_liab
                FROM balance_sheet
                WHERE ts_code = %s AND f_ann_date IS NOT NULL
                ORDER BY f_ann_date DESC
                LIMIT 4
                """,
                (ts_code,),
            )
            rows = cur.fetchall()
            cols = [
                "ann_date", "f_ann_date", "report_year", "report_type",
                "total_assets", "total_liab", "total_hldr_eqy_exc_min_int",
                "total_cur_assets", "total_cur_liab",
            ]
            records = []
            for row in rows:
                item = {}
                for i, col in enumerate(cols):
                    val = row[i]
                    if hasattr(val, "isoformat"):
                        val = val.isoformat()
                    elif isinstance(val, (float, int)):
                        val = _decimal_to_float(val)
                    item[col] = val
                records.append(item)
        finally:
            cur.close()
            conn.close()

        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {"symbol": bare, "ts_code": ts_code, "balance": records},
        })
    except Exception as e:
        logger.error(f"stock_balance failed: {e}\n{traceback.format_exc()}")
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@stock_financials_blp.route("/cashflow", methods=["GET"])
def get_cashflow():
    """Get latest cashflow statement for a stock."""
    try:
        symbol = (request.args.get("symbol") or "").strip()
        if not symbol:
            return jsonify({"code": 0, "msg": "symbol required", "data": None}), 400

        ts_code, bare = _normalize_ts_code(symbol)

        conn = _get_pg_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT ann_date, f_ann_date, report_year, report_type,
                       n_cashflow_act, n_cashflow_inv_act, n_cash_flows_fnc_act,
                       n_incr_cash_cash_equ
                FROM cashflow
                WHERE ts_code = %s AND f_ann_date IS NOT NULL
                ORDER BY f_ann_date DESC
                LIMIT 4
                """,
                (ts_code,),
            )
            rows = cur.fetchall()
            cols = [
                "ann_date", "f_ann_date", "report_year", "report_type",
                "n_cashflow_act", "n_cashflow_inv_act", "n_cash_flows_fnc_act",
                "n_incr_cash_cash_equ",
            ]
            records = []
            for row in rows:
                item = {}
                for i, col in enumerate(cols):
                    val = row[i]
                    if hasattr(val, "isoformat"):
                        val = val.isoformat()
                    elif isinstance(val, (float, int)):
                        val = _decimal_to_float(val)
                    item[col] = val
                records.append(item)
        finally:
            cur.close()
            conn.close()

        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {"symbol": bare, "ts_code": ts_code, "cashflow": records},
        })
    except Exception as e:
        logger.error(f"stock_cashflow failed: {e}\n{traceback.format_exc()}")
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500


@stock_financials_blp.route("/indices", methods=["GET"])
def get_indices():
    """Get latest A-share index snapshot (上证/深证/创业板/科创50/沪深300)."""
    try:
        conn = _get_pg_conn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT MAX(trade_date) FROM index_daily")
            latest_date = cur.fetchone()[0]

            cur.execute(
                """
                SELECT symbol, name, trade_date, close, pct_chg, volume, amount
                FROM index_daily
                WHERE trade_date = %s
                ORDER BY symbol
                """,
                (latest_date,),
            )
            rows = cur.fetchall()
            indices = []
            for row in rows:
                indices.append({
                    "symbol": row[0],
                    "name": row[1],
                    "trade_date": row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]),
                    "close": _decimal_to_float(row[3]),
                    "pct_chg": _decimal_to_float(row[4]),
                    "volume": _decimal_to_float(row[5]),
                    "amount": _decimal_to_float(row[6]),
                })
        finally:
            cur.close()
            conn.close()

        return jsonify({
            "code": 1,
            "msg": "success",
            "data": {"date": str(latest_date), "indices": indices},
        })
    except Exception as e:
        logger.error(f"indices failed: {e}\n{traceback.format_exc()}")
        return jsonify({"code": 0, "msg": str(e), "data": None}), 500
