"""Risk overview blueprint — reads risk_events, strategy_state, daily_snapshots, alerts from SQLite."""

from flask import Blueprint

risk_bp = Blueprint("quant_risk", __name__, url_prefix="/api/quant/risk")


def init_app(app):
    """Register the risk blueprint with the Flask app."""
    from app.extensions.quant_sys.risk import routes  # noqa: F401
    app.register_blueprint(risk_bp)
