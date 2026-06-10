"""Strategy lifecycle state machine.

Manages strategy states and transitions:
  draft → paper_trading → live → paused → stopped
  (archived is terminal from any state)

Uses SQLite ``strategies`` table for persistence.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.extensions.quant_sys.data.sqlite import _get_conn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class StrategyStatus(str, Enum):
    DRAFT = "draft"
    PAPER_TRADING = "paper_trading"
    LIVE = "live"
    PAUSED = "paused"
    STOPPED = "stopped"
    ARCHIVED = "archived"


# Valid transitions
TRANSITIONS: dict[StrategyStatus, set[StrategyStatus]] = {
    StrategyStatus.DRAFT: {StrategyStatus.PAPER_TRADING, StrategyStatus.ARCHIVED},
    StrategyStatus.PAPER_TRADING: {StrategyStatus.LIVE, StrategyStatus.STOPPED, StrategyStatus.ARCHIVED},
    StrategyStatus.LIVE: {StrategyStatus.PAUSED, StrategyStatus.STOPPED, StrategyStatus.ARCHIVED},
    StrategyStatus.PAUSED: {StrategyStatus.LIVE, StrategyStatus.STOPPED, StrategyStatus.ARCHIVED},
    StrategyStatus.STOPPED: {StrategyStatus.ARCHIVED},
    StrategyStatus.ARCHIVED: set(),  # terminal
}

TABLE = "strategies"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class StrategyDef:
    """Definition of a strategy (immutable core fields)."""

    name: str
    sleeve: str = "A"
    description: str = ""
    factor_weights: dict[str, float] = field(default_factory=dict)
    universe: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyRecord:
    """Full strategy record persisted to SQLite."""

    id: str
    name: str
    sleeve: str
    status: StrategyStatus
    description: str
    factor_weights: dict[str, float]
    universe: list[str]
    config: dict[str, Any]
    created_at: str
    updated_at: str
    paper_started_at: Optional[str] = None
    live_started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    archived_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Ensure table exists
# ---------------------------------------------------------------------------


def _ensure_table():
    """Create the strategies table if it doesn't exist."""
    conn = _get_conn(readonly=False)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                sleeve TEXT DEFAULT 'A',
                status TEXT DEFAULT 'draft',
                description TEXT DEFAULT '',
                factor_weights TEXT DEFAULT '{}',
                universe TEXT DEFAULT '[]',
                config TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                paper_started_at TEXT,
                live_started_at TEXT,
                stopped_at TEXT,
                archived_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategies_sleeve ON strategies(sleeve)"
        )
        conn.commit()
    finally:
        conn.close()


_ensure_table()


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def _row_to_record(row: dict) -> StrategyRecord:
    """Convert a DB row dict to a StrategyRecord."""
    import json

    return StrategyRecord(
        id=row["id"],
        name=row["name"],
        sleeve=row.get("sleeve", "A"),
        status=StrategyStatus(row.get("status", "draft")),
        description=row.get("description", ""),
        factor_weights=json.loads(row.get("factor_weights", "{}")),
        universe=json.loads(row.get("universe", "[]")),
        config=json.loads(row.get("config", "{}")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        paper_started_at=row.get("paper_started_at"),
        live_started_at=row.get("live_started_at"),
        stopped_at=row.get("stopped_at"),
        archived_at=row.get("archived_at"),
    )


def _record_to_row(rec: StrategyRecord) -> dict:
    """Convert a StrategyRecord to a DB-compatible row dict."""
    import json

    return {
        "id": rec.id,
        "name": rec.name,
        "sleeve": rec.sleeve,
        "status": rec.status.value,
        "description": rec.description,
        "factor_weights": json.dumps(rec.factor_weights),
        "universe": json.dumps(rec.universe),
        "config": json.dumps(rec.config),
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "paper_started_at": rec.paper_started_at,
        "live_started_at": rec.live_started_at,
        "stopped_at": rec.stopped_at,
        "archived_at": rec.archived_at,
    }


# ---------------------------------------------------------------------------
# Lifecycle operations
# ---------------------------------------------------------------------------


def create_strategy(defn: StrategyDef) -> StrategyRecord:
    """Create a new strategy in draft state.

    Parameters
    ----------
    defn : StrategyDef
        Strategy definition (name, sleeve, factor_weights, etc.).

    Returns
    -------
    StrategyRecord
        The newly created strategy record.
    """
    now = datetime.now(timezone.utc).isoformat()
    rec = StrategyRecord(
        id=str(uuid.uuid4()),
        name=defn.name,
        sleeve=defn.sleeve,
        status=StrategyStatus.DRAFT,
        description=defn.description,
        factor_weights=defn.factor_weights,
        universe=defn.universe,
        config=defn.config,
        created_at=now,
        updated_at=now,
    )

    conn = _get_conn(readonly=False)
    try:
        row = _record_to_row(rec)
        columns = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO strategies ({columns}) VALUES ({placeholders})",
            list(row.values()),
        )
        conn.commit()
        logger.info("Created strategy %s (%s)", rec.id, rec.name)
        return rec
    finally:
        conn.close()


def get_strategy(strategy_id: str) -> Optional[StrategyRecord]:
    """Retrieve a strategy by ID.

    Parameters
    ----------
    strategy_id : str
        The strategy UUID.

    Returns
    -------
    StrategyRecord or None
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(dict(row))
    finally:
        conn.close()


def list_strategies(
    status: Optional[str] = None,
    sleeve: Optional[str] = None,
    limit: int = 50,
) -> list[StrategyRecord]:
    """List strategies with optional filters.

    Parameters
    ----------
    status : str or None
        Filter by status value (e.g. 'live', 'draft').
    sleeve : str or None
        Filter by sleeve (A, B, C).
    limit : int
        Maximum number of records.

    Returns
    -------
    list[StrategyRecord]
    """
    conn = _get_conn()
    try:
        sql = "SELECT * FROM strategies WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if sleeve:
            sql += " AND sleeve = ?"
            params.append(sleeve)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_record(dict(r)) for r in rows]
    finally:
        conn.close()


def transition_strategy(strategy_id: str, new_status: str) -> StrategyRecord:
    """Transition a strategy to a new status.

    Validates the transition is allowed and updates timestamps
    (e.g. sets ``live_started_at`` when going live).

    Parameters
    ----------
    strategy_id : str
        The strategy UUID.
    new_status : str
        Target status (see StrategyStatus enum values).

    Returns
    -------
    StrategyRecord
        The updated strategy record.

    Raises
    ------
    ValueError
        If strategy not found or transition is invalid.
    """
    target = StrategyStatus(new_status)
    rec = get_strategy(strategy_id)
    if rec is None:
        raise ValueError(f"Strategy not found: {strategy_id}")

    if target not in TRANSITIONS.get(rec.status, set()):
        raise ValueError(
            f"Invalid transition: {rec.status.value} → {target.value}. "
            f"Allowed: {[s.value for s in TRANSITIONS.get(rec.status, set())]}"
        )

    now = datetime.now(timezone.utc).isoformat()
    rec.status = target
    rec.updated_at = now

    # Set milestone timestamps
    if target == StrategyStatus.PAPER_TRADING and rec.paper_started_at is None:
        rec.paper_started_at = now
    if target == StrategyStatus.LIVE and rec.live_started_at is None:
        rec.live_started_at = now
    if target == StrategyStatus.STOPPED:
        rec.stopped_at = now
    if target == StrategyStatus.ARCHIVED:
        rec.archived_at = now

    # Persist
    conn = _get_conn(readonly=False)
    try:
        conn.execute(
            """UPDATE strategies SET
                status = ?, updated_at = ?,
                paper_started_at = ?, live_started_at = ?,
                stopped_at = ?, archived_at = ?
            WHERE id = ?""",
            (
                rec.status.value, rec.updated_at,
                rec.paper_started_at, rec.live_started_at,
                rec.stopped_at, rec.archived_at,
                rec.id,
            ),
        )
        conn.commit()
        logger.info(
            "Strategy %s transitioned: %s → %s",
            rec.id, rec.status.value if rec.status != target else "???", target.value,
        )
        return rec
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def start_paper(strategy_id: str) -> StrategyRecord:
    """Start paper trading for a strategy (draft → paper_trading)."""
    return transition_strategy(strategy_id, "paper_trading")


def go_live(strategy_id: str) -> StrategyRecord:
    """Take a strategy live (paper_trading → live)."""
    return transition_strategy(strategy_id, "live")


def pause(strategy_id: str) -> StrategyRecord:
    """Pause a live strategy (live → paused)."""
    return transition_strategy(strategy_id, "paused")


def stop(strategy_id: str) -> StrategyRecord:
    """Stop a strategy (any active → stopped)."""
    return transition_strategy(strategy_id, "stopped")


def archive(strategy_id: str) -> StrategyRecord:
    """Archive a strategy (any → archived)."""
    return transition_strategy(strategy_id, "archived")
