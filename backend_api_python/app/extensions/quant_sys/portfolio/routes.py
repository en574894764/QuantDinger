"""Portfolio API routes — positions, trades, summary."""

from flask import jsonify, request

from app.extensions.quant_sys.portfolio import portfolio_bp
from app.extensions.quant_sys.portfolio.data import (
    get_positions,
    get_trades,
    get_portfolio_summary,
)


@portfolio_bp.route("/positions")
def positions():
    """Current open positions."""
    sleeve = request.args.get("sleeve", "")
    status = request.args.get("status", "")
    data = get_positions(sleeve=sleeve, status=status)
    return jsonify({"count": len(data), "data": data})


@portfolio_bp.route("/trades")
def trades():
    """Trade history."""
    sleeve = request.args.get("sleeve", "")
    ts_code = request.args.get("ts_code", "")
    limit = request.args.get("limit", 200, type=int)
    data = get_trades(sleeve=sleeve, ts_code=ts_code, limit=limit)
    return jsonify({"count": len(data), "data": data})


@portfolio_bp.route("/summary")
def portfolio_summary():
    """Aggregated portfolio summary."""
    summary = get_portfolio_summary()
    return jsonify(summary)
