"""
Backtest configuration — constants, commission schedules, and default parameters.

All values can be overridden at runtime via the API.
"""

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# A-share commission schedule (simplified)
# ---------------------------------------------------------------------------
# Stamp duty: 0.05% on sell only (as of 2024 reduction)
# Broker commission: 0.03% buy + sell
# Transfer fee: 0.002% both sides
# Total: buy ≈ 0.032%, sell ≈ 0.082%  (rounding to conservative)
# We use 0.1% buy + 0.15% sell as a conservative / simplified schedule
# that covers most retail scenarios.

A_SHARE_COMMISSION_BUY = 0.001   # 0.10%
A_SHARE_COMMISSION_SELL = 0.0015 # 0.15%
A_SHARE_MIN_COMMISSION = 5.0     # min 5 RMB per trade
A_SHARE_SLIPPAGE = 0.001         # 0.10% slippage per side

# ---------------------------------------------------------------------------
# Default backtest parameters
# ---------------------------------------------------------------------------


@dataclass
class BacktestConfig:
    """Backtest configuration."""

    # Date range
    start_date: str = "2020-01-01"
    end_date: str = "2024-12-31"

    # Universe: list of ts_codes
    symbols: list[str] = field(default_factory=list)

    # Rebalance frequency in trading days (e.g. 20 = monthly)
    rebalance_days: int = 20

    # Position sizing
    sizing: str = "equal_weight"  # "equal_weight" or "inverse_volatility"

    # Max number of holdings
    max_holdings: int = 20

    # Commission schedule
    commission_buy: float = A_SHARE_COMMISSION_BUY
    commission_sell: float = A_SHARE_COMMISSION_SELL
    min_commission: float = A_SHARE_MIN_COMMISSION
    slippage: float = A_SHARE_SLIPPAGE

    # Initial capital
    initial_capital: float = 1_000_000.0

    # Cash reserve ratio (kept in cash, not invested)
    cash_reserve: float = 0.0

    # Stop-loss per position (0 = disabled)
    stop_loss: float = 0.0  # e.g. 0.15 = 15%

    # Signal source: 'composite_rank' or 'predefined'
    signal_source: str = "composite_rank"

    # Predefined signal list (when signal_source='predefined')
    signals: list[dict] = field(default_factory=list)

    # Factor weights (when signal_source='composite_rank')
    factor_weights: dict[str, float] = field(default_factory=dict)

    # Factor data (pre-computed factor panel)
    factor_data: dict = field(default_factory=dict)


# Trading day calendar (simplified — production should use actual calendar)
# This is a minimal set of Chinese holidays; in production load from a file.
_CN_HOLIDAYS = {
    "2020-01-01", "2020-01-24", "2020-01-27", "2020-01-28", "2020-01-29", "2020-01-30",
    "2020-04-06", "2020-05-01", "2020-05-04", "2020-05-05",
    "2020-06-25", "2020-06-26", "2020-10-01", "2020-10-02", "2020-10-05", "2020-10-06",
    "2020-10-07", "2020-10-08",
    "2021-01-01", "2021-02-11", "2021-02-12", "2021-02-15", "2021-02-16", "2021-02-17",
    "2021-04-05", "2021-05-03", "2021-05-04", "2021-05-05",
    "2021-06-14", "2021-09-20", "2021-09-21", "2021-10-01", "2021-10-04", "2021-10-05",
    "2021-10-06", "2021-10-07",
    "2022-01-03", "2022-01-31", "2022-02-01", "2022-02-02", "2022-02-03", "2022-02-04",
    "2022-04-04", "2022-04-05", "2022-05-02", "2022-05-03", "2022-05-04",
    "2022-06-03", "2022-09-12", "2022-10-03", "2022-10-04", "2022-10-05",
    "2022-10-06", "2022-10-07",
    "2023-01-02", "2023-01-23", "2023-01-24", "2023-01-25", "2023-01-26", "2023-01-27",
    "2023-04-05", "2023-05-01", "2023-06-22", "2023-06-23",
    "2023-09-29", "2023-10-02", "2023-10-03", "2023-10-04", "2023-10-05", "2023-10-06",
    "2024-01-01", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14",
    "2024-02-15", "2024-02-16",
    "2024-04-04", "2024-04-05", "2024-05-01", "2024-05-02", "2024-05-03",
    "2024-06-10", "2024-09-16", "2024-09-17",
    "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
}


def is_trading_day(date_str: str) -> bool:
    """Check if a date string (YYYY-MM-DD) is a trading day."""
    from datetime import date as dt_date

    try:
        d = dt_date.fromisoformat(date_str)
    except ValueError:
        return False
    # Weekday 0=Mon ... 4=Fri, 5=Sat, 6=Sun
    if d.weekday() >= 5:
        return False
    if date_str in _CN_HOLIDAYS:
        return False
    return True


def next_trading_day(date_str: str, step: int = 1) -> str:
    """Return the nth next trading day (step positive = forward)."""
    from datetime import date as dt_date, timedelta

    d = dt_date.fromisoformat(date_str)
    count = 0
    direction = 1 if step > 0 else -1
    while count < abs(step):
        d += timedelta(days=direction)
        if is_trading_day(str(d)):
            count += 1
    return str(d)


def prev_trading_day(date_str: str, step: int = 1) -> str:
    """Return the nth previous trading day."""
    return next_trading_day(date_str, step=-step)
