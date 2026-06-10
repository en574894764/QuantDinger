"""Strategy experiments data access — reads strategy_experiment_log from SQLite."""

import sqlite3

from app.extensions.quant_sys.data.sqlite import _get_conn, query_table


TABLE = "strategy_experiment_log"


def get_experiments(sleeve: str = "", limit: int = 50) -> list:
    """Return recent strategy experiments, optionally filtered by sleeve."""
    if sleeve:
        return query_table(
            TABLE,
            order_by="created_at DESC",
            limit=limit,
            where_clause="sleeve = ?",
            params=(sleeve,),
        )
    return query_table(TABLE, order_by="created_at DESC", limit=limit)


def get_experiment_by_id(experiment_id: str) -> dict | None:
    """Return a single experiment by its primary key ID."""
    rows = query_table(
        TABLE,
        order_by="id DESC",
        limit=1,
        where_clause="id = ?",
        params=(experiment_id,),
    )
    return rows[0] if rows else None


def get_experiment_stats() -> dict:
    """Aggregated statistics across all experiments."""
    conn = _get_conn()
    try:
        # Total experiments
        total = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]

        # Per-sleeve counts
        sleeves = {}
        try:
            rows = conn.execute(
                f"SELECT sleeve, COUNT(*) as cnt FROM {TABLE} GROUP BY sleeve"
            ).fetchall()
            for r in rows:
                sleeves[r["sleeve"]] = r["cnt"]
        except Exception:
            pass

        # Best sharpe
        best_sharpe = None
        try:
            row = conn.execute(
                f"SELECT id, sleeve, sharpe FROM {TABLE} "
                "WHERE sharpe IS NOT NULL ORDER BY sharpe DESC LIMIT 1"
            ).fetchone()
            if row:
                best_sharpe = {"id": row["id"], "sleeve": row["sleeve"],
                               "sharpe": row["sharpe"]}
        except Exception:
            pass

        return {
            "total_experiments": total,
            "by_sleeve": sleeves,
            "best_sharpe": best_sharpe,
        }
    finally:
        conn.close()
