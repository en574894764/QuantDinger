"""Shared SQLite access for quant_sys risk/strategy/portfolio blueprints."""

import os
import sqlite3

SQLITE_PATH = os.environ.get(
    "QUANT_SYS_SQLITE_PATH",
    "/quant_sys_data/system.db",
)


def _get_conn(readonly: bool = True) -> sqlite3.Connection:
    """Return a SQLite connection to system.db.

    Uses URI mode so we can open in read-only mode safely when multiple
    processes (or the quant_sys pipeline itself) may write concurrently.
    """
    uri = f"file:{SQLITE_PATH}?mode=ro" if readonly else SQLITE_PATH
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def query_table(table: str, order_by: str = "id DESC", limit: int = 200,
                where_clause: str = "", params: tuple = ()):
    """Generic table reader returning list of dicts."""
    conn = _get_conn()
    try:
        sql = f"SELECT * FROM {table}"
        if where_clause:
            sql += f" WHERE {where_clause}"
        sql += f" ORDER BY {order_by}"
        if limit > 0:
            sql += f" LIMIT {limit}"
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        return rows
    finally:
        conn.close()