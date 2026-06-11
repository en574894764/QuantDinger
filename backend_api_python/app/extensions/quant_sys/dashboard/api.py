"""Dashboard API — core data access logic for market overview, portfolio,
risk dashboard, and backtest summary.

Reads from PG (investassist) and Parquet storage. Designed to be called
from Flask route handlers.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

QUANT_SYS_DB_URL = os.environ.get(
    "QUANT_SYS_DATABASE_URL",
    "postgresql://james@host.docker.internal:5432/investassist",
)


def _get_db_connection():
    """Get a PostgreSQL connection to the investassist database."""
    import psycopg2

    return psycopg2.connect(QUANT_SYS_DB_URL)


# ── Market Overview ──────────────────────────────────────────────────────────


def get_market_overview() -> dict:
    """Get A-shares market overview: indices, hot sectors, top movers.

    Reads from PG daily_quote to compute:
    - Market breadth (advance/decline)
    - Top gainers and losers
    - Volume leaders
    - Recent market aggregate stats

    Index data tries the index_daily table first, falls back to daily_quote
    analysis.
    """
    conn = None
    overview: dict = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "indices": {},
        "breadth": {"advancing": 0, "declining": 0, "flat": 0, "total": 0},
        "top_gainers": [],
        "top_losers": [],
        "volume_leaders": [],
        "sector_performance": [],
    }

    try:
        conn = _get_db_connection()
        import psycopg2.extras

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Get latest trade date
        cur.execute("SELECT MAX(trade_date) AS latest FROM daily_quote")
        row = cur.fetchone()
        if not row or not row["latest"]:
            cur.close()
            return overview

        latest_date = str(row["latest"])
        # Parse date robustly — PG may return "2026-06-08", "20260608", or datetime obj
        _ld = str(latest_date)
        if "-" in _ld:
            overview["date"] = _ld[:10]  # "2026-06-08" or "2026-06-08 00:00:00" → "2026-06-08"
        elif len(_ld) >= 8:
            overview["date"] = f"{_ld[:4]}-{_ld[4:6]}-{_ld[6:8]}"
        else:
            overview["date"] = _ld

        # ── Breadth: advance/decline on latest date ─────────────────
        cur.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN change > 0 THEN 1 ELSE 0 END) AS advancing,
                 SUM(CASE WHEN change < 0 THEN 1 ELSE 0 END) AS declining,
                 SUM(CASE WHEN change = 0 THEN 1 ELSE 0 END) AS flat
               FROM daily_quote
               WHERE trade_date = %s""",
            (latest_date,),
        )
        row = cur.fetchone()
        if row:
            overview["breadth"] = {
                "advancing": row["advancing"] or 0,
                "declining": row["declining"] or 0,
                "flat": row["flat"] or 0,
                "total": row["total"] or 0,
            }

        # ── Top gainers (top 10 by pct_chg) ─────────────────────────
        cur.execute(
            """SELECT ts_code, close, pct_chg, vol, amount
               FROM daily_quote
               WHERE trade_date = %s
                 AND pct_chg IS NOT NULL
               ORDER BY pct_chg DESC
               LIMIT 10""",
            (latest_date,),
        )
        overview["top_gainers"] = [
            {
                "ts_code": r["ts_code"],
                "close": float(r["close"]) if r["close"] else 0,
                "pct_chg": float(r["pct_chg"]) if r["pct_chg"] else 0,
                "vol": float(r["vol"]) if r["vol"] else 0,
                "amount": float(r["amount"]) if r["amount"] else 0,
            }
            for r in cur.fetchall()
        ]

        # ── Top losers (bottom 10 by pct_chg) ───────────────────────
        cur.execute(
            """SELECT ts_code, close, pct_chg, vol, amount
               FROM daily_quote
               WHERE trade_date = %s
                 AND pct_chg IS NOT NULL
               ORDER BY pct_chg ASC
               LIMIT 10""",
            (latest_date,),
        )
        overview["top_losers"] = [
            {
                "ts_code": r["ts_code"],
                "close": float(r["close"]) if r["close"] else 0,
                "pct_chg": float(r["pct_chg"]) if r["pct_chg"] else 0,
                "vol": float(r["vol"]) if r["vol"] else 0,
                "amount": float(r["amount"]) if r["amount"] else 0,
            }
            for r in cur.fetchall()
        ]

        # ── Volume leaders (top 10 by amount) ───────────────────────
        cur.execute(
            """SELECT ts_code, close, pct_chg, vol, amount
               FROM daily_quote
               WHERE trade_date = %s
                 AND amount IS NOT NULL
               ORDER BY amount DESC
               LIMIT 10""",
            (latest_date,),
        )
        overview["volume_leaders"] = [
            {
                "ts_code": r["ts_code"],
                "close": float(r["close"]) if r["close"] else 0,
                "pct_chg": float(r["pct_chg"]) if r["pct_chg"] else 0,
                "vol": float(r["vol"]) if r["vol"] else 0,
                "amount": float(r["amount"]) if r["amount"] else 0,
            }
            for r in cur.fetchall()
        ]

        # ── Sector performance (by industry from stocks table) ──────
        cur.execute(
            """SELECT s.industry, COUNT(*) AS stock_count,
                      AVG(d.pct_chg) AS avg_pct_chg,
                      AVG(d.amount) AS avg_amount
               FROM stocks s
               JOIN daily_quote d ON s.ts_code = d.ts_code
               WHERE d.trade_date = %s
                 AND s.industry IS NOT NULL AND s.industry != ''
                 AND d.pct_chg IS NOT NULL
               GROUP BY s.industry
               ORDER BY AVG(d.pct_chg) DESC
               LIMIT 20""",
            (latest_date,),
        )
        overview["sector_performance"] = [
            {
                "industry": r["industry"],
                "stock_count": r["stock_count"],
                "avg_pct_chg": float(r["avg_pct_chg"]) if r["avg_pct_chg"] else 0,
                "avg_amount": float(r["avg_amount"]) if r["avg_amount"] else 0,
            }
            for r in cur.fetchall()
        ]

        # ── Index data (try index_daily table) ──────────────────────
        try:
            cur.execute(
                """SELECT i.symbol, i.name, i.trade_date, i.close, i.pct_chg, i.volume, i.amount
                   FROM index_daily i
                   WHERE i.trade_date = %s
                   ORDER BY i.symbol""",
                (latest_date,),
            )
            for r in cur.fetchall():
                overview["indices"][r["symbol"]] = {
                    "name": r["name"],
                    "close": float(r["close"]) if r["close"] else 0,
                    "pct_chg": float(r["pct_chg"]) if r["pct_chg"] else 0,
                    "vol": float(r["volume"]) if r["volume"] else 0,
                }
        except Exception as e:
            logger.error("index_daily query failed: %s", e)

        cur.close()
    except Exception as e:
        logger.error("Market overview failed: %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return overview


# ── Portfolio Summary ────────────────────────────────────────────────────────


def get_portfolio_summary() -> dict:
    """Get simplified portfolio summary from positions + trades.

    Tries SQLite first (legacy portfolio DB), falls back to PG aggregation.
    """
    summary: dict = {
        "open_positions": 0,
        "positions_by_sleeve": {},
        "recent_trades_30d": 0,
        "total_trades": 0,
        "source": "unknown",
    }

    # Try SQLite (the portfolio module's data source)
    try:
        from app.extensions.quant_sys.portfolio.data import (
            get_portfolio_summary as sqlite_summary,
        )

        summary = sqlite_summary()
        summary["source"] = "sqlite"
        return summary
    except Exception:
        logger.debug("SQLite portfolio unavailable, trying PG")

    # Fall back to PG — aggregate from daily_quote
    try:
        conn = _get_db_connection()
        import psycopg2.extras

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Count listed stocks as "positions"
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM stocks WHERE list_status = 'L'"
        )
        row = cur.fetchone()
        summary["open_positions"] = row["cnt"] if row else 0

        # Count trades in daily_quote as proxy
        cur.execute(
            """SELECT COUNT(DISTINCT ts_code) AS cnt FROM daily_quote
               WHERE trade_date >= %s""",
            ((datetime.now() - timedelta(days=30)).strftime("%Y%m%d"),),
        )
        row = cur.fetchone()
        summary["recent_trades_30d"] = row["cnt"] if row else 0

        cur.execute("SELECT COUNT(DISTINCT ts_code) AS cnt FROM daily_quote")
        row = cur.fetchone()
        summary["total_trades"] = row["cnt"] if row else 0

        summary["source"] = "pg_aggregate"
        cur.close()
    except Exception as e:
        logger.error("PG portfolio summary failed: %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return summary


# ── Risk Dashboard ────────────────────────────────────────────────────────────


def get_risk_dashboard() -> dict:
    """Get risk dashboard overview: drawdown, alerts, risk events.

    Tries to delegate to the existing risk module's data layer.
    """
    risk_data: dict = {
        "latest_snapshot": None,
        "alerts_count": 0,
        "recent_events_count": 0,
        "strategy_count": 0,
    }

    # Try the existing risk data layer first
    try:
        from app.extensions.quant_sys.risk.data import (
            get_risk_overview,
        )

        return get_risk_overview()
    except Exception:
        logger.debug("Risk data layer unavailable, using PG fallback")

    # Fallback: aggregate from PG
    conn = None
    try:
        conn = _get_db_connection()
        import psycopg2.extras

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Latest data date
        cur.execute("SELECT MAX(trade_date) AS latest FROM daily_quote")
        row = cur.fetchone()
        latest_date = str(row["latest"]) if row and row["latest"] else None

        # Aggregate market snapshot
        if latest_date:
            cur.execute(
                """SELECT COUNT(*) AS stocks,
                          AVG(pct_chg) AS avg_change,
                          MIN(pct_chg) AS min_change,
                          MAX(pct_chg) AS max_change,
                          SUM(amount) AS total_amount
                   FROM daily_quote
                   WHERE trade_date = %s""",
                (latest_date,),
            )
            row = cur.fetchone()
            if row:
                risk_data["latest_snapshot"] = {
                    "date": latest_date,
                    "stocks": row["stocks"],
                    "avg_change": float(row["avg_change"]) if row["avg_change"] else 0,
                    "min_change": float(row["min_change"]) if row["min_change"] else 0,
                    "max_change": float(row["max_change"]) if row["max_change"] else 0,
                    "total_amount": float(row["total_amount"]) if row["total_amount"] else 0,
                }

        # Count strategies (from stocks / listed count)
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM stocks WHERE list_status = 'L'"
        )
        row = cur.fetchone()
        if row:
            risk_data["strategy_count"] = row["cnt"]

        cur.close()
        risk_data.setdefault("source", "pg_aggregate")
    except Exception as e:
        logger.error("Risk dashboard PG fallback failed: %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return risk_data


# ── Backtest Summary ─────────────────────────────────────────────────────────


def get_backtest_summary(limit: int = 20) -> dict:
    """Get backtest performance summary from strategy experiments.

    Tries the strategy data layer first, falls back to PG.
    """
    summary: dict = {
        "count": 0,
        "experiments": [],
        "aggregate": {},
    }

    # Try the existing strategy data layer
    try:
        from app.extensions.quant_sys.strategy.data import (
            get_experiments,
            get_experiment_stats,
        )

        experiments = get_experiments(limit=limit)
        stats = get_experiment_stats()

        summary["count"] = len(experiments)
        summary["experiments"] = experiments
        summary["aggregate"] = stats
        summary["source"] = "sqlite_strategy"
        return summary
    except Exception:
        logger.debug("Strategy experiments unavailable, using PG fallback")

    # Fallback: aggregate some basic stats from PG
    conn = None
    try:
        conn = _get_db_connection()
        import psycopg2.extras

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Aggregate daily returns stats as a rough backtest proxy
        cur.execute(
            """SELECT trade_date, COUNT(*) AS stocks,
                      AVG(pct_chg) AS avg_return,
                      STDDEV(pct_chg) AS std_return
               FROM daily_quote
               WHERE pct_chg IS NOT NULL
               GROUP BY trade_date
               ORDER BY trade_date DESC
               LIMIT %s""",
            (limit,),
        )
        experiments = []
        for r in cur.fetchall():
            experiments.append(
                {
                    "trade_date": str(r["trade_date"]),
                    "stocks": r["stocks"],
                    "avg_return": float(r["avg_return"]) if r["avg_return"] else 0,
                    "std_return": float(r["std_return"]) if r["std_return"] else 0,
                }
            )

        # Overall aggregate
        cur.execute(
            """SELECT AVG(pct_chg) AS mean_daily,
                      STDDEV(pct_chg) AS std_daily,
                      COUNT(*) AS total_obs
               FROM daily_quote
               WHERE pct_chg IS NOT NULL"""
        )
        row = cur.fetchone()

        summary["count"] = len(experiments)
        summary["experiments"] = experiments
        summary["aggregate"] = {
            "mean_daily_return": float(row["mean_daily"]) if row and row["mean_daily"] else 0,
            "std_daily_return": float(row["std_daily"]) if row and row["std_daily"] else 0,
            "total_observations": row["total_obs"] if row else 0,
        }
        summary["source"] = "pg_aggregate"
        cur.close()
    except Exception as e:
        logger.error("Backtest summary PG fallback failed: %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    return summary