"""
ETF Rotation Strategies (Sleeve B).

Provides three rotation approaches:
  - Momentum rotation: rank ETFs by N-day return, rotate to top K
  - Risk parity: allocate inversely proportional to volatility
  - Equal weight baseline

Also includes a backtest harness for rotation strategies.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core rotation strategy functions
# ---------------------------------------------------------------------------

def momentum_rotation(
    df: pd.DataFrame,
    lookback: int = 20,
    top_k: int = 5,
) -> dict[str, float]:
    """Rank ETFs by N-day return and assign equal weights to the top K.

    Parameters
    ----------
    df : pd.DataFrame
        Price DataFrame with columns = ETF symbols, index = dates.
        Must contain at least `lookback` rows.
    lookback : int
        Number of trading days for momentum calculation.
    top_k : int
        Number of ETFs to include in the portfolio.

    Returns
    -------
    dict[str, float]
        Target weights keyed by ETF symbol (sums to ~1.0).
        Empty dict if insufficient data.
    """
    if df.empty or len(df) < lookback or df.shape[1] == 0:
        logger.warning(
            "Momentum rotation: insufficient data (rows=%d, cols=%d, lookback=%d)",
            len(df), df.shape[1], lookback,
        )
        return {}

    # Use closing prices; fall back to the last column if 'close' not present
    if "close" in df.columns.get_level_values(0) if isinstance(df.columns, pd.MultiIndex) else False:
        prices = df.xs("close", axis=1, level=0)
    elif "close" in df.columns:
        prices = df["close"]
    else:
        # Assume single-level columns are close prices
        prices = df

    # Calculate momentum: (latest - oldest) / oldest
    momentum = (prices.iloc[-1] - prices.iloc[-lookback]) / prices.iloc[-lookback]
    momentum = momentum.dropna().replace([np.inf, -np.inf], np.nan).dropna()

    if momentum.empty:
        logger.warning("Momentum rotation: all ETFs have NaN momentum")
        return {}

    # Rank by momentum descending, select top_k
    ranked = momentum.sort_values(ascending=False)
    selected = ranked.head(min(top_k, len(ranked)))

    # Equal weight among selected
    weight = 1.0 / len(selected)
    weights = {symbol: weight for symbol in selected.index}

    logger.info(
        "Momentum rotation (lookback=%d, top_k=%d): selected %d ETFs",
        lookback, top_k, len(weights),
    )
    return weights


def risk_parity(
    df: pd.DataFrame,
    lookback: int = 60,
) -> dict[str, float]:
    """Allocate weights inversely proportional to historical volatility.

    Each ETF's weight ∝ 1 / σ_i, then normalised to sum to 1.

    Parameters
    ----------
    df : pd.DataFrame
        Price DataFrame with columns = ETF symbols, index = dates.
    lookback : int
        Number of trading days for volatility calculation.

    Returns
    -------
    dict[str, float]
        Target weights keyed by ETF symbol (sums to ~1.0).
    """
    if df.empty or len(df) < max(lookback, 2) or df.shape[1] == 0:
        logger.warning(
            "Risk parity: insufficient data (rows=%d, cols=%d, lookback=%d)",
            len(df), df.shape[1], lookback,
        )
        return {}

    # Extract close prices
    if isinstance(df.columns, pd.MultiIndex) and "close" in df.columns.get_level_values(0):
        prices = df.xs("close", axis=1, level=0)
    elif "close" in df.columns:
        prices = df["close"]
    else:
        prices = df

    if prices.empty:
        return {}

    # Calculate daily returns
    returns = prices.pct_change().iloc[-lookback:]

    # Annualised volatility (approximate)
    vol = returns.std() * np.sqrt(252)
    vol = vol.replace(0, np.nan).dropna()

    if vol.empty:
        logger.warning("Risk parity: all ETFs have zero/NaN volatility")
        return {}

    # Inverse volatility weights
    inv_vol = 1.0 / vol
    total = inv_vol.sum()
    if total == 0:
        return {}
    weights = (inv_vol / total).to_dict()

    logger.info(
        "Risk parity (lookback=%d): %d ETFs, max weight=%.3f, min weight=%.3f",
        lookback, len(weights), max(weights.values()), min(weights.values()),
    )
    return weights


def equal_weight(df: pd.DataFrame) -> dict[str, float]:
    """Equal weight across all ETFs in the DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Price DataFrame with columns = ETF symbols.

    Returns
    -------
    dict[str, float]
        Equal weights for all ETFs.
    """
    if df.shape[1] == 0:
        logger.warning("Equal weight: no ETF columns in DataFrame")
        return {}

    # Determine symbols from columns
    symbols = df.columns.tolist()
    if isinstance(df.columns, pd.MultiIndex):
        # Use top-level ETF codes if multi-index
        symbols = df.columns.get_level_values(0).unique().tolist()

    n = len(symbols)
    if n == 0:
        return {}
    weight = 1.0 / n
    weights = {sym: weight for sym in symbols}

    logger.info("Equal weight: %d ETFs allocated", n)
    return weights


# ---------------------------------------------------------------------------
# Backtest harness
# ---------------------------------------------------------------------------

def backtest_rotation(
    df: pd.DataFrame,
    strategy_fn: Callable[[pd.DataFrame], dict[str, float]],
    rebalance_freq: int = 20,
    initial_capital: float = 1_000_000.0,
) -> dict:
    """Backtest a rotation strategy with periodic rebalancing.

    Parameters
    ----------
    df : pd.DataFrame
        Price DataFrame (T rows × N ETF symbols). Must be MultiIndex with
        'close' at level 1, or single-level columns as close prices.
    strategy_fn : callable
        Function that takes a window of prices (pd.DataFrame) and returns
        dict of symbol → target weight.
    rebalance_freq : int
        Number of trading days between rebalances.
    initial_capital : float
        Starting capital.

    Returns
    -------
    dict
        {
            "equity_curve": list[{"date": str, "value": float}],
            "metrics": {
                "total_return": float,
                "annual_return": float,
                "sharpe": float,
                "max_drawdown": float,
                "volatility": float,
            },
            "final_weights": dict[str, float],
        }
    """
    # Extract close prices
    if isinstance(df.columns, pd.MultiIndex):
        if "close" in df.columns.get_level_values(1):
            close = df.xs("close", axis=1, level=1)
        elif "close" in df.columns.get_level_values(0):
            close = df.xs("close", axis=1, level=0)
        else:
            close = df
    elif "close" in df.columns:
        close = df[["close"]]
    else:
        close = df

    if close.empty:
        return {
            "equity_curve": [],
            "metrics": {"total_return": 0, "annual_return": 0, "sharpe": 0,
                        "max_drawdown": 0, "volatility": 0},
            "final_weights": {},
        }

    # Work with single-level columns (symbols)
    if isinstance(close.columns, pd.MultiIndex):
        symbols = close.columns.get_level_values(0).unique().tolist()
    else:
        symbols = close.columns.tolist()

    n_dates = len(close)
    if n_dates <= rebalance_freq:
        logger.warning("backtest_rotation: not enough dates (%d) for rebalance_freq=%d",
                       n_dates, rebalance_freq)
        rebalance_freq = n_dates

    dates = close.index.tolist()
    equity = [initial_capital]
    peak = initial_capital
    max_drawdown = 0.0
    daily_returns: list[float] = []

    current_weights: dict[str, float] = {}
    current_cash = initial_capital

    for i in range(n_dates):
        date = dates[i]

        # Rebalance on schedule
        if i % rebalance_freq == 0:
            window = close.iloc[max(0, i - 252):i + 1]
            if not window.empty and window.shape[1] > 0:
                try:
                    target_weights = strategy_fn(window)
                except Exception as e:
                    logger.warning("Strategy fn failed at date %s: %s", date, e)
                    target_weights = {}
                if target_weights:
                    # Liquidate: value all holdings at current close
                    portfolio_value = current_cash
                    for sym, w in current_weights.items():
                        if sym in symbols and sym in close.columns:
                            sym_price = close.iloc[i][sym]
                            if not pd.isna(sym_price):
                                portfolio_value += w * current_cash * (
                                    sym_price / close.iloc[i - 1][sym]
                                    if i > 0 and sym in close.iloc[i - 1].index else 1.0
                                )
                    # Reallocate
                    current_weights = target_weights
                    current_cash = portfolio_value

        # Mark-to-market
        portfolio_value = current_cash
        for sym, w in current_weights.items():
            if sym in close.columns:
                sym_close = close.iloc[i][sym]
                if not pd.isna(sym_close):
                    portfolio_value += w * current_cash
                    # Adjust: w * cash at last rebalance * price ratio
                    pass

        # Simplified mark-to-market using price ratios
        if i > 0 and equity:
            prev_equity = equity[-1]
            # Weighted return across holdings
            if current_weights:
                port_return = 0.0
                for sym, w in current_weights.items():
                    if sym in close.columns:
                        prev_close = close.iloc[i - 1][sym]
                        curr_close = close.iloc[i][sym]
                        if not pd.isna(prev_close) and not pd.isna(curr_close) and prev_close > 0:
                            port_return += w * (curr_close / prev_close - 1)
                # Cash portion (if any unallocated)
                cash_weight = max(0, 1.0 - sum(current_weights.values()))
                portfolio_value = prev_equity * (1.0 + port_return)
            else:
                portfolio_value = prev_equity
        else:
            portfolio_value = initial_capital

        equity.append(float(portfolio_value))
        if portfolio_value > peak:
            peak = portfolio_value
        drawdown = (portfolio_value - peak) / peak if peak > 0 else 0
        if drawdown < max_drawdown:
            max_drawdown = drawdown

        if len(equity) >= 2:
            daily_ret = (equity[-1] - equity[-2]) / equity[-2]
            daily_returns.append(daily_ret)

    # Compute metrics
    total_return = (equity[-1] - initial_capital) / initial_capital
    n_years = n_dates / 252.0 if n_dates > 0 else 1.0
    annual_return = (1 + total_return) ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0

    daily_ret_series = pd.Series(daily_returns)
    volatility = float(daily_ret_series.std() * np.sqrt(252)) if len(daily_returns) > 1 else 0.0
    sharpe = float((daily_ret_series.mean() / daily_ret_series.std() * np.sqrt(252))
                   if len(daily_returns) > 1 and daily_ret_series.std() > 0 else 0.0)

    metrics = {
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_drawdown, 6),
        "volatility": round(volatility, 6),
    }

    equity_curve = [
        {"date": str(dates[i]), "value": round(equity[i], 2)}
        for i in range(n_dates)
    ]

    logger.info(
        "Rotation backtest: total_return=%.2f%%, sharpe=%.2f, max_dd=%.2f%%",
        total_return * 100, sharpe, max_drawdown * 100,
    )
    return {
        "equity_curve": equity_curve,
        "metrics": metrics,
        "final_weights": current_weights,
    }
