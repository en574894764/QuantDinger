"""Paper trading engine — simulates fills at next-day open.

Implements the Broker interface in-memory, tracking positions,
orders, P&L, and account value. Uses ParquetStore to read daily
OHLCV data for fill price simulation.

Designed to be a drop-in replacement for live broker adapters
so strategy code remains identical between paper and live.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from app.extensions.quant_sys.data.store.parquet import ParquetStore
from app.extensions.quant_sys.strategy.live.broker import (
    Account,
    Broker,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal data
# ---------------------------------------------------------------------------


@dataclass
class _PaperPosition:
    """Internal position tracking for the paper broker."""

    symbol: str
    quantity: int
    avg_cost: float
    realized_pnl: float = 0.0


# ---------------------------------------------------------------------------
# PaperBroker
# ---------------------------------------------------------------------------


class PaperBroker(Broker):
    """In-memory paper trading broker that simulates fills.

    Orders are filled at the next trading day's open price (slippage
    and commission applied). Position tracking and P&L are computed
    from historical data loaded via ParquetStore.

    Parameters
    ----------
    initial_capital : float
        Starting cash (default 1,000,000).
    commission_rate : float
        Per-trade commission as fraction of notional (default 0.03%).
    slippage : float
        Slippage as fraction of fill price (default 0.1%).
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        commission_rate: float = 0.0003,
        slippage: float = 0.001,
    ):
        self._initial_capital = initial_capital
        self._commission_rate = commission_rate
        self._slippage = slippage
        self._connected: bool = False

        # State
        self._cash: float = initial_capital
        self._positions: dict[str, _PaperPosition] = {}
        self._orders: dict[str, Order] = {}
        self._account_id: str = f"paper_{uuid.uuid4().hex[:8]}"

        # Data store for price lookups
        self._store = ParquetStore()

    # ------------------------------------------------------------------
    # Broker interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Initialize paper broker (no network connection needed)."""
        self._connected = True
        logger.info("PaperBroker connected (account=%s, capital=%.2f)",
                     self._account_id, self._initial_capital)
        return True

    def disconnect(self) -> None:
        """Mark broker as disconnected."""
        self._connected = False
        logger.info("PaperBroker disconnected")

    def get_account(self) -> Account:
        """Return current paper account summary."""
        positions = self.get_positions()
        mv = sum(p.market_value for p in positions)
        total = self._cash + mv
        return Account(
            account_id=self._account_id,
            cash=self._cash,
            total_value=total,
            positions=positions,
        )

    def get_positions(self) -> list[Position]:
        """Return list of open paper positions with mark-to-market."""
        result: list[Position] = []
        for sym, pp in self._positions.items():
            if pp.quantity == 0:
                continue
            # Attempt to get latest close price
            latest_price = self._get_latest_price(sym)
            mv = pp.quantity * latest_price
            upnl = mv - (pp.quantity * pp.avg_cost)
            result.append(Position(
                symbol=sym,
                quantity=pp.quantity,
                avg_cost=pp.avg_cost,
                market_value=mv,
                unrealized_pnl=upnl,
                realized_pnl=pp.realized_pnl,
            ))
        return result

    def submit_order(self, order: Order) -> Order:
        """Submit an order for next-day fill simulation.

        Paper orders are filled immediately at the next trading day's
        open price (simulated via the most recent close for simplicity).
        In production, you'd look up the actual next open from data.

        Parameters
        ----------
        order : Order
            Order to submit (market or limit).

        Returns
        -------
        Order
            The order with status updated (FILLED / REJECTED).
        """
        if not self._connected:
            order.status = OrderStatus.REJECTED
            order.notes = "Broker not connected"
            return order

        order.order_id = order.order_id or f"ord_{uuid.uuid4().hex[:8]}"
        order.submitted_at = datetime.now(timezone.utc).isoformat()

        # Validate
        if order.quantity <= 0:
            order.status = OrderStatus.REJECTED
            order.notes = "Quantity must be positive"
            self._orders[order.order_id] = order
            return order

        # Get fill price
        fill_price = self._get_fill_price(order)
        if fill_price <= 0:
            order.status = OrderStatus.REJECTED
            order.notes = f"No price data for {order.symbol}"
            self._orders[order.order_id] = order
            return order

        # Apply slippage
        if order.side == OrderSide.BUY:
            fill_price *= (1.0 + self._slippage)
        else:
            fill_price *= (1.0 - self._slippage)

        notional = order.quantity * fill_price
        commission = notional * self._commission_rate

        if order.side == OrderSide.BUY:
            total_cost = notional + commission
            if total_cost > self._cash:
                order.status = OrderStatus.REJECTED
                order.notes = (
                    f"Insufficient cash: need {total_cost:.2f}, "
                    f"have {self._cash:.2f}"
                )
                self._orders[order.order_id] = order
                return order
            self._cash -= total_cost
        else:
            # Sell
            pos = self._positions.get(order.symbol)
            if pos is None or pos.quantity < order.quantity:
                order.status = OrderStatus.REJECTED
                order.notes = (
                    f"Insufficient shares: have {pos.quantity if pos else 0}, "
                    f"need {order.quantity}"
                )
                self._orders[order.order_id] = order
                return order
            self._cash += notional - commission

        # Update position
        self._update_position(order.symbol, order.side, order.quantity, fill_price)

        # Mark order filled
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = fill_price
        order.filled_at = datetime.now(timezone.utc).isoformat()
        self._orders[order.order_id] = order

        logger.info(
            "Paper fill: %s %s %d @ %.2f (order=%s)",
            order.side.value.upper(), order.symbol,
            order.quantity, fill_price, order.order_id,
        )
        return order

    def cancel_order(self, order_id: str) -> Order:
        """Cancel a pending order (only if not yet filled)."""
        order = self._orders.get(order_id)
        if order is None:
            raise ValueError(f"Order not found: {order_id}")
        if order.status != OrderStatus.PENDING:
            raise ValueError(
                f"Cannot cancel order {order_id}: status is {order.status.value}"
            )
        order.status = OrderStatus.CANCELLED
        logger.info("Cancelled paper order %s", order_id)
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        """Get an order by ID."""
        return self._orders.get(order_id)

    def get_orders(
        self, status: Optional[OrderStatus] = None, limit: int = 50
    ) -> list[Order]:
        """List orders, optionally filtered by status."""
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o.status == status]
        orders.sort(key=lambda o: o.submitted_at, reverse=True)
        return orders[:limit]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_position(
        self, symbol: str, side: OrderSide, qty: int, price: float
    ) -> None:
        """Update internal position after a fill."""
        if symbol not in self._positions:
            self._positions[symbol] = _PaperPosition(
                symbol=symbol, quantity=0, avg_cost=0.0
            )
        pos = self._positions[symbol]

        if side == OrderSide.BUY:
            total_cost = pos.avg_cost * pos.quantity + price * qty
            pos.quantity += qty
            pos.avg_cost = total_cost / pos.quantity if pos.quantity > 0 else 0.0
        else:
            # Sell — realize P&L on the sold portion
            if pos.quantity > 0:
                realized = (price - pos.avg_cost) * min(qty, pos.quantity)
                pos.realized_pnl += realized
            pos.quantity -= qty
            if pos.quantity == 0:
                pos.avg_cost = 0.0

    def _get_fill_price(self, order: Order) -> float:
        """Determine the fill price for a paper order.

        For market orders: uses the latest close price.
        For limit orders: checks if limit price is triggerable vs last close.
        """
        close = self._get_latest_price(order.symbol)
        if close <= 0:
            return 0.0

        if order.order_type == OrderType.LIMIT and order.limit_price > 0:
            if order.side == OrderSide.BUY and close <= order.limit_price:
                return order.limit_price
            elif order.side == OrderSide.SELL and close >= order.limit_price:
                return order.limit_price
            else:
                return 0.0  # Limit not triggered
        return close

    def _get_latest_price(self, symbol: str) -> float:
        """Get the most recent close price for a symbol from the data store."""
        try:
            df = self._store.read_partitioned(
                f"a_shares/daily/{symbol}",
                start_date="20200101",
                end_date="20991231",
                storage="raw",
            )
            if df is not None and not df.empty:
                # Look for close column
                if "close" in df.columns:
                    return float(df["close"].iloc[-1])
                if "Close" in df.columns:
                    return float(df["Close"].iloc[-1])
        except Exception:
            logger.debug("No price data for %s in paper broker", symbol)
        return 0.0


# ---------------------------------------------------------------------------
# Paper trading engine (higher-level runner)
# ---------------------------------------------------------------------------


@dataclass
class PaperTradingSession:
    """A paper trading session tying a strategy to a paper broker."""

    session_id: str
    strategy_id: str
    broker: PaperBroker
    started_at: str
    status: str = "running"  # running, paused, stopped
    daily_pnl: list[dict] = field(default_factory=list)

    def get_status(self) -> dict[str, Any]:
        """Return current session status summary."""
        account = self.broker.get_account()
        orders = self.broker.get_orders(limit=100)
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        return {
            "session_id": self.session_id,
            "strategy_id": self.strategy_id,
            "status": self.status,
            "started_at": self.started_at,
            "account": {
                "cash": account.cash,
                "total_value": account.total_value,
                "pnl": account.total_value - self.broker._initial_capital,
                "pnl_pct": (
                    (account.total_value / self.broker._initial_capital - 1.0) * 100
                    if self.broker._initial_capital > 0
                    else 0.0
                ),
            },
            "position_count": len(account.positions),
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "avg_cost": p.avg_cost,
                    "market_value": p.market_value,
                    "unrealized_pnl": p.unrealized_pnl,
                }
                for p in account.positions
            ],
            "order_count": len(orders),
            "filled_count": len(filled),
        }


# ---------------------------------------------------------------------------
# Paper session manager (in-memory, replace with DB for persistence)
# ---------------------------------------------------------------------------

_paper_sessions: dict[str, PaperTradingSession] = {}


def start_paper_session(
    strategy_id: str,
    initial_capital: float = 1_000_000.0,
) -> PaperTradingSession:
    """Start a new paper trading session for a strategy.

    Parameters
    ----------
    strategy_id : str
        The strategy UUID.
    initial_capital : float
        Starting capital for the paper account.

    Returns
    -------
    PaperTradingSession
    """
    broker = PaperBroker(initial_capital=initial_capital)
    broker.connect()

    session = PaperTradingSession(
        session_id=f"paper_{uuid.uuid4().hex[:8]}",
        strategy_id=strategy_id,
        broker=broker,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    _paper_sessions[session.session_id] = session

    logger.info(
        "Started paper session %s for strategy %s",
        session.session_id, strategy_id,
    )
    return session


def get_paper_session(session_id: str) -> Optional[PaperTradingSession]:
    """Retrieve a paper trading session by ID."""
    return _paper_sessions.get(session_id)


def stop_paper_session(session_id: str) -> Optional[PaperTradingSession]:
    """Stop a paper trading session."""
    session = _paper_sessions.get(session_id)
    if session:
        session.status = "stopped"
        session.broker.disconnect()
    return session
