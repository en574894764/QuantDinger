"""Portfolio blueprint — reads positions and trades from SQLite."""

from flask import Blueprint

portfolio_bp = Blueprint("quant_portfolio", __name__, url_prefix="/api/quant/portfolio")


def init_app(app):
    """Register the portfolio blueprint with the Flask app."""
    from app.extensions.quant_sys.portfolio import routes  # noqa: F401
    app.register_blueprint(portfolio_bp)
