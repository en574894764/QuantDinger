"""Broker abstraction for live and paper trading.

Defines an abstract Broker interface that both live broker adapters
and the paper trading engine implement. Provides a factory function
to instantiate the appropriate broker type.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & data classes
# ---------------------------------------------------------------------------


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    """A trading order."""

    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    submitted_at: str = ""
    filled_at: str = ""
    strategy_id: str = ""
    notes: str = ""


@dataclass
class Position:
    """A current position held in the account."""

    symbol: str
    quantity: int
    avg_cost: float
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class Account:
    """Broker account summary."""

    account_id: str
    cash: float
    total_value: float
    positions: list[Position] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Abstract broker interface
# ---------------------------------------------------------------------------


class Broker(ABC):
    """Abstract broker interface for order execution and account management.

    Subclasses implement concrete broker connections (live broker APIs
    or paper trading simulation).
    """

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to the broker. Returns True on success."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down broker connection."""
        ...

    @abstractmethod
    def get_account(self) -> Account:
        """Return the current account summary."""
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Return list of open positions."""
        ...

    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """Submit an order. Returns the order with updated status/IDs."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> Order:
        """Cancel a pending order. Returns updated order."""
        ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        """Query the status of a specific order."""
        ...

    @abstractmethod
    def get_orders(
        self, status: Optional[OrderStatus] = None, limit: int = 50
    ) -> list[Order]:
        """List orders, optionally filtered by status."""
        ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_broker(broker_type: str, config: dict[str, Any] | None = None) -> Broker:
    """Factory function to create a broker instance.

    Parameters
    ----------
    broker_type : str
        One of: 'paper', 'xtp', 'ctp', 'oanda', 'ib'.
    config : dict or None
        Broker-specific configuration (API keys, endpoints, etc.).

    Returns
    -------
    Broker
        A concrete broker instance.

    Raises
    ------
    ValueError
        If broker_type is unknown.
    """
    cfg = config or {}

    if broker_type == "paper":
        from app.extensions.quant_sys.strategy.paper_trading.engine import PaperBroker

        return PaperBroker(
            initial_capital=cfg.get("initial_capital", 1_000_000.0),
            commission_rate=cfg.get("commission_rate", 0.0003),
            slippage=cfg.get("slippage", 0.001),
        )

    elif broker_type in ("xtp", "ctp"):
        # Placeholder for Chinese broker adapters
        raise NotImplementedError(
            f"Live broker '{broker_type}' not yet implemented. "
            "Integrate with XTP/CTP SDK as needed."
        )

    elif broker_type == "oanda":
        raise NotImplementedError(
            "OANDA broker integration not yet implemented."
        )

    elif broker_type == "ib":
        raise NotImplementedError(
            "Interactive Brokers integration not yet implemented."
        )

    else:
        raise ValueError(
            f"Unknown broker_type: '{broker_type}'. "
            "Supported: paper, xtp, ctp, oanda, ib."
        )
