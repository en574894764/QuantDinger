"""Dashboard API routes — market overview, portfolio, risk, backtest summary.

All endpoints are read-only GETs under /api/quant/dashboard/*.
"""

import logging

from flask import jsonify, request

from app.extensions.quant_sys.dashboard import dashboard_bp

logger = logging.getLogger(__name__)


# ── Market Overview ──────────────────────────────────────────────────────────


@dashboard_bp.route("/market/overview")
def market_overview():
    """Get A-shares market overview.

    Returns indices, breadth (advance/decline), top gainers/losers,
    volume leaders, and sector performance.
    """
    try:
        from app.extensions.quant_sys.dashboard.api import get_market_overview

        overview = get_market_overview()
        return jsonify(overview)
    except Exception as e:
        logger.error("Market overview failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Portfolio Summary ────────────────────────────────────────────────────────


@dashboard_bp.route("/portfolio")
def portfolio_summary():
    """Get portfolio summary: open positions, recent trades, P&L snapshot.

    Query params:
        date — snapshot date YYYYMMDD (default: latest)
    """
    date = request.args.get("date", "")
    try:
        from app.extensions.quant_sys.dashboard.api import get_portfolio_summary

        summary = get_portfolio_summary()

        # Optionally overlay MCP-backed snapshot if date is provided
        if date:
            try:
                from app.extensions.quant_sys.portfolio.data import (
                    get_positions,
                )
                positions = get_positions()
                summary["positions"] = positions[:50]
            except Exception:
                logger.debug("Could not enrich portfolio with positions")

        return jsonify(summary)
    except Exception as e:
        logger.error("Portfolio summary failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Risk Dashboard ────────────────────────────────────────────────────────────


@dashboard_bp.route("/risk")
def risk_dashboard():
    """Get risk dashboard: drawdown, alerts, risk events, strategy state."""
    try:
        from app.extensions.quant_sys.dashboard.api import get_risk_dashboard

        dashboard = get_risk_dashboard()
        return jsonify(dashboard)
    except Exception as e:
        logger.error("Risk dashboard failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Backtest Summary ─────────────────────────────────────────────────────────


@dashboard_bp.route("/backtest/summary")
def backtest_summary():
    """Get backtest performance summary: experiments and aggregate stats.

    Query params:
        limit — max experiments to return (default 20)
    """
    limit = request.args.get("limit", 20, type=int)
    try:
        from app.extensions.quant_sys.dashboard.api import get_backtest_summary

        summary = get_backtest_summary(limit=limit)
        return jsonify(summary)
    except Exception as e:
        logger.error("Backtest summary failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500