"""Quant System Flask Blueprint — A-share data pipeline, risk, strategy, signals.

Loaded as a plugin into QuantDinger backend. All routes under /api/quant/*.
Does NOT modify any original QuantDinger code.
"""

import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

quant_bp = Blueprint("quant_sys", __name__, url_prefix="/api/quant")


@quant_bp.route("/health")
def health():
    return jsonify({"status": "ok", "module": "quant_sys"})


@quant_bp.route("/data/status")
def data_status():
    """Return data pipeline status (table row counts, last update dates)."""
    from app.extensions.quant_sys.data.pipeline import get_pipeline_status
    return jsonify(get_pipeline_status())


@quant_bp.route("/kline/cnstock")
def kline_cnstock():
    """Get A-share K-line data for CNStock market."""
    symbol = request.args.get("symbol", "")
    timeframe = request.args.get("timeframe", "1D")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    from app.extensions.quant_sys.data.pipeline import get_kline_data
    return jsonify(get_kline_data(symbol=symbol, timeframe=timeframe,
                                  start=start, end=end))


@quant_bp.route("/fundamentals")
def fundamentals():
    """Get stock fundamental data."""
    symbol = request.args.get("symbol", "")
    from app.extensions.quant_sys.data.pipeline import get_fundamentals
    return jsonify(get_fundamentals(symbol))


# /macro routes are now handled by the dedicated macro_bp blueprint
# (app/extensions/quant_sys/macro/routes.py) which provides:
#   GET /api/quant/macro/list              — list available indicators
#   GET /api/quant/macro/<indicator>       — get indicator data
#   GET /api/quant/macro/<indicator>/latest — get latest value

# ---------------------------------------------------------------------------
# Index data refresh endpoints
# ---------------------------------------------------------------------------
@quant_bp.route("/data/index/refresh", methods=["POST"])
def index_refresh():
    """Trigger index data refresh (backfill or daily increment).

    Query params:
        mode   — 'today' (last 7 days, default) or 'backfill'
        start  — start date YYYYMMDD (required for backfill)
        end    — end date YYYYMMDD (optional, defaults to today)
    """
    mode = request.args.get("mode", "today").strip().lower()
    from app.extensions.quant_sys.data.index_refresh import (
        run_backfill,
        run_daily_increment,
    )

    try:
        if mode == "backfill":
            start = request.args.get("start", "")
            end = request.args.get("end", "")
            if not start:
                return jsonify({
                    "success": False,
                    "error": "start parameter required for backfill mode",
                }), 400
            result = run_backfill(start, end)
        elif mode == "today":
            result = run_daily_increment()
        else:
            return jsonify({
                "success": False,
                "error": f"Invalid mode '{mode}'. Use 'today' or 'backfill'.",
            }), 400
    except FileNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 503
    except Exception as e:
        logger.error("Index refresh failed: %s", e, exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500

    status_code = 200 if result.get("success") else 500
    return jsonify(result), status_code


@quant_bp.route("/data/index/status")
def index_status():
    """Get index data status: row counts, latest dates per index."""
    from app.extensions.quant_sys.data.index_refresh import get_index_status
    try:
        result = get_index_status()
        return jsonify({"success": True, **result})
    except Exception as e:
        logger.error("Index status check failed: %s", e, exc_info=True)
        return jsonify({
            "success": False,
            "error": str(e),
            "hint": "Run POST /api/quant/data/index/refresh first to create the table",
        }), 500


@quant_bp.route("/data/pipeline/run", methods=["POST"])
def pipeline_run():
    """Manually trigger the daily data pipeline.

    Query params:
        date  — trade date YYYYMMDD (default: today, skips weekends)
    """
    date = request.args.get("date", "")
    try:
        from app.extensions.quant_sys.data.daily_pipeline import run_daily_pipeline
        result = run_daily_pipeline(date=date)
        status_code = 200 if result.get("status") != "failed" else 500
        return jsonify(result), status_code
    except Exception as e:
        logger.error("Pipeline run failed: %s", e, exc_info=True)
        return jsonify({"status": "failed", "error": str(e)}), 500


@quant_bp.route("/data/pipeline/status")
def pipeline_status():
    """Get the current daily pipeline state."""
    try:
        from app.extensions.quant_sys.data.pipeline_state import (
            PipelineStateManager,
        )
        state = PipelineStateManager().load()
        return jsonify(state)
    except Exception as e:
        logger.error("Pipeline status check failed: %s", e, exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Data cross-validation endpoints
# ---------------------------------------------------------------------------
@quant_bp.route("/data/validate", methods=["POST"])
def data_validate():
    """Run cross-validation (PG vs Parquet) for all categories.

    Query params:
        date — trade date YYYYMMDD (default: today)
    """
    date = request.args.get("date", "")
    try:
        from app.extensions.quant_sys.data.cross_validate import validate_all
        result = validate_all(date=date)
        status_code = 200 if result.get("overall_pass") else 200
        return jsonify(result), status_code
    except Exception as e:
        logger.error("Cross-validation failed: %s", e, exc_info=True)
        return jsonify({"error": str(e), "overall_pass": False}), 500


@quant_bp.route("/data/validate/status")
def data_validate_status():
    """Get latest cross-validation results from disk."""
    try:
        from app.extensions.quant_sys.data.cross_validate import get_latest_validation
        result = get_latest_validation()
        if not result:
            return (
                jsonify(
                    {
                        "status": "no_data",
                        "message": "No validation results yet. Run POST /api/quant/data/validate first.",
                    }
                ),
                200,
            )
        return jsonify(result)
    except Exception as e:
        logger.error("Validation status failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Backfill endpoint
# ---------------------------------------------------------------------------
@quant_bp.route("/data/backfill", methods=["POST"])
def data_backfill():
    """Run historical data backfill for a date range.

    Query params:
        start — start date YYYYMMDD (required)
        end   — end date YYYYMMDD (default: today)
        market — 'a_shares' (default), 'etf', 'hk_connect'
    """
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    market = request.args.get("market", "a_shares").strip()

    if not start:
        return jsonify({"success": False, "error": "start parameter is required"}), 400

    try:
        from app.extensions.quant_sys.data.backfill import run_backfill
        result = run_backfill(start_date=start, end_date=end, market=market)
        status_code = 200 if result.get("successful", 0) > 0 or result.get("skipped", 0) > 0 else 500
        return jsonify({"success": True, **result}), status_code
    except Exception as e:
        logger.error("Backfill failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@quant_bp.route("/data/backfill/progress")
def data_backfill_progress():
    """Get current backfill progress."""
    try:
        from app.extensions.quant_sys.data.backfill import _get_progress
        progress = _get_progress()
        return jsonify(progress)
    except Exception as e:
        logger.error("Backfill progress check failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Rebuild Parquet endpoint
# ---------------------------------------------------------------------------
@quant_bp.route("/data/rebuild-parquet", methods=["POST"])
def data_rebuild_parquet():
    """Rebuild Parquet files from PG data.

    Query params:
        category    — 'a_shares' (default), 'stock_basic', 'financials', 'all'
        incremental — 'true' (default) or 'false'
        dates       — comma-separated YYYYMMDD dates (optional, for a_shares only)
    """
    category = request.args.get("category", "a_shares").strip()
    incremental = request.args.get("incremental", "true").strip().lower() in ("1", "true", "yes")
    dates_str = request.args.get("dates", "").strip()

    kwargs = {"incremental": incremental}
    if dates_str:
        kwargs["dates"] = [d.strip() for d in dates_str.split(",") if d.strip()]

    try:
        from app.extensions.quant_sys.data.rebuild_parquet import rebuild_all
        result = rebuild_all(category=category, **kwargs)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        logger.error("Rebuild parquet failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@quant_bp.route("/data/restore", methods=["POST"])
def data_restore():
    """Restore database from backup file.

    Query params:
        file — Backup filename or path (required).
        type — ``pg`` or ``sqlite`` (auto-detect if omitted).
    """
    file_param = request.args.get("file", "").strip()
    db_type = request.args.get("type", "").strip().lower()

    if not file_param:
        return jsonify({"success": False, "error": "file parameter is required"}), 400

    try:
        from app.extensions.quant_sys.data.restore import restore_from_backup
        result = restore_from_backup(file_param, db_type)
        status_code = 200 if result.get("success") else 400
        return jsonify(result), status_code
    except Exception as e:
        logger.error("Restore failed: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


def init_app(app):
    """Register the quant_sys blueprint with the Flask app."""
    # Import side-effect modules to attach routes to quant_bp
    from app.extensions.quant_sys import indicator  # noqa: F401
    from app.extensions.quant_sys import market_routes  # noqa: F401
    app.register_blueprint(quant_bp)