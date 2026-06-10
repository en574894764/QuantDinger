"""Portfolio data access — reads positions and trades from SQLite."""

from app.extensions.quant_sys.data.sqlite import _get_conn, query_table


def get_positions(sleeve: str = "", status: str = "") -> list:
    """Return current positions, optionally filtered by sleeve/status."""
    clauses = []
    params = []

    if sleeve:
        clauses.append("sleeve = ?")
        params.append(sleeve)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = " AND ".join(clauses) if clauses else ""
    return query_table(
        "positions",
        order_by="sleeve, ts_code",
        limit=500,
        where_clause=where,
        params=tuple(params),
    )


def get_trades(sleeve: str = "", ts_code: str = "", limit: int = 200) -> list:
    """Return trade history, optionally filtered."""
    clauses = []
    params = []

    if sleeve:
        clauses.append("sleeve = ?")
        params.append(sleeve)
    if ts_code:
        clauses.append("ts_code = ?")
        params.append(ts_code)

    where = " AND ".join(clauses) if clauses else ""
    return query_table(
        "trades",
        order_by="trade_date DESC",
        limit=limit,
        where_clause=where,
        params=tuple(params),
    )


def get_portfolio_summary() -> dict:
    """Aggregated portfolio summary from positions + trades."""
    conn = _get_conn()
    try:
        # Count open positions
        pos_count = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'open'"
        ).fetchone()[0]

        # Positions by sleeve
        by_sleeve = {}
        rows = conn.execute(
            "SELECT sleeve, COUNT(*) as cnt FROM positions "
            "WHERE status = 'open' GROUP BY sleeve"
        ).fetchall()
        for r in rows:
            by_sleeve[r["sleeve"]] = r["cnt"]

        # Recent trades count
        trade_count = conn.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE trade_date >= date('now', '-30 days')"
        ).fetchone()[0]

        # Total trade count
        total_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

        # Buy vs sell breakdown (last 30 days)
        buys = conn.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE direction = 'buy' AND trade_date >= date('now', '-30 days')"
        ).fetchone()[0]
        sells = conn.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE direction = 'sell' AND trade_date >= date('now', '-30 days')"
        ).fetchone()[0]

        return {
            "open_positions": pos_count,
            "positions_by_sleeve": by_sleeve,
            "recent_trades_30d": trade_count,
            "total_trades": total_trades,
            "buys_30d": buys,
            "sells_30d": sells,
        }
    finally:
        conn.close()
