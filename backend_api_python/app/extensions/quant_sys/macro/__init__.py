"""Macro data blueprint — reads Chinese macro indicators from Parquet files.

Parquet files expected at QUANT_SYS_MACRO_DIR:
  cpi.parquet, pmi.parquet, gdp.parquet, shibor.parquet,
  lpr.parquet, money_supply.parquet, bond_yield_10y.parquet
"""

from flask import Blueprint

macro_bp = Blueprint("quant_macro", __name__, url_prefix="/api/quant/macro")


def init_app(app):
    """Register the macro blueprint with the Flask app."""
    # Import routes to attach them to the blueprint
    from app.extensions.quant_sys.macro import routes  # noqa: F401
    app.register_blueprint(macro_bp)
