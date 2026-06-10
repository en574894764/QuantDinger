"""Risk data access — reads risk_events, strategy_state, daily_snapshots, alerts from SQLite."""

from app.extensions.quant_sys.data.sqlite import query_table


def get_risk_events(severity: str = "", limit: int = 100) -> list:
    """Return recent risk events, optionally filtered by severity."""
    if severity:
        return query_table(
            "risk_events",
            order_by="created_at DESC",
            limit=limit,
            where_clause="severity = ?",
            params=(severity,),
        )
    return query_table("risk_events", order_by="created_at DESC", limit=limit)


def get_strategy_state(sleeve: str = "") -> list:
    """Return strategy state, optionally filtered by sleeve."""
    if sleeve:
        return query_table(
            "strategy_state",
            order_by="entered_at DESC",
            limit=200,
            where_clause="sleeve = ?",
            params=(sleeve,),
        )
    return query_table("strategy_state", order_by="entered_at DESC", limit=200)


def get_daily_snapshots(limit: int = 60) -> list:
    """Return recent daily PnL/value snapshots."""
    return query_table(
        "daily_snapshots",
        order_by="trade_date DESC",
        limit=limit,
    )


def get_alerts(limit: int = 100) -> list:
    """Return recent alerts."""
    return query_table("alerts", order_by="created_at DESC", limit=limit)


def get_risk_overview() -> dict:
    """Combined risk overview: latest snapshot, active alerts, recent events."""
    latest_snapshot = query_table(
        "daily_snapshots", order_by="trade_date DESC", limit=1
    )
    recent_events = get_risk_events(limit=20)
    active_alerts = get_alerts(limit=20)
    strategy_states = get_strategy_state()

    return {
        "latest_snapshot": latest_snapshot[0] if latest_snapshot else None,
        "recent_events": recent_events,
        "active_alerts": active_alerts,
        "strategy_states": strategy_states,
    }
