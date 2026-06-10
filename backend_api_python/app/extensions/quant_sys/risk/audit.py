"""Audit logging — writes risk events into SQLite risk_events table.

Every check, block, warning, or state transition is recorded for post‑mortem
analysis.
"""

import logging
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

_SQLITE_PATH = os.environ.get(
    "QUANT_SYS_SQLITE_PATH", "/quant_sys_data/system.db"
)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_events_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT    NOT NULL,
            severity    TEXT    NOT NULL DEFAULT 'info',
            scope       TEXT    DEFAULT '',
            signal_id   TEXT    DEFAULT '',
            symbol      TEXT    DEFAULT '',
            summary     TEXT    NOT NULL DEFAULT '',
            detail_json TEXT    DEFAULT '{}',
            created_at  TEXT    NOT NULL
        )
    """)
    # Migration: add scope column if table was created before it existed
    _migrate_add_column(conn, "risk_events", "scope", "TEXT DEFAULT ''")
    _migrate_add_column(conn, "risk_events", "signal_id", "TEXT DEFAULT ''")
    _migrate_add_column(conn, "risk_events", "detail_json", "TEXT DEFAULT '{}'")


def _migrate_add_column(conn: sqlite3.Connection, table: str, column: str, col_def: str):
    """Add a column if it doesn't already exist (no-op if present)."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
    except sqlite3.OperationalError:
        pass  # column already exists


def log_event(
    event_type: str,
    summary: str,
    severity: str = "info",
    scope: str = "",
    signal_id: str = "",
    symbol: str = "",
    detail: dict | None = None,
) -> int | None:
    """Write a risk event to the audit log.

    Returns the inserted row id.
    """
    import json

    conn = _get_conn()
    try:
        _ensure_events_table(conn)

        now = datetime.utcnow().isoformat() + "Z"
        detail_str = json.dumps(detail or {}, default=str)

        cur = conn.execute(
            "INSERT INTO risk_events "
            "(event_type, severity, scope, signal_id, symbol, summary, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_type, severity, scope, signal_id, symbol, summary, detail_str, now),
        )
        conn.commit()
        event_id = cur.lastrowid
        logger.debug("Audit event #%d: [%s] %s — %s", event_id, event_type, severity, summary)
        return event_id
    except Exception:
        conn.rollback()
        logger.exception("Failed to log audit event: %s", summary)
        raise
    finally:
        conn.close()
