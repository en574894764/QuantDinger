"""Macro data API routes — serve Parquet-based macro indicators."""

from flask import jsonify, request

from app.extensions.quant_sys.macro import macro_bp
from app.extensions.quant_sys.macro.data import (
    get_macro_indicators,
    get_macro_indicator,
    get_macro_latest,
)


INDICATORS = ["cpi", "pmi", "gdp", "shibor", "lpr", "money_supply", "bond_yield_10y"]


@macro_bp.route("/list")
def macro_list():
    """List available macro indicators."""
    return jsonify({
        "indicators": INDICATORS,
        "count": len(INDICATORS),
    })


@macro_bp.route("/<indicator>")
def macro_indicator(indicator: str):
    """Get all data for a specific macro indicator."""
    if indicator not in INDICATORS:
        return jsonify({"error": f"Unknown indicator: {indicator}",
                        "available": INDICATORS}), 404

    limit = request.args.get("limit", 200, type=int)
    data = get_macro_indicator(indicator, limit=limit)
    return jsonify({"indicator": indicator, "count": len(data), "data": data})


@macro_bp.route("/<indicator>/latest")
def macro_latest(indicator: str):
    """Get the latest value for a specific macro indicator."""
    if indicator not in INDICATORS:
        return jsonify({"error": f"Unknown indicator: {indicator}",
                        "available": INDICATORS}), 404

    record = get_macro_latest(indicator)
    if record is None:
        return jsonify({"indicator": indicator, "data": None,
                        "message": "No data available"})
    return jsonify({"indicator": indicator, "data": record})
