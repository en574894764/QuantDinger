"""
Signal generator — transforms factor values into actionable buy/sell signals.

Pipeline:
  1. Receive a dated panel of factor values (symbols x factors per date).
  2. Normalise / rank each factor cross-sectionally.
  3. Combine into a composite score via configurable weights.
  4. Threshold the composite into buy / sell / hold signals.
  5. Return a list of signal dicts suitable for downstream execution.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SignalConfig:
    """Configuration for signal generation."""

    # Factor weights: dict of factor_name -> weight (positive = long bias, negative = short)
    factor_weights: dict[str, float] = field(default_factory=dict)

    # Top N stocks to signal as buy (ranked by composite score)
    top_n_buy: int = 20

    # Bottom N stocks to signal as sell
    top_n_sell: int = 0

    # Minimum composite score (z-score) to generate a buy signal
    buy_threshold: float = 0.5

    # Maximum composite score (z-score) to generate a sell signal
    sell_threshold: float = -0.5

    # Sleeve label for the signals
    sleeve: str = "A"

    # Direction: "long_only", "short_only", "long_short"
    direction: str = "long_only"


def _cross_sectional_zscore(series: pd.Series) -> pd.Series:
    """Standardise a cross-section to mean 0, std 1."""
    mean = series.mean()
    std = series.std()
    if std == 0 or pd.isna(std):
        return pd.Series(0.0, index=series.index)
    return (series - mean) / std


def _rank_percentile(series: pd.Series) -> pd.Series:
    """Rank cross-sectionally, returning percentiles [0, 1]."""
    return series.rank(pct=True)


def generate_signals(
    factor_panel: dict[str, pd.DataFrame],
    config: SignalConfig,
    date: Optional[str] = None,
) -> list[dict]:
    """
    Generate buy/sell signals from a factor panel.

    Parameters
    ----------
    factor_panel : dict[str, pd.DataFrame]
        key = factor_name, value = DataFrame(T x N).  Rows=dates, cols=symbols.
        All DataFrames must share the same index and columns.
    config : SignalConfig
        Generation parameters.
    date : str or None
        Date to generate signals for (must be in the index). If None, uses the
        last row (most recent date).

    Returns
    -------
    list[dict]: each dict has keys:
        ts_code, signal_type, confidence, composite_score, factor_values, date
    """
    if not factor_panel:
        logger.warning("Empty factor panel — no signals generated")
        return []

    # Determine the target date
    target_date = date
    if target_date is None:
        # Use the last date present in all factor DataFrames
        common_index = None
        for fdf in factor_panel.values():
            if common_index is None:
                common_index = fdf.index
            else:
                common_index = common_index.intersection(fdf.index)
        if common_index is None or len(common_index) == 0:
            logger.warning("No common dates in factor panel")
            return []
        target_date = str(common_index[-1])

    # Build composite score for the target date
    composite = None
    symbol_set = None

    for fname, weight in config.factor_weights.items():
        if fname not in factor_panel:
            logger.warning("Factor %s not found in panel — skipping", fname)
            continue

        fdf = factor_panel[fname]
        if target_date not in fdf.index:
            logger.warning("Date %s not in factor %s — skipping", target_date, fname)
            continue

        raw_values = fdf.loc[target_date].dropna()
        if symbol_set is None:
            symbol_set = set(raw_values.index)
        else:
            symbol_set = symbol_set.intersection(set(raw_values.index))

        # Cross-sectional z-score normalisation
        zs = _cross_sectional_zscore(raw_values)

        if composite is None:
            composite = zs * weight
        else:
            composite = composite.add(zs * weight, fill_value=0)

    if composite is None or symbol_set is None or len(symbol_set) == 0:
        logger.warning("No valid composite scores at date %s", target_date)
        return []

    composite = composite.dropna()
    if len(composite) == 0:
        return []

    # Rank and generate signals
    ranked = composite.sort_values(ascending=False)

    signals = []
    n_buy = min(config.top_n_buy, len(ranked))
    n_sell = min(config.top_n_sell, len(ranked))

    # Build factor-values lookup for each symbol
    factor_snap = {}
    for fname in config.factor_weights:
        if fname in factor_panel and target_date in factor_panel[fname].index:
            frow = factor_panel[fname].loc[target_date]
            factor_snap[fname] = frow

    for i, (symbol, score) in enumerate(ranked.items()):
        if config.direction in ("long_only", "long_short") and i < n_buy:
            signal_type = "buy"
        elif config.direction in ("short_only", "long_short") and i >= len(ranked) - n_sell:
            signal_type = "sell"
        elif score >= config.buy_threshold and config.direction in ("long_only", "long_short"):
            signal_type = "buy"
        elif score <= config.sell_threshold and config.direction in ("short_only", "long_short"):
            signal_type = "sell"
        else:
            continue  # no signal

        # Gather factor values
        fv = {}
        for fname, frow in factor_snap.items():
            v = frow.get(symbol)
            fv[fname] = round(float(v), 6) if not (v is None or (isinstance(v, float) and np.isnan(v))) else None

        signals.append({
            "ts_code": str(symbol),
            "signal_type": signal_type,
            "confidence": round(float(score), 4),
            "composite_score": round(float(score), 6),
            "factor_values": fv,
            "date": str(target_date),
            "sleeve": config.sleeve,
        })

    # Sort by absolute confidence descending
    signals.sort(key=lambda s: abs(s["confidence"]), reverse=True)

    logger.info(
        "Generated %d signals for date %s (buy=%d, sell=%d)",
        len(signals),
        target_date,
        sum(1 for s in signals if s["signal_type"] == "buy"),
        sum(1 for s in signals if s["signal_type"] == "sell"),
    )
    return signals


def generate_signals_rolling(
    factor_panel: dict[str, pd.DataFrame],
    config: SignalConfig,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """
    Generate signals for every date in the factor panel range.

    Returns a flat list of signal dicts across all dates.
    """
    # Find common date range
    common_dates = None
    for fdf in factor_panel.values():
        if common_dates is None:
            common_dates = set(fdf.index)
        else:
            common_dates = common_dates.intersection(set(fdf.index))

    if common_dates is None:
        return []

    all_dates = sorted(common_dates)

    if start_date:
        all_dates = [d for d in all_dates if str(d) >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if str(d) <= end_date]

    all_signals = []
    for d in all_dates:
        sigs = generate_signals(factor_panel, config, date=str(d))
        all_signals.extend(sigs)

    logger.info(
        "Generated %d signals across %d dates (range: %s → %s)",
        len(all_signals),
        len(all_dates),
        all_dates[0] if all_dates else "N/A",
        all_dates[-1] if all_dates else "N/A",
    )
    return all_signals
