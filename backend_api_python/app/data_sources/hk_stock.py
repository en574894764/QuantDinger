"""
港股/H股数据源 — 多层 fallback

有 TWELVE_DATA_API_KEY:
  所有周期 → Twelve Data（主） → 腾讯日/周线 → yfinance → AkShare

无 API Key:
  分钟/小时 → yfinance → AkShare
  日/周线 → 腾讯 fqkline → yfinance → AkShare
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.tencent import normalize_hk_code, fetch_quote, parse_quote_to_ticker, fetch_kline, tencent_kline_rows_to_dicts
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
    fetch_twelvedata_klines,
    fetch_yfinance_klines,
    fetch_akshare_minute_klines,
    fetch_akshare_weekly_klines,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Local PG helpers (hk_quote is in quant_sys DB, not investassist) ──

_HK_PG_DB_URL = os.environ.get(
    "QUANT_SYS_DATABASE_URL",
    "postgresql://james@host.docker.internal:5432/investassist"
).replace("/investassist", "/quant_sys")


def _get_hk_pg_conn():
    import psycopg2
    import psycopg2.extras
    return psycopg2.connect(_HK_PG_DB_URL)


def _hk_code_to_pg_ts_code(tencent_code: str) -> str:
    """Convert hk00700 → 00700.HK for DB query."""
    if tencent_code.lower().startswith("hk") and tencent_code[2:].isdigit():
        digits = tencent_code[2:]
        return digits + ".HK"
    return tencent_code


def _fetch_hk_kline_from_local_pg(
    tencent_code: str,
    timeframe: str,
    limit: int,
    before_time: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Query hk_quote from local PostgreSQL (quant_sys DB).

    Only supports 1D and 1W timeframes. Returns [] otherwise.
    """
    if timeframe not in ("1D", "1W"):
        return []

    ts_code = _hk_code_to_pg_ts_code(tencent_code)

    try:
        conn = _get_hk_pg_conn()
        cur = conn.cursor()
    except Exception:
        logger.debug("Local PG not reachable for HK, skipping", exc_info=True)
        return []

    try:
        if before_time:
            before_dt = datetime.fromtimestamp(before_time, tz=timezone.utc)
            before_date = before_dt.strftime("%Y-%m-%d")
            cur.execute(
                """SELECT trade_date, open, high, low, close, vol
                   FROM hk_quote
                   WHERE ts_code = %s AND trade_date < %s
                   ORDER BY trade_date DESC
                   LIMIT %s""",
                (ts_code, before_date, limit)
            )
        else:
            cur.execute(
                """SELECT trade_date, open, high, low, close, vol
                   FROM hk_quote
                   WHERE ts_code = %s
                   ORDER BY trade_date DESC
                   LIMIT %s""",
                (ts_code, limit)
            )

        rows = cur.fetchall()
        if not rows:
            return []

        out = []
        for trade_date, open_p, high_p, low_p, close_p, vol in rows:
            dt = datetime.combine(trade_date, datetime.min.time(), tzinfo=timezone.utc)
            ts = int(dt.timestamp())
            out.append({
                "time": ts,
                "open": float(open_p),
                "high": float(high_p),
                "low": float(low_p),
                "close": float(close_p),
                "volume": float(vol),
            })

        out.sort(key=lambda x: x["time"])
        return out

    except Exception:
        logger.debug("Local PG HK query failed, falling through", exc_info=True)
        return []
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass


class HKStockDataSource(BaseDataSource):
    """港股/H股数据源（TwelveData + Tencent + yfinance + AkShare）"""

    name = "HKStock/multi-source"

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = normalize_hk_code(symbol)
        parts = fetch_quote(code)
        if not parts:
            return {"last": 0, "symbol": code}
        t = parse_quote_to_ticker(parts)
        return {
            "last": t.get("last", 0),
            "change": t.get("change", 0),
            "changePercent": t.get("changePercent", 0),
            "high": t.get("high", 0),
            "low": t.get("low", 0),
            "open": t.get("open", 0),
            "previousClose": t.get("previousClose", 0),
            "name": t.get("name", ""),
            "symbol": code,
        }

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        code = normalize_hk_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # Tier 0: Local PG (quant_sys.hk_quote — fastest, most reliable for daily/weekly)
        rows = _fetch_hk_kline_from_local_pg(
            tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 1: Twelve Data (paid, most reliable)
        rows = fetch_twelvedata_klines(
            is_hk=True, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 2: Tencent for daily/weekly (fast, free)
        if tf in ("1D", "1W"):
            tf_map = {"1D": "day", "1W": "week"}
            period = tf_map.get(tf, "day")
            raw_rows = fetch_kline(code, period=period, count=lim, adj="qfq")
            out = tencent_kline_rows_to_dicts(raw_rows)
            if out:
                return self.filter_and_limit(
                    out,
                    limit=lim,
                    before_time=before_time,
                    after_time=after_time,
                    truncate=(after_time is None),
                )

        # Tier 3: yfinance (works when Yahoo not rate-limited)
        rows = fetch_yfinance_klines(
            is_hk=True, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 4: AkShare (fragile overseas, last resort)
        if tf in ("1m", "5m", "15m", "30m", "1H", "4H"):
            rows = fetch_akshare_minute_klines(
                is_hk=True, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
            )
        elif tf == "1W":
            rows = fetch_akshare_weekly_klines(
                is_hk=True, tencent_code=code, limit=lim, before_time=before_time
            )
        else:
            rows = []

        return self.filter_and_limit(
            rows,
            limit=lim,
            before_time=before_time,
            after_time=after_time,
            truncate=(after_time is None),
        )
