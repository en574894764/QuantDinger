"""Extended market data routes — codelist, indices, sectors, ranking, watchlist.

Attached to quant_bp at /api/quant/market/*.
"""

import logging

from flask import jsonify, request

from app.extensions.quant_sys import quant_bp

logger = logging.getLogger(__name__)


def _get_pg_cur():
    """Get a PostgreSQL cursor (RealDictCursor) from investassist DB."""
    import os
    import psycopg2
    import psycopg2.extras

    db_url = os.environ.get(
        "QUANT_SYS_DATABASE_URL",
        "postgresql://james@host.docker.internal:5432/investassist",
    )
    conn = psycopg2.connect(db_url)
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ── Code List ──────────────────────────────────────────────────────────

@quant_bp.route("/market/codelist")
def market_codelist():
    """Return the full A-share stock code list (ts_code, name, market, industry)."""
    try:
        conn, cur = _get_pg_cur()
        cur.execute(
            "SELECT ts_code, name, market, industry, area, list_status "
            "FROM stocks WHERE list_status = 'L' ORDER BY ts_code"
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify({"count": len(rows), "data": rows})
    except Exception as e:
        logger.error("codelist failed: %s", e)
        return jsonify({"error": str(e), "data": []}), 500


# ── Market Indices ─────────────────────────────────────────────────────

@quant_bp.route("/market/indices")
def market_indices():
    """Return major A-share index data: SSE, SZSE, CSI300, etc."""
    try:
        from app.extensions.quant_sys.data.store.parquet import ParquetStore
        import glob, os, pandas as pd

        store = ParquetStore()
        base = os.path.join(store.data_dir, "raw", "index", "daily")
        files = sorted(glob.glob(os.path.join(base, "**", "*.parquet"), recursive=True))

        if not files:
            return jsonify({"data": []})

        # Read all index parquet files
        dfs = []
        for f in files:
            df = pd.read_parquet(f)
            dfs.append(df)

        df = pd.concat(dfs, ignore_index=True)
        df["trade_date"] = pd.to_datetime(df["trade_date"])

        index_map = {
            "000001.SH": "上证指数",
            "399001.SZ": "深证成指",
            "000300.SH": "沪深300",
            "000905.SH": "中证500",
            "000852.SH": "中证1000",
            "399006.SZ": "创业板指",
            "000688.SH": "科创50",
            "000016.SH": "上证50",
        }

        latest = df["trade_date"].max()

        result = []
        for code, name in index_map.items():
            sub = df[df["symbol"] == code]
            if sub.empty:
                continue
            latest_row = sub[sub["trade_date"] == sub["trade_date"].max()]
            if latest_row.empty:
                continue
            row = latest_row.iloc[-1]

            result.append({
                "ts_code": code,
                "name": name,
                "close": float(row.get("close", 0)),
                "pct_chg": float(row.get("pct_chg", 0)),
                "vol": float(row.get("volume", 0)) if "volume" in row.index else 0,
                "amount": float(row.get("amount", 0)) if "amount" in row.index else 0,
                "trade_date": str(latest.date()),
            })

        return jsonify({"data": result, "trade_date": str(latest.date())})
    except Exception as e:
        logger.error("indices failed: %s", e)
        return jsonify({"error": str(e), "data": []}), 500


# ── Market Sectors ─────────────────────────────────────────────────────

@quant_bp.route("/market/sectors")
def market_sectors():
    """Return sector/industry performance on latest trade date."""
    try:
        import os
        import psycopg2
        import psycopg2.extras

        db_url = os.environ.get(
            "QUANT_SYS_DATABASE_URL",
            "postgresql://james@host.docker.internal:5432/investassist",
        )
        conn = psycopg2.connect(db_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT MAX(trade_date) AS latest FROM daily_quote")
        row = cur.fetchone()
        if not row or not row["latest"]:
            cur.close(); conn.close()
            return jsonify({"data": [], "trade_date": None})

        latest_date = str(row["latest"])

        cur.execute(
            """SELECT s.industry, COUNT(*) AS stock_count,
                      AVG(d.pct_chg) AS avg_pct_chg,
                      SUM(d.amount) AS total_amount,
                      SUM(CASE WHEN d.pct_chg > 0 THEN 1 ELSE 0 END) AS advancing,
                      SUM(CASE WHEN d.pct_chg < 0 THEN 1 ELSE 0 END) AS declining
               FROM stocks s
               JOIN daily_quote d ON s.ts_code = d.ts_code
               WHERE d.trade_date = %s
                 AND s.industry IS NOT NULL AND s.industry != ''
                 AND d.pct_chg IS NOT NULL
               GROUP BY s.industry
               ORDER BY AVG(d.pct_chg) DESC""",
            (latest_date,),
        )
        sectors = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()

        for s in sectors:
            for k in ("avg_pct_chg", "total_amount"):
                if s.get(k) is not None:
                    s[k] = round(float(s[k]), 2)

        return jsonify({
            "data": sectors,
            "count": len(sectors),
            "trade_date": latest_date[:10] if "-" in latest_date else f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:8]}",
        })
    except Exception as e:
        logger.error("sectors failed: %s", e)
        return jsonify({"error": str(e), "data": []}), 500


# ── Market Ranking (top gainers/losers/volume) ─────────────────────────

@quant_bp.route("/market/overview/ranking")
def market_ranking():
    """Return top gainers, losers, and volume leaders on latest trade date."""
    try:
        import os
        import psycopg2
        import psycopg2.extras

        db_url = os.environ.get(
            "QUANT_SYS_DATABASE_URL",
            "postgresql://james@host.docker.internal:5432/investassist",
        )
        conn = psycopg2.connect(db_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT MAX(trade_date) AS latest FROM daily_quote")
        row = cur.fetchone()
        if not row or not row["latest"]:
            cur.close(); conn.close()
            return jsonify({})

        latest_date = str(row["latest"])

        def _fetch(sql, limit=10):
            cur.execute(sql, (latest_date,))
            return [dict(r) for r in cur.fetchall()]

        result = {
            "top_gainers": _fetch(
                "SELECT ts_code, close, pct_chg, vol, amount FROM daily_quote "
                "WHERE trade_date = %s AND pct_chg IS NOT NULL "
                "ORDER BY pct_chg DESC LIMIT 10"
            ),
            "top_losers": _fetch(
                "SELECT ts_code, close, pct_chg, vol, amount FROM daily_quote "
                "WHERE trade_date = %s AND pct_chg IS NOT NULL "
                "ORDER BY pct_chg ASC LIMIT 10"
            ),
            "volume_leaders": _fetch(
                "SELECT ts_code, close, pct_chg, vol, amount FROM daily_quote "
                "WHERE trade_date = %s AND amount IS NOT NULL "
                "ORDER BY amount DESC LIMIT 10"
            ),
            "trade_date": latest_date[:10] if "-" in latest_date else f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:8]}",
        }

        cur.close(); conn.close()
        return jsonify(result)
    except Exception as e:
        logger.error("ranking failed: %s", e)
        return jsonify({"error": str(e)}), 500


@quant_bp.route("/market/stocks")
def market_stocks():
    """Paginated stock list with optional market/industry filter. (QuantDinger frontend compat)"""
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("pageSize", 50, type=int)
    market = request.args.get("market", "")
    industry = request.args.get("industry", "")
    page_size = min(page_size, 200)
    offset = (page - 1) * page_size

    try:
        conn, cur = _get_pg_cur()

        where = ["list_status = 'L'"]
        params = []
        if market:
            where.append("market = %s")
            params.append(market)
        if industry:
            where.append("industry = %s")
            params.append(industry)

        where_clause = " AND ".join(where)

        cur.execute(
            f"SELECT ts_code AS symbol, name, market, industry, area FROM stocks WHERE {where_clause} ORDER BY ts_code LIMIT %s OFFSET %s",
            params + [page_size, offset],
        )
        items = [dict(r) for r in cur.fetchall()]

        cur.execute(f"SELECT COUNT(*) AS cnt FROM stocks WHERE {where_clause}", params)
        total = cur.fetchone()["cnt"]

        cur.close(); conn.close()
        return jsonify({"items": items, "total": total, "page": page, "pageSize": page_size})
    except Exception as e:
        logger.error("stocks list failed: %s", e)
        return jsonify({"items": [], "total": 0}), 500


def init_app(app):
    """No-op — routes attached to quant_bp."""
    pass
