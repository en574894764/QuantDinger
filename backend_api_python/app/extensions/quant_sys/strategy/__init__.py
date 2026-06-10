"""Strategy experiments blueprint — reads strategy_experiment_log from SQLite."""

from flask import Blueprint

strategy_bp = Blueprint("quant_strategy", __name__, url_prefix="/api/quant/strategy")


def init_app(app):
    """Register the strategy blueprint with the Flask app."""
    from app.extensions.quant_sys.strategy import routes  # noqa: F401
    app.register_blueprint(strategy_bp)
