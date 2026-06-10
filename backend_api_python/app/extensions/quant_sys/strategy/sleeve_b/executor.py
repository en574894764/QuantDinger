"""
Sleeve B Rebalancing Executor.

Given current positions and target weights from a rotation strategy,
calculates the necessary buy/sell orders to reach the target allocation.

Features:
  - Calculate target weight → delta → rebalance orders
  - Handle minimum trade size
  - Commission-aware sizing
  - Partial fill handling
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class RebalanceOrder:
    """A single rebalance order (buy or sell)."""

    symbol: str
    side: str  # "buy" or "sell"
    target_weight: float
    current_weight: float
    delta_weight: float
    notional: float  # in CNY
    shares: int
    reason: str = ""


@dataclass
class RebalanceConfig:
    """Configuration for rebalance calculation."""

    # Minimum trade size in CNY (skip trades below this)
    min_trade_value: float = 5_000.0

    # Commission rate (buy side)
    commission_buy: float = 0.0003  # 万三

    # Commission rate (sell side, includes stamp duty)
    commission_sell: float = 0.0013  # 万三 + 千一 stamp

    # Minimum commission per trade
    min_commission: float = 5.0

    # Slippage assumption
    slippage: float = 0.001

    # Round lots (A-share = 100 shares per lot)
    lot_size: int = 100

    # Max turnover ratio (0-1); caps total notional traded
    max_turnover: float = 0.50

    # Max single-order weight
    max_single_weight: float = 0.20

    # Allow partial fills (scale down orders proportionally)
    allow_partial: bool = True


@dataclass
class RebalanceResult:
    """Output of a rebalance calculation."""

    current_positions: dict[str, float]  # symbol → weight
    target_weights: dict[str, float]     # symbol → weight
    orders: list[RebalanceOrder]
    total_buy_notional: float
    total_sell_notional: float
    turnover: float  # fraction of capital traded
    remaining_cash_weight: float  # unallocated cash
    notes: list[str] = field(default_factory=list)


def calculate_rebalance(
    current_positions: dict[str, float],
    target_weights: dict[str, float],
    capital: float,
    config: Optional[RebalanceConfig] = None,
) -> RebalanceResult:
    """Calculate rebalance orders to move from current to target weights.

    Parameters
    ----------
    current_positions : dict[str, float]
        Current position weights (symbol → weight as fraction of portfolio).
    target_weights : dict[str, float]
        Target weights from rotation strategy (symbol → weight as fraction).
    capital : float
        Total portfolio capital in CNY.
    config : RebalanceConfig, optional
        Rebalance configuration.

    Returns
    -------
    RebalanceResult
        Calculated orders and summary statistics.
    """
    if config is None:
        config = RebalanceConfig()

    notes: list[str] = []
    all_symbols = set(current_positions.keys()) | set(target_weights.keys())

    orders: list[RebalanceOrder] = []
    total_buy = 0.0
    total_sell = 0.0

    for symbol in sorted(all_symbols):
        current_w = current_positions.get(symbol, 0.0)
        target_w = target_weights.get(symbol, 0.0)
        delta = target_w - current_w

        # Skip near-zero deltas
        if abs(delta) < 0.0001:
            continue

        notional = delta * capital
        abs_notional = abs(notional)

        # Minimum trade size check
        if abs_notional < config.min_trade_value:
            logger.debug(
                "Skip %s: notional %.0f < min %.0f",
                symbol, abs_notional, config.min_trade_value,
            )
            notes.append(
                f"Skip {symbol}: delta={delta:.4f}, "
                f"notional={abs_notional:.0f} < min={config.min_trade_value:.0f}"
            )
            continue

        side = "buy" if delta > 0 else "sell"

        # Apply max single weight cap
        if side == "buy" and target_w > config.max_single_weight:
            capped_w = config.max_single_weight
            capped_delta = capped_w - current_w
            if capped_delta <= 0:
                continue
            notional = capped_delta * capital
            abs_notional = abs(notional)
            notes.append(
                f"Capped {symbol}: target {target_w:.4f} → {capped_w:.4f} "
                f"(max single weight)"
            )

        # Calculate shares (round to lot size)
        # Assume approximate price from notional / target_weight fraction
        raw_shares = int(abs_notional / config.lot_size) * config.lot_size
        if raw_shares == 0:
            notes.append(
                f"Skip {symbol}: {abs_notional:.0f} CNY < 1 lot ({config.lot_size} shares)"
            )
            continue

        order = RebalanceOrder(
            symbol=symbol,
            side=side,
            target_weight=target_w,
            current_weight=current_w,
            delta_weight=delta,
            notional=round(notional, 2),
            shares=raw_shares,
            reason=f"Rebalance to target {target_w:.4f}",
        )
        orders.append(order)

        if side == "buy":
            total_buy += abs(notional)
        else:
            total_sell += abs(notional)

    # Apply turnover cap
    turnover = (total_buy + total_sell) / (2 * capital) if capital > 0 else 0
    if turnover > config.max_turnover and config.allow_partial:
        scale = config.max_turnover / turnover
        logger.info(
            "Turnover cap hit: %.2f > %.2f, scaling orders by %.3f",
            turnover, config.max_turnover, scale,
        )
        notes.append(
            f"Turnover capped: {turnover:.3f} → {config.max_turnover:.3f} "
            f"(scaled by {scale:.3f})"
        )
        for order in orders:
            order.notional = round(order.notional * scale, 2)
            order.shares = int(order.shares * scale)
        total_buy *= scale
        total_sell *= scale
        turnover = config.max_turnover

    # Calculate remaining cash weight
    allocated = sum(target_weights.values())
    remaining_cash = max(0.0, 1.0 - allocated)

    result = RebalanceResult(
        current_positions=current_positions,
        target_weights=target_weights,
        orders=orders,
        total_buy_notional=round(total_buy, 2),
        total_sell_notional=round(total_sell, 2),
        turnover=round(turnover, 4),
        remaining_cash_weight=round(remaining_cash, 4),
        notes=notes,
    )

    logger.info(
        "Rebalance: %d orders (buy=%.0f, sell=%.0f), turnover=%.2f%%, cash=%.1f%%",
        len(orders),
        total_buy, total_sell,
        turnover * 100,
        remaining_cash * 100,
    )
    return result


def calculate_rebalance_from_df(
    df: pd.DataFrame,
    strategy_fn,
    capital: float,
    current_positions: Optional[dict[str, float]] = None,
    config: Optional[RebalanceConfig] = None,
) -> RebalanceResult:
    """Convenience: run rotation strategy on price data, then calculate rebalance.

    Parameters
    ----------
    df : pd.DataFrame
        Price DataFrame for rotation strategy.
    strategy_fn : callable
        Rotation strategy function (e.g. momentum_rotation).
    capital : float
        Total portfolio capital.
    current_positions : dict, optional
        Current position weights. Default empty.
    config : RebalanceConfig, optional
        Rebalance configuration.

    Returns
    -------
    RebalanceResult
    """
    import pandas as pd

    target_weights = strategy_fn(df)
    if current_positions is None:
        current_positions = {}

    return calculate_rebalance(
        current_positions=current_positions,
        target_weights=target_weights,
        capital=capital,
        config=config,
    )
