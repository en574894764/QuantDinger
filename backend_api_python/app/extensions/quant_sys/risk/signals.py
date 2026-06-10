"""Signal state machine with CAS atomic operations on SQLite.

States:
    pending → confirmed → pre_check_passed → executed
                       → blocked_hard (terminal)
                       → blocked_soft → force_executed
           → rejected
           → expired

Atomicity via BEGIN IMMEDIATE / WAL.
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, date

logger = logging.getLogger(__name__)

_SQLITE_PATH = os.environ.get(
    "QUANT_SYS_SQLITE_PATH", "/quant_sys_data/system.db"
)

# Valid states and their legal transitions
_STATE_TRANSITIONS = {
    "pending": {"confirmed", "rejected", "expired"},
    "confirmed": {"pre_check_passed", "blocked_hard", "blocked_soft"},
    "pre_check_passed": {"executed", "blocked_hard", "blocked_soft"},
    "blocked_soft": {"force_executed", "blocked_hard", "expired"},
    "blocked_hard": set(),          # terminal
    "executed": set(),              # terminal
    "rejected": set(),              # terminal
    "expired": set(),               # terminal
    "force_executed": set(),        # terminal
}


def _get_conn(readonly: bool = False) -> sqlite3.Connection:
    """Return a writable SQLite connection by default (signals need writes)."""
    uri = f"file:{_SQLITE_PATH}?mode=ro" if readonly else _SQLITE_PATH
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_signals_table(conn: sqlite3.Connection):
    """Create the signals table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id              TEXT PRIMARY KEY,
            symbol          TEXT    NOT NULL,
            sleeve          TEXT    NOT NULL DEFAULT 'default',
            direction       TEXT    NOT NULL DEFAULT 'buy',
            order_size_pct  REAL    NOT NULL DEFAULT 0.0,
            strategy_name   TEXT    NOT NULL DEFAULT '',
            trigger_factor  TEXT    DEFAULT '',
            state           TEXT    NOT NULL DEFAULT 'pending',
            reason          TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL,
            confirmed_at    TEXT,
            executed_at     TEXT,
            trade_date      TEXT
        )
    """)
    # Indices for common queries
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_state
        ON signals(state)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signals_trade_date
        ON signals(trade_date)
    """)


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def _transition(conn: sqlite3.Connection, signal_id: str,
                to_state: str, reason: str = "", extra_updates: dict | None = None) -> dict:
    """Atomically transition a signal to *to_state*.

    Uses BEGIN IMMEDIATE for write serialisation; verifies the transition is
    legal from the current state.
    """
    current_state = "?"
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT state FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()

        if not row:
            conn.rollback()
            return {"success": False, "error": f"Signal {signal_id} not found"}

        current_state = row["state"]
        allowed = _STATE_TRANSITIONS.get(current_state, set())

        if to_state not in allowed:
            conn.rollback()
            return {
                "success": False,
                "error": (
                    f"Illegal transition: {current_state} → {to_state}. "
                    f"Allowed: {sorted(allowed)}"
                ),
                "current_state": current_state,
            }

        now = datetime.utcnow().isoformat() + "Z"
        fields = ["state = ?", "updated_at = ?"]
        params = [to_state, now]

        if reason:
            fields.append("reason = ?")
            params.append(reason)

        if to_state == "confirmed":
            fields.append("confirmed_at = ?")
            params.append(now)
        elif to_state in ("executed", "force_executed"):
            fields.append("executed_at = ?")
            params.append(now)

        if extra_updates:
            for col, val in extra_updates.items():
                fields.append(f"{col} = ?")
                params.append(val)

        sql = f"UPDATE signals SET {', '.join(fields)} WHERE id = ?"
        params.append(signal_id)
        conn.execute(sql, params)
        conn.commit()

        logger.info("Signal %s: %s → %s%s",
                     signal_id, current_state, to_state,
                     f" ({reason})" if reason else "")

        return {
            "success": True,
            "signal_id": signal_id,
            "previous_state": current_state,
            "new_state": to_state,
        }
    except Exception:
        conn.rollback()
        logger.exception("Transition %s → %s failed for signal %s",
                         current_state, to_state, signal_id)
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def confirm_signal(signal_id: str) -> dict:
    """Confirm a pending signal → 'confirmed'."""
    conn = _get_conn(readonly=False)
    try:
        _ensure_signals_table(conn)
        return _transition(conn, signal_id, "confirmed")
    finally:
        conn.close()


def pass_pre_check(signal_id: str) -> dict:
    """Mark signal as having passed risk pre‑check → 'pre_check_passed'."""
    conn = _get_conn(readonly=False)
    try:
        _ensure_signals_table(conn)
        return _transition(conn, signal_id, "pre_check_passed")
    finally:
        conn.close()


def block_signal(signal_id: str, hard: bool = False, reason: str = "") -> dict:
    """Block a signal (soft or hard)."""
    target = "blocked_hard" if hard else "blocked_soft"
    conn = _get_conn(readonly=False)
    try:
        _ensure_signals_table(conn)
        return _transition(conn, signal_id, target, reason=reason)
    finally:
        conn.close()


def reject_signal(signal_id: str, reason: str = "") -> dict:
    """Reject a pending signal → 'rejected'."""
    conn = _get_conn(readonly=False)
    try:
        _ensure_signals_table(conn)
        return _transition(conn, signal_id, "rejected", reason=reason)
    finally:
        conn.close()


def mark_executed(signal_id: str) -> dict:
    """Mark a pre‑check‑passed signal as executed."""
    conn = _get_conn(readonly=False)
    try:
        _ensure_signals_table(conn)
        return _transition(conn, signal_id, "executed")
    finally:
        conn.close()


def force_execute(signal_id: str, reason: str = "") -> dict:
    """Force‑execute a soft‑blocked signal."""
    conn = _get_conn(readonly=False)
    try:
        _ensure_signals_table(conn)
        return _transition(conn, signal_id, "force_executed", reason=reason)
    finally:
        conn.close()


def expire_stale_signals(trade_date: str, days: int = 5) -> dict:
    """Expire pending/soft‑blocked signals older than *days* relative to
    *trade_date* (YYYYMMDD)."""
    cutoff = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")

    conn = _get_conn(readonly=False)
    try:
        _ensure_signals_table(conn)

        rows = conn.execute(
            "SELECT id, state FROM signals "
            "WHERE state IN ('pending', 'blocked_soft') "
            "AND trade_date <= ?",
            (cutoff,),
        ).fetchall()

        expired_count = 0
        for row in rows:
            try:
                result = _transition(
                    conn, row["id"], "expired",
                    reason=f"Stale after {days} days (cutoff {cutoff})"
                )
                if result["success"]:
                    expired_count += 1
            except Exception:
                logger.exception("Failed to expire signal %s", row["id"])

        logger.info("Expired %d stale signals (cutoff %s, days %d)",
                     expired_count, cutoff, days)
        return {"success": True, "expired_count": expired_count, "cutoff": cutoff}
    finally:
        conn.close()


def get_signal_detail(signal_id: str) -> dict:
    """Return full details for one signal."""
    conn = _get_conn(readonly=True)
    try:
        # Skip _ensure_signals_table — read-only connection cannot execute DDL.
        row = conn.execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        if not row:
            return {"success": False, "error": f"Signal {signal_id} not found"}
        return {"success": True, "data": dict(row)}
    finally:
        conn.close()


def get_pending_signals(sleeve: str = "", limit: int = 200) -> dict:
    """List signals in pending / confirmed / pre_check_passed / blocked_soft states."""
    states = ("pending", "confirmed", "pre_check_passed", "blocked_soft")
    conn = _get_conn(readonly=True)
    try:
        # Skip _ensure_signals_table — read-only connection cannot execute DDL.
        if sleeve:
            rows = conn.execute(
                "SELECT * FROM signals "
                "WHERE state IN (?,?,?,?) AND sleeve = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (*states, sleeve, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM signals "
                "WHERE state IN (?,?,?,?) "
                "ORDER BY created_at DESC LIMIT ?",
                (*states, limit),
            ).fetchall()
        return {"success": True, "data": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


def get_signal_stats(start_date: str = "", end_date: str = "") -> dict:
    """Aggregate signal statistics: total, confirm rate, execute rate."""
    conn = _get_conn(readonly=True)
    try:
        # Skip _ensure_signals_table — read-only connection cannot execute DDL.
        where = ""
        params = []
        if start_date:
            where += " AND created_at >= ?"
            params.append(start_date)
        if end_date:
            where += " AND created_at <= ?"
            params.append(end_date)

        total = conn.execute(
            f"SELECT COUNT(*) as cnt FROM signals WHERE 1=1 {where}", params
        ).fetchone()["cnt"]

        confirmed = conn.execute(
            f"SELECT COUNT(*) as cnt FROM signals "
            f"WHERE state IN ('confirmed','pre_check_passed','executed','force_executed') {where}",
            params,
        ).fetchone()["cnt"]

        executed = conn.execute(
            f"SELECT COUNT(*) as cnt FROM signals "
            f"WHERE state IN ('executed','force_executed') {where}",
            params,
        ).fetchone()["cnt"]

        blocked = conn.execute(
            f"SELECT COUNT(*) as cnt FROM signals "
            f"WHERE state IN ('blocked_hard','blocked_soft') {where}",
            params,
        ).fetchone()["cnt"]

        return {
            "success": True,
            "total": total,
            "confirmed": confirmed,
            "confirm_rate": round(confirmed / total, 4) if total else 0,
            "executed": executed,
            "execute_rate": round(executed / total, 4) if total else 0,
            "blocked": blocked,
        }
    finally:
        conn.close()


def insert_signal(signal: dict) -> dict:
    """Insert a new pending signal into the system.

    *signal* must contain: id, symbol, direction, order_size_pct.
    Optional: sleeve, strategy_name, trigger_factor, trade_date.
    """
    conn = _get_conn(readonly=False)
    try:
        _ensure_signals_table(conn)

        now = datetime.utcnow().isoformat() + "Z"
        conn.execute(
            "INSERT INTO signals (id, symbol, sleeve, direction, order_size_pct, "
            "strategy_name, trigger_factor, state, created_at, updated_at, trade_date) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                signal["id"],
                signal.get("symbol", "").upper(),
                signal.get("sleeve", "default"),
                signal.get("direction", signal.get("side", "buy")),
                float(signal.get("order_size_pct", 0)),
                signal.get("strategy_name", ""),
                signal.get("trigger_factor", ""),
                "pending",
                now,
                now,
                signal.get("trade_date", ""),
            ),
        )
        conn.commit()
        logger.info("Inserted signal %s (%s %s)", signal["id"],
                     signal.get("symbol"), signal.get("direction"))
        return {"success": True, "signal_id": signal["id"]}
    except sqlite3.IntegrityError:
        conn.rollback()
        return {"success": False, "error": f"Signal {signal.get('id')} already exists"}
    except Exception:
        conn.rollback()
        logger.exception("Failed to insert signal %s", signal.get("id"))
        raise
    finally:
        conn.close()
