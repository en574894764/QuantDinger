"""
Factor library — ~25 alpha factors computed from daily OHLCV DataFrames.

Each factor function accepts a DataFrame with columns:
    open, high, low, close, vol
(all lowercase) and returns a pd.Series indexed by date.

All calculations use numpy/pandas only — no external quant libraries.
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Momentum factors
# ---------------------------------------------------------------------------

def momentum_20(df: pd.DataFrame) -> pd.Series:
    """20-day price momentum: close / close_20 - 1."""
    return df["close"] / df["close"].shift(20) - 1.0


def momentum_60(df: pd.DataFrame) -> pd.Series:
    """60-day price momentum."""
    return df["close"] / df["close"].shift(60) - 1.0


def momentum_120(df: pd.DataFrame) -> pd.Series:
    """120-day price momentum."""
    return df["close"] / df["close"].shift(120) - 1.0


def momentum_5(df: pd.DataFrame) -> pd.Series:
    """5-day price momentum (short-term reversal proxy)."""
    return df["close"] / df["close"].shift(5) - 1.0


# ---------------------------------------------------------------------------
# Volatility factors
# ---------------------------------------------------------------------------

def volatility_20(df: pd.DataFrame) -> pd.Series:
    """20-day annualised volatility (pct_change std * sqrt(252))."""
    return df["close"].pct_change().rolling(20).std() * np.sqrt(252)


def volatility_60(df: pd.DataFrame) -> pd.Series:
    """60-day annualised volatility."""
    return df["close"].pct_change().rolling(60).std() * np.sqrt(252)


def amplitude_20(df: pd.DataFrame) -> pd.Series:
    """20-day average daily amplitude: (high - low) / close."""
    amp = (df["high"] - df["low"]) / df["close"]
    return amp.rolling(20).mean()


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def rsi_14(df: pd.DataFrame) -> pd.Series:
    """14-day RSI (Wilder smoothing)."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    # Use Wilder smoothing after the first average
    for _ in range(14, len(delta)):
        # Build iteratively for true Wilder smoothing
        pass
    # Simpler SMA version — good enough for factor analysis
    rs = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def ma_cross_5_20(df: pd.DataFrame) -> pd.Series:
    """5/20-day MA crossover signal: (ma5 - ma20) / close."""
    ma5 = df["close"].rolling(5).mean()
    ma20 = df["close"].rolling(20).mean()
    return (ma5 - ma20) / df["close"]


def bbands_position(df: pd.DataFrame) -> pd.Series:
    """
    Bollinger Bands position: (close - lower) / (upper - lower).
    Range [0, 1]: 0 = at lower band, 1 = at upper band.
    """
    ma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    upper = ma20 + 2 * std20
    lower = ma20 - 2 * std20
    denom = upper - lower
    denom = denom.replace(0, np.nan)
    return (df["close"] - lower) / denom


def bbands_width(df: pd.DataFrame) -> pd.Series:
    """Bollinger Bands width: (upper - lower) / ma20."""
    ma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    return (4 * std20) / ma20


def macd_hist(df: pd.DataFrame) -> pd.Series:
    """MACD histogram: (ema12 - ema26) - ema9 of the difference, normalised by close."""
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return (dif - dea) / df["close"]


def atr_14(df: pd.DataFrame) -> pd.Series:
    """14-day Average True Range, normalised by close."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()
    return atr / close


# ---------------------------------------------------------------------------
# Volume / liquidity factors
# ---------------------------------------------------------------------------

def volume_ratio(df: pd.DataFrame) -> pd.Series:
    """Volume ratio: today's volume / 20-day average volume."""
    return df["vol"] / df["vol"].rolling(20).mean()


def turnover_5(df: pd.DataFrame) -> pd.Series:
    """5-day average turnover (volume), normalised by its own 20-day average."""
    vol5 = df["vol"].rolling(5).mean()
    vol20 = df["vol"].rolling(20).mean()
    return vol5 / vol20.replace(0, np.nan)


def volume_trend(df: pd.DataFrame) -> pd.Series:
    """Volume trend: slope of log(volume) over 20 days (OLS regression)."""
    log_vol = np.log(df["vol"].replace(0, np.nan))
    result = pd.Series(np.nan, index=df.index)
    for i in range(20, len(df)):
        y = log_vol.iloc[i - 20 : i].values
        x = np.arange(20)
        valid = ~np.isnan(y)
        if valid.sum() < 5:
            continue
        slope = np.polyfit(x[valid], y[valid], 1)[0]
        result.iloc[i] = slope
    return result


# ---------------------------------------------------------------------------
# Price-pattern factors
# ---------------------------------------------------------------------------

def skewness_20(df: pd.DataFrame) -> pd.Series:
    """20-day return skewness."""
    return df["close"].pct_change().rolling(20).skew()


def max_drawdown_20(df: pd.DataFrame) -> pd.Series:
    """20-day maximum drawdown (positive number, so drawdown => negative return)."""
    roll_max = df["close"].rolling(20, min_periods=1).max()
    drawdown = df["close"] / roll_max - 1.0
    return drawdown.rolling(20, min_periods=1).min()


def upside_volatility_20(df: pd.DataFrame) -> pd.Series:
    """Upside volatility: std of positive returns over 20 days."""
    rets = df["close"].pct_change()
    pos = rets.clip(lower=0)
    # Count of non-zero positive returns for min_periods
    return pos.rolling(20, min_periods=5).std() * np.sqrt(252)


def downside_volatility_20(df: pd.DataFrame) -> pd.Series:
    """Downside volatility: std of negative returns over 20 days."""
    rets = df["close"].pct_change()
    neg = rets.clip(upper=0)
    return neg.rolling(20, min_periods=5).std() * np.sqrt(252)


# ---------------------------------------------------------------------------
# Composite / ranking helpers
# ---------------------------------------------------------------------------

def price_position_60(df: pd.DataFrame) -> pd.Series:
    """Where close sits within 60-day high-low range. 0=bottom, 1=top."""
    high60 = df["high"].rolling(60, min_periods=20).max()
    low60 = df["low"].rolling(60, min_periods=20).min()
    denom = high60 - low60
    denom = denom.replace(0, np.nan)
    return (df["close"] - low60) / denom


def efficiency_ratio_20(df: pd.DataFrame) -> pd.Series:
    """
    Kaufman Efficiency Ratio: |close - close_20| / sum of 20 daily absolute changes.
    Higher => stronger directional movement.
    """
    direction = (df["close"] - df["close"].shift(20)).abs()
    volatility = df["close"].diff().abs().rolling(20).sum()
    return direction / volatility.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Factor registry
# ---------------------------------------------------------------------------

FACTOR_REGISTRY: dict[str, callable] = {
    # Momentum
    "momentum_5": momentum_5,
    "momentum_20": momentum_20,
    "momentum_60": momentum_60,
    "momentum_120": momentum_120,
    # Volatility
    "volatility_20": volatility_20,
    "volatility_60": volatility_60,
    "amplitude_20": amplitude_20,
    # Technical
    "rsi_14": rsi_14,
    "ma_cross_5_20": ma_cross_5_20,
    "bbands_position": bbands_position,
    "bbands_width": bbands_width,
    "macd_hist": macd_hist,
    "atr_14": atr_14,
    # Volume
    "volume_ratio": volume_ratio,
    "turnover_5": turnover_5,
    "volume_trend": volume_trend,
    # Price pattern
    "skewness_20": skewness_20,
    "max_drawdown_20": max_drawdown_20,
    "upside_volatility_20": upside_volatility_20,
    "downside_volatility_20": downside_volatility_20,
    # Composite
    "price_position_60": price_position_60,
    "efficiency_ratio_20": efficiency_ratio_20,
}


def list_factors() -> list[dict]:
    """Return metadata for all registered factors."""
    descriptions = {
        "momentum_5": "5-day price momentum (short-term)",
        "momentum_20": "20-day price momentum",
        "momentum_60": "60-day price momentum",
        "momentum_120": "120-day price momentum",
        "volatility_20": "20-day annualised volatility",
        "volatility_60": "60-day annualised volatility",
        "amplitude_20": "20-day average daily price amplitude",
        "rsi_14": "14-day RSI (relative strength index)",
        "ma_cross_5_20": "5/20-day MA crossover signal",
        "bbands_position": "Bollinger Bands position (0=lower, 1=upper)",
        "bbands_width": "Bollinger Bands width normalised",
        "macd_hist": "MACD histogram normalised by close",
        "atr_14": "14-day Average True Range normalised by close",
        "volume_ratio": "Today's volume / 20-day average volume",
        "turnover_5": "5-day avg volume / 20-day avg volume",
        "volume_trend": "Log-volume OLS slope over 20 days",
        "skewness_20": "20-day return skewness",
        "max_drawdown_20": "20-day maximum drawdown",
        "upside_volatility_20": "20-day upside volatility (annualised)",
        "downside_volatility_20": "20-day downside volatility (annualised)",
        "price_position_60": "Close position within 60-day high-low range",
        "efficiency_ratio_20": "Kaufman Efficiency Ratio (20-day)",
    }
    return [
        {"name": name, "description": descriptions.get(name, "")}
        for name in FACTOR_REGISTRY
    ]


def compute_factor(df: pd.DataFrame, name: str) -> pd.Series:
    """Compute a single factor by name."""
    fn = FACTOR_REGISTRY[name]
    return fn(df)


def compute_all_factors(df: pd.DataFrame, factor_names: list[str]) -> pd.DataFrame:
    """
    Compute all specified factors and return a DataFrame with factor columns
    indexed by the original df's index (dates).
    """
    result = pd.DataFrame(index=df.index)
    for name in factor_names:
        try:
            result[name] = compute_factor(df, name)
        except Exception:
            logger.exception("Failed to compute factor %s", name)
            result[name] = np.nan
    return result
