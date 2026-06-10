"""Idea Pool — CRUD + state machine for investment ideas.

States:
    submitted → researching → backtesting → validated
                                         → rejected

Store: SQLite /quant_sys_data/system.db table: ideas

Port from quant_sys/src/ideas/pool.py (simplified).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SQLITE_PATH = os.environ.get(
    "QUANT_SYS_SQLITE_PATH",
    "/quant_sys_data/system.db",
)

# Valid state transitions
VALID_STATES = ("submitted", "researching", "backtesting", "validated", "rejected")

STATE_TRANSITIONS: dict[str, set[str]] = {
    "submitted":    {"researching", "rejected"},
    "researching":  {"backtesting", "rejected"},
    "backtesting":  {"validated", "rejected"},
    "validated":    set(),
    "rejected":     set(),
}


def _get_conn(readonly: bool = False) -> sqlite3.Connection:
    """Return a SQLite connection to system.db, ensuring the ideas table exists."""
    uri = f"file:{SQLITE_PATH}?mode=ro" if readonly else SQLITE_PATH
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_table(conn)
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the ideas table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ideas (
            id          TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            logic       TEXT DEFAULT '',
            hypothesis  TEXT DEFAULT '',
            market      TEXT DEFAULT 'a_shares',
            priority    TEXT DEFAULT 'medium',
            status      TEXT DEFAULT 'submitted',
            tags        TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _validate_state(status: str) -> bool:
    """Return True if status is a valid state."""
    return status in VALID_STATES


def _validate_transition(current: str, target: str) -> bool:
    """Check if a state transition is allowed."""
    if not _validate_state(current) or not _validate_state(target):
        return False
    return target in STATE_TRANSITIONS.get(current, set())


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def submit_idea(
    description: str,
    logic: str = "",
    hypothesis: str = "",
    market: str = "a_shares",
    priority: str = "medium",
    tags: str = "",
) -> dict:
    """Submit a new investment idea to the pool.

    Args:
        description: One-line description of the idea.
        logic: Investment logic (why you think it works).
        hypothesis: Testable proposition.
        market: ``a_shares``, ``us_stocks``, or ``crypto``.
        priority: ``high``, ``medium``, or ``low``.
        tags: Comma-separated tags.

    Returns:
        dict: The created idea record.
    """
    if priority not in ("high", "medium", "low"):
        priority = "medium"

    idea_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO ideas
                   (id, description, logic, hypothesis, market, priority, status, tags, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'submitted', ?, ?, ?)""",
            (idea_id, description, logic, hypothesis, market, priority, tags, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        logger.info("Idea submitted: %s — %s", idea_id, description[:60])
        return _row_to_dict(row) or {}
    except Exception as e:
        logger.error("Failed to submit idea: %s", e, exc_info=True)
        raise
    finally:
        conn.close()


def list_ideas(limit: int = 30, status: str = "") -> list[dict]:
    """List recent ideas, optionally filtered by status.

    Args:
        limit: Maximum number of results (default 30).
        status: Filter by status (e.g. ``submitted``, ``validated``).

    Returns:
        list[dict]: List of idea records ordered by created_at descending.
    """
    conn = _get_conn(readonly=True)
    try:
        if status:
            cur = conn.execute(
                "SELECT * FROM ideas WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM ideas ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [_row_to_dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error("Failed to list ideas: %s", e, exc_info=True)
        return []
    finally:
        conn.close()


def get_idea(idea_id: str) -> dict | None:
    """Get a single idea by ID.

    Returns:
        dict | None: The idea record, or None if not found.
    """
    conn = _get_conn(readonly=True)
    try:
        row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        return _row_to_dict(row)
    except Exception as e:
        logger.error("Failed to get idea %s: %s", idea_id, e, exc_info=True)
        return None
    finally:
        conn.close()


def update_idea_status(idea_id: str, new_status: str) -> dict:
    """Update the status of an idea, enforcing state-machine transitions.

    Args:
        idea_id: The idea ID.
        new_status: Target status (must be a valid transition).

    Returns:
        dict: ``{"success": True, "data": {...}}`` or ``{"success": False, "error": "..."}``
    """
    if not _validate_state(new_status):
        return {"success": False, "error": f"Invalid status: {new_status}. Valid: {VALID_STATES}"}

    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        if row is None:
            return {"success": False, "error": f"Idea not found: {idea_id}"}

        current = row["status"]
        if current == new_status:
            return {"success": True, "data": _row_to_dict(row), "message": "No change"}

        if not _validate_transition(current, new_status):
            allowed = STATE_TRANSITIONS.get(current, set())
            return {
                "success": False,
                "error": (
                    f"Invalid transition: '{current}' → '{new_status}'. "
                    f"Allowed: {allowed or 'none (terminal state)'}"
                ),
            }

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, idea_id),
        )
        conn.commit()

        updated = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        logger.info("Idea %s: %s → %s", idea_id, current, new_status)
        return {"success": True, "data": _row_to_dict(updated)}
    except Exception as e:
        logger.error("Failed to update idea %s: %s", idea_id, e, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def get_idea_stats() -> dict:
    """Aggregated statistics across all ideas."""
    conn = _get_conn(readonly=True)
    try:
        total = conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0]

        by_status = {}
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM ideas GROUP BY status"
        ).fetchall()
        for r in rows:
            by_status[r["status"]] = r["cnt"]

        by_market = {}
        rows = conn.execute(
            "SELECT market, COUNT(*) as cnt FROM ideas GROUP BY market"
        ).fetchall()
        for r in rows:
            by_market[r["market"]] = r["cnt"]

        by_priority = {}
        rows = conn.execute(
            "SELECT priority, COUNT(*) as cnt FROM ideas GROUP BY priority"
        ).fetchall()
        for r in rows:
            by_priority[r["priority"]] = r["cnt"]

        return {
            "total": total,
            "by_status": by_status,
            "by_market": by_market,
            "by_priority": by_priority,
        }
    except Exception as e:
        logger.error("Failed to get idea stats: %s", e, exc_info=True)
        return {"total": 0, "by_status": {}, "by_market": {}, "by_priority": {}}
    finally:
        conn.close()
