"""Dashboard blueprint — market overview, portfolio summary, risk dashboard.

Provides read-only aggregated views for the frontend dashboard.
Routes are all under /api/quant/dashboard/*.
"""

from flask import Blueprint

dashboard_bp = Blueprint(
    "quant_dashboard", __name__, url_prefix="/api/quant/dashboard"
)


def init_app(app):
    """Register the dashboard blueprint with the Flask app."""
    from app.extensions.quant_sys.dashboard import routes  # noqa: F401

    app.register_blueprint(dashboard_bp)