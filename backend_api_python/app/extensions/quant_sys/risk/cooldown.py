"""Strategy and account cooldown after drawdown events.

After a drawdown breach, the affected strategy sleeve (or the whole account)
enters a cooldown period where new signals are automatically blocked.
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_SQLITE_PATH = os.environ.get(
    "QUANT_SYS_SQLITE_PATH", "/quant_sys_data/system.db"
)

# Default cooldown durations (hours)
DEFAULT_SLEEVE_COOLDOWN_H = int(os.environ.get("RISK_COOLDOWN_SLEEVE_H", 24))
DEFAULT_ACCOUNT_COOLDOWN_H = int(os.environ.get("RISK_COOLDOWN_ACCOUNT_H", 48))
DEFAULT_SOFT_COOLDOWN_H = int(os.environ.get("RISK_COOLDOWN_SOFT_H", 4))


def _get_conn(readonly: bool = False) -> sqlite3.Connection:
    uri = f"file:{_SQLITE_PATH}?mode=ro" if readonly else _SQLITE_PATH
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_cooldown_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cooldown_state (
            scope       TEXT NOT NULL,   -- 'sleeve:A', 'sleeve:B', 'sleeve:C', 'account'
            cooldown_until TEXT NOT NULL,
            trigger_event TEXT NOT NULL, -- e.g. 'drawdown_breach', 'stop_loss'
            severity    TEXT NOT NULL DEFAULT 'hard',  -- 'hard' or 'soft'
            created_at  TEXT NOT NULL,
            PRIMARY KEY (scope)
        )
    """)


def set_cooldown(scope: str, hours: int | None = None, severity: str = "hard",
                 trigger_event: str = "") -> dict:
    """Place *scope* into cooldown for *hours*."""
    if hours is None:
        hours = DEFAULT_SLEEVE_COOLDOWN_H if scope.startswith("sleeve") else DEFAULT_ACCOUNT_COOLDOWN_H

    until = (datetime.utcnow() + timedelta(hours=hours)).isoformat() + "Z"
    now = datetime.utcnow().isoformat() + "Z"

    conn = _get_conn(readonly=False)
    try:
        _ensure_cooldown_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO cooldown_state "
            "(scope, cooldown_until, trigger_event, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope, until, trigger_event, severity, now),
        )
        conn.commit()
        logger.info("Cooldown set: scope=%s until=%s severity=%s trigger=%s",
                     scope, until, severity, trigger_event)
        return {
            "success": True,
            "scope": scope,
            "cooldown_until": until,
            "severity": severity,
            "trigger_event": trigger_event,
        }
    except Exception:
        conn.rollback()
        logger.exception("Failed to set cooldown for %s", scope)
        raise
    finally:
        conn.close()


def clear_cooldown(scope: str) -> dict:
    """Remove cooldown for *scope*."""
    conn = _get_conn(readonly=False)
    try:
        _ensure_cooldown_table(conn)
        conn.execute("DELETE FROM cooldown_state WHERE scope = ?", (scope,))
        conn.commit()
        logger.info("Cooldown cleared: scope=%s", scope)
        return {"success": True, "scope": scope, "cleared": True}
    except Exception:
        conn.rollback()
        logger.exception("Failed to clear cooldown for %s", scope)
        raise
    finally:
        conn.close()


def check_cooldown(scope: str | None = None) -> dict:
    """Check whether *scope* (or any scope, if omitted) is in cooldown.

    Returns
    -------
    dict
        ``{"in_cooldown": bool, "cooldowns": [...], "blocked_scopes": [...]}``
    """
    conn = _get_conn(readonly=True)
    try:
        _ensure_cooldown_table(conn)

        now = datetime.utcnow().isoformat() + "Z"
        if scope:
            rows = conn.execute(
                "SELECT * FROM cooldown_state WHERE scope = ? AND cooldown_until > ?",
                (scope, now),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cooldown_state WHERE cooldown_until > ?",
                (now,),
            ).fetchall()

        cooldowns = [dict(r) for r in rows]
        blocked_scopes = [r["scope"] for r in rows]

        # Clean up expired cooldowns
        if not scope:
            expired = conn.execute(
                "SELECT scope FROM cooldown_state WHERE cooldown_until <= ?",
                (now,),
            ).fetchall()
            if expired:
                # Use a separate write connection to clean up
                wconn = _get_conn(readonly=False)
                try:
                    for row in expired:
                        wconn.execute("DELETE FROM cooldown_state WHERE scope = ?",
                                      (row["scope"],))
                    wconn.commit()
                finally:
                    wconn.close()

        return {
            "in_cooldown": len(blocked_scopes) > 0,
            "cooldowns": cooldowns,
            "blocked_scopes": blocked_scopes,
        }
    finally:
        conn.close()


def is_strategy_in_cooldown(sleeve: str) -> bool:
    """Quick check: return True if *sleeve* is in cooldown."""
    scope = f"sleeve:{sleeve}" if not sleeve.startswith("sleeve:") else sleeve
    result = check_cooldown(scope)
    return result["in_cooldown"]


def trigger_drawdown_cooldown(sleeve: str = "", drawdown_pct: float = 0.0) -> dict:
    """Convenience: set cooldown based on drawdown severity."""
    if drawdown_pct >= 0.15:
        # Hard drawdown → account-wide cooldown
        return set_cooldown("account", hours=DEFAULT_ACCOUNT_COOLDOWN_H,
                            severity="hard", trigger_event=f"drawdown_{drawdown_pct:.1%}")
    elif drawdown_pct >= 0.10 and sleeve:
        # Moderate drawdown → sleeve cooldown
        scope = f"sleeve:{sleeve}" if not sleeve.startswith("sleeve:") else sleeve
        return set_cooldown(scope, hours=DEFAULT_SLEEVE_COOLDOWN_H,
                            severity="hard", trigger_event=f"drawdown_{drawdown_pct:.1%}")
    elif drawdown_pct >= 0.05:
        # Soft drawdown → short sleeve cooldown
        scope = f"sleeve:{sleeve}" if sleeve and not sleeve.startswith("sleeve:") else (f"sleeve:{sleeve}" if sleeve else "account")
        return set_cooldown(scope, hours=DEFAULT_SOFT_COOLDOWN_H,
                            severity="soft", trigger_event=f"drawdown_{drawdown_pct:.1%}")
    return {"success": True, "in_cooldown": False, "reason": "Below cooldown threshold"}
