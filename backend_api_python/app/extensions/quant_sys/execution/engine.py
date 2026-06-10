"""Execution engine — placeholder interface for vnpy integration.

This module is a stub. When vnpy is integrated, this will contain:

- Order submission via REST/gRPC to vnpy gateway
- Position sync (vnpy → QuantDinger)
- Trade confirmation callbacks
- Paper-trading fallback when vnpy is unavailable
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Placeholder interface
# ---------------------------------------------------------------------------


class ExecutionEngine:
    """Stub execution engine — not yet connected to vnpy.

    Attributes are placeholders for future integration.
    """

    def __init__(self) -> None:
        self.connected: bool = False
        self.mode: str = "paper"  # "paper" | "live"
        logger.info("Execution engine initialized (placeholder, paper mode)")

    def connect(self, gateway_config: dict[str, Any] | None = None) -> bool:
        """Connect to vnpy gateway (placeholder)."""
        logger.warning("vnpy not integrated — staying in paper mode")
        return False

    def submit_order(
        self,
        symbol: str,
        direction: str,
        volume: int,
        price: float = 0.0,
        order_type: str = "limit",
    ) -> dict[str, Any]:
        """Submit an order (placeholder — logs and returns a mock response)."""
        logger.info(
            "[PAPER] Order: %s %s %d @ %.2f (%s)",
            symbol, direction, volume, price, order_type,
        )
        return {
            "status": "paper_only",
            "message": "vnpy not integrated — order logged only",
            "order_id": None,
        }

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an order (placeholder)."""
        logger.info("[PAPER] Cancel order: %s", order_id)
        return {"status": "paper_only", "cancelled": False}

    def sync_positions(self) -> list[dict[str, Any]]:
        """Sync positions from vnpy (placeholder)."""
        logger.debug("Position sync — vnpy not integrated")
        return []

    def disconnect(self) -> None:
        """Disconnect from vnpy gateway (placeholder)."""
        self.connected = False
        logger.info("Execution engine disconnected")
