"""Quant System backup module — pg_dump + sqlite3 .backup with rotation.

Registers backup routes on the existing quant_sys blueprint.  No modifications
to quant_sys/__init__.py are needed — just import this module after quant_init().
"""

from app.extensions.quant_sys.backup.routes import register_routes

__all__ = ["register_routes"]