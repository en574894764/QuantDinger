"""Risk API routes — combined risk overview + signal management + checks."""

import logging

from flask import jsonify, request

from app.extensions.quant_sys.risk import risk_bp
from app.extensions.quant_sys.risk.data import (
    get_risk_events,
    get_strategy_state,
    get_daily_snapshots,
    get_alerts,
    get_risk_overview,
)
from app.extensions.quant_sys.risk.checker import RiskChecker
from app.extensions.quant_sys.risk.signals import (
    confirm_signal,
    reject_signal,
    expire_stale_signals,
    get_pending_signals,
    get_signal_detail,
    get_signal_stats,
    insert_signal,
)
from app.extensions.quant_sys.risk.cooldown import (
    check_cooldown,
    set_cooldown,
    clear_cooldown,
    trigger_drawdown_cooldown,
)
from app.extensions.quant_sys.risk.pre_market import run_pre_market_checks
from app.extensions.quant_sys.risk.audit import log_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Existing read‑only endpoints (preserved)
# ---------------------------------------------------------------------------


@risk_bp.route("/events")
def risk_events():
    """Recent risk events (breaches, warnings)."""
    severity = request.args.get("severity", "")
    limit = request.args.get("limit", 100, type=int)
    data = get_risk_events(severity=severity, limit=limit)
    return jsonify({"count": len(data), "data": data})


@risk_bp.route("/strategy_state")
def strategy_state():
    """Active strategy states (running/stopped/error per sleeve)."""
    sleeve = request.args.get("sleeve", "")
    data = get_strategy_state(sleeve=sleeve)
    return jsonify({"count": len(data), "data": data})


@risk_bp.route("/snapshots")
def daily_snapshots():
    """Daily portfolio snapshots (value, PnL, drawdown)."""
    limit = request.args.get("limit", 60, type=int)
    data = get_daily_snapshots(limit=limit)
    return jsonify({"count": len(data), "data": data})


@risk_bp.route("/alerts")
def alerts():
    """Active and recent alerts."""
    limit = request.args.get("limit", 100, type=int)
    data = get_alerts(limit=limit)
    return jsonify({"count": len(data), "data": data})


@risk_bp.route("/overview")
def risk_overview():
    """Combined risk overview: latest snapshot + active alerts + recent events."""
    overview = get_risk_overview()
    return jsonify(overview)


# ---------------------------------------------------------------------------
# Risk checker endpoints
# ---------------------------------------------------------------------------


@risk_bp.route("/check-signal", methods=["POST"])
def route_check_signal():
    """Run RiskChecker.check_signal() against a prospective trade.

    JSON body:
        signal (dict): {symbol, direction, order_size_pct, sleeve}
        portfolio_value (float, optional): current portfolio value
        positions (list, optional): current positions
    """
    body = request.get_json(silent=True) or {}
    signal = body.get("signal", body)  # Accept {signal: {...}} or flat {...}
    portfolio_value = float(body.get("portfolio_value", 0))

    checker = RiskChecker()
    try:
        result = checker.check_signal(
            signal=signal,
            portfolio_value=portfolio_value,
            positions=body.get("positions"),
        )
        log_event(
            event_type="risk_check_signal",
            summary=f"Signal check: {result['status']} — {signal.get('symbol', '?')}",
            severity="high" if result["status"] == "block" else "info",
            signal_id=signal.get("id", ""),
            symbol=signal.get("symbol", ""),
            detail=result,
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("check-signal failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@risk_bp.route("/check-account", methods=["POST"])
def route_check_account():
    """Run RiskChecker.check_account() for drawdown check.

    JSON body:
        portfolio_value (float): current portfolio value
        peak_value (float): all‑time peak value
    """
    body = request.get_json(silent=True) or {}
    portfolio_value = float(body.get("portfolio_value", 0))
    peak_value = float(body.get("peak_value", 0))

    checker = RiskChecker()
    try:
        result = checker.check_account(portfolio_value, peak_value)
        log_event(
            event_type="risk_check_account",
            summary=f"Account check: {result['status']} — DD {result.get('drawdown_pct', 0):.1%}",
            severity="high" if result["status"] == "block" else "info",
            detail=result,
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("check-account failed")
        return jsonify({"status": "error", "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Signal management endpoints
# ---------------------------------------------------------------------------


@risk_bp.route("/signals/confirm", methods=["POST"])
def route_confirm_signal():
    """Confirm a pending signal → 'confirmed'.

    JSON body: {signal_id: str}
    """
    body = request.get_json(silent=True) or {}
    signal_id = body.get("signal_id", "")

    if not signal_id:
        return jsonify({"success": False, "error": "signal_id required"}), 400

    try:
        result = confirm_signal(signal_id)
        if result["success"]:
            log_event(
                event_type="signal_confirm",
                summary=f"Signal {signal_id} confirmed",
                severity="info",
                signal_id=signal_id,
            )
        return jsonify(result)
    except Exception as e:
        logger.exception("confirm-signal failed")
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/signals/reject", methods=["POST"])
def route_reject_signal():
    """Reject a pending signal → 'rejected'.

    JSON body: {signal_id: str, reason: str (optional)}
    """
    body = request.get_json(silent=True) or {}
    signal_id = body.get("signal_id", "")
    reason = body.get("reason", "")

    if not signal_id:
        return jsonify({"success": False, "error": "signal_id required"}), 400

    try:
        result = reject_signal(signal_id, reason=reason)
        if result["success"]:
            log_event(
                event_type="signal_reject",
                summary=f"Signal {signal_id} rejected: {reason}",
                severity="medium",
                signal_id=signal_id,
                detail={"reason": reason},
            )
        return jsonify(result)
    except Exception as e:
        logger.exception("reject-signal failed")
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/signals/pending")
def route_pending_signals():
    """List signals in pending / confirmed / pre_check_passed / blocked_soft states.

    Query params: sleeve (optional filter), limit (default 200)
    """
    sleeve = request.args.get("sleeve", "")
    limit = request.args.get("limit", 200, type=int)
    try:
        result = get_pending_signals(sleeve=sleeve, limit=limit)
        return jsonify(result)
    except Exception as e:
        logger.exception("pending-signals failed")
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/signals/stats")
def route_signal_stats():
    """Aggregate signal statistics.

    Query params: start_date, end_date (YYYYMMDD)
    """
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    try:
        result = get_signal_stats(start_date=start_date, end_date=end_date)
        return jsonify(result)
    except Exception as e:
        logger.exception("signal-stats failed")
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/signals/<signal_id>")
def route_signal_detail(signal_id):
    """Get full detail for one signal."""
    try:
        result = get_signal_detail(signal_id)
        return jsonify(result)
    except Exception as e:
        logger.exception("signal-detail failed for %s", signal_id)
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/signals/expire", methods=["POST"])
def route_expire_stale():
    """Expire stale pending signals.

    JSON body: {trade_date: str (YYYYMMDD), days: int (default 5)}
    """
    body = request.get_json(silent=True) or {}
    trade_date = body.get("trade_date", "")
    days = body.get("days", 5)

    if not trade_date:
        return jsonify({"success": False, "error": "trade_date required"}), 400

    try:
        result = expire_stale_signals(trade_date, days=days)
        log_event(
            event_type="signal_expire",
            summary=f"Expired {result.get('expired_count', 0)} stale signals",
            severity="info",
            detail=result,
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("expire-stale failed")
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/signals/insert", methods=["POST"])
def route_insert_signal():
    """Insert a new pending signal.

    JSON body: {id, symbol, direction, order_size_pct, sleeve, ...}
    """
    body = request.get_json(silent=True) or {}
    required = ["id", "symbol"]
    for field in required:
        if field not in body:
            return jsonify({"success": False, "error": f"{field} required"}), 400

    try:
        result = insert_signal(body)
        if result["success"]:
            log_event(
                event_type="signal_insert",
                summary=f"Signal {body['id']} inserted: {body.get('symbol')} {body.get('direction')}",
                severity="info",
                signal_id=body["id"],
                symbol=body.get("symbol", ""),
            )
        return jsonify(result)
    except Exception as e:
        logger.exception("insert-signal failed")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Cooldown endpoints
# ---------------------------------------------------------------------------


@risk_bp.route("/cooldown/check", methods=["POST"])
def route_cooldown_check():
    """Check if a scope (or any) is currently in cooldown.

    JSON body (optional): {scope: str}  — omit to check all scopes.
    """
    body = request.get_json(silent=True) or {}
    scope = body.get("scope", None)
    try:
        result = check_cooldown(scope=scope)
        return jsonify(result)
    except Exception as e:
        logger.exception("cooldown-check failed")
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/cooldown/set", methods=["POST"])
def route_cooldown_set():
    """Manually set cooldown for a scope.

    JSON body: {scope: str, hours: int (optional), severity: str, trigger_event: str}
    """
    body = request.get_json(silent=True) or {}
    scope = body.get("scope", "")
    if not scope:
        return jsonify({"success": False, "error": "scope required"}), 400

    try:
        result = set_cooldown(
            scope=scope,
            hours=body.get("hours"),
            severity=body.get("severity", "hard"),
            trigger_event=body.get("trigger_event", "manual"),
        )
        log_event(
            event_type="cooldown_set",
            summary=f"Cooldown set: {scope} until {result.get('cooldown_until')}",
            severity="high",
            scope=scope,
            detail=result,
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("cooldown-set failed")
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/cooldown/clear", methods=["POST"])
def route_cooldown_clear():
    """Clear cooldown for a scope.

    JSON body: {scope: str}
    """
    body = request.get_json(silent=True) or {}
    scope = body.get("scope", "")
    if not scope:
        return jsonify({"success": False, "error": "scope required"}), 400

    try:
        result = clear_cooldown(scope)
        log_event(
            event_type="cooldown_clear",
            summary=f"Cooldown cleared: {scope}",
            severity="info",
            scope=scope,
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("cooldown-clear failed")
        return jsonify({"success": False, "error": str(e)}), 500


@risk_bp.route("/cooldown/trigger", methods=["POST"])
def route_cooldown_trigger():
    """Trigger drawdown‑based cooldown automatically.

    JSON body: {sleeve: str, drawdown_pct: float}
    """
    body = request.get_json(silent=True) or {}
    sleeve = body.get("sleeve", "")
    drawdown_pct = float(body.get("drawdown_pct", 0))

    try:
        result = trigger_drawdown_cooldown(sleeve=sleeve, drawdown_pct=drawdown_pct)
        log_event(
            event_type="cooldown_trigger",
            summary=f"Cooldown triggered: sleeve={sleeve} DD={drawdown_pct:.1%}",
            severity="high" if drawdown_pct >= 0.10 else "medium",
            scope=f"sleeve:{sleeve}" if sleeve else "account",
            detail=result,
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("cooldown-trigger failed")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Pre‑market check endpoint
# ---------------------------------------------------------------------------


@risk_bp.route("/pre-market", methods=["POST"])
def route_pre_market():
    """Run pre‑market risk checks on all pending signals.

    JSON body (optional): {trade_date: str (YYYYMMDD)}
    """
    body = request.get_json(silent=True) or {}
    trade_date = body.get("trade_date", "")

    try:
        result = run_pre_market_checks(trade_date=trade_date)
        log_event(
            event_type="pre_market_run",
            summary=f"Pre‑market check: {result.get('passed', 0)} passed, "
                     f"{result.get('blocked_hard', 0)} blocked",
            severity="info",
            detail={"trade_date": result.get("trade_date"),
                    "passed": result.get("passed"),
                    "blocked_hard": result.get("blocked_hard")},
        )
        return jsonify(result)
    except Exception as e:
        logger.exception("pre-market check failed")
        return jsonify({"success": False, "error": str(e)}), 500
