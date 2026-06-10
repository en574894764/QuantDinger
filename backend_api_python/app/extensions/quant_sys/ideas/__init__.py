"""Ideas Pool blueprint — submit, track, and validate investment ideas."""

from flask import Blueprint

ideas_bp = Blueprint("quant_ideas", __name__, url_prefix="/api/quant/ideas")


def init_app(app):
    """Register the ideas blueprint with the Flask app."""
    from app.extensions.quant_sys.ideas import routes  # noqa: F401
    app.register_blueprint(ideas_bp)
