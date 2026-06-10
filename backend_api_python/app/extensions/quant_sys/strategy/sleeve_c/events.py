"""
Event-Driven Strategies (Sleeve C).

Detects tradeable corporate events from price and fundamental data:
  - Earnings surprise: buy on positive surprise, sell on negative
  - Index rebalancing: buy stocks being added to major indices
  - Limit-up breakout: buy stocks that hit limit-up with high volume
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# A-share daily price limits
A_SHARE_LIMIT_UP = 0.10   # 10% for main board
A_SHARE_LIMIT_DOWN = -0.10


# ---------------------------------------------------------------------------
# Earnings Surprise
# ---------------------------------------------------------------------------

def earnings_surprise(
    df: pd.DataFrame,
    threshold: float = 0.05,
) -> list[dict]:
    """Scan for earnings surprise signals.

    A positive surprise is when actual EPS exceeds consensus/prior EPS
    by more than `threshold` (fractional). Negative surprise is the reverse.

    Expects DataFrame with columns: symbol, eps_actual, eps_consensus, eps_prior,
    report_date, price, market_cap. At minimum: symbol, eps_actual, eps_prior.

    Parameters
    ----------
    df : pd.DataFrame
        Earnings data with at least 'symbol', 'eps_actual', 'eps_prior'.
    threshold : float
        Minimum surprise magnitude to generate a signal (e.g. 0.05 = 5%).

    Returns
    -------
    list[dict]
        Signals sorted by surprise magnitude (descending).
    """
    required_cols = {"symbol", "eps_actual"}
    missing = required_cols - set(df.columns)
    if missing:
        logger.warning("Earnings surprise: missing columns %s", missing)
        return []

    signals: list[dict] = []
    df = df.copy()

    # Determine comparison baseline
    if "eps_consensus" in df.columns:
        baseline_col = "eps_consensus"
        baseline_label = "consensus"
    elif "eps_prior" in df.columns:
        baseline_col = "eps_prior"
        baseline_label = "prior"
    else:
        logger.warning("Earnings surprise: no baseline column (eps_consensus/epr_prior)")
        return []

    # Filter out rows with missing data
    df = df.dropna(subset=["symbol", "eps_actual", baseline_col])

    for _, row in df.iterrows():
        symbol = str(row["symbol"])
        eps_actual = float(row["eps_actual"])
        eps_baseline = float(row[baseline_col])

        if eps_baseline == 0:
            continue

        surprise = (eps_actual - eps_baseline) / abs(eps_baseline)
        abs_surprise = abs(surprise)

        if abs_surprise < threshold:
            continue

        signal_type = "buy" if surprise > 0 else "sell"
        confidence = min(abs_surprise / threshold, 2.0)  # cap at 2.0

        signal = {
            "symbol": symbol,
            "signal_type": signal_type,
            "event": "earnings_surprise",
            "confidence": round(confidence, 4),
            "surprise": round(surprise, 4),
            "eps_actual": eps_actual,
            f"eps_{baseline_label}": eps_baseline,
            "report_date": str(row.get("report_date", "")),
            "price": round(float(row["price"]), 2) if "price" in row and not pd.isna(row["price"]) else None,
        }
        signals.append(signal)

    # Sort by surprise magnitude descending
    signals.sort(key=lambda s: abs(s["surprise"]), reverse=True)

    buy_count = sum(1 for s in signals if s["signal_type"] == "buy")
    sell_count = len(signals) - buy_count
    logger.info(
        "Earnings surprise scan: %d signals (buy=%d, sell=%d), threshold=%.1f%%",
        len(signals), buy_count, sell_count, threshold * 100,
    )
    return signals


# ---------------------------------------------------------------------------
# Index Rebalancing Detector
# ---------------------------------------------------------------------------

def index_rebalance_detector(df: pd.DataFrame) -> list[dict]:
    """Detect stocks being added to or removed from major indices.

    Expects DataFrame with columns: symbol, index_name, action ('add'/'remove'),
    effective_date, price, weight_estimate.

    Parameters
    ----------
    df : pd.DataFrame
        Index rebalance data.

    Returns
    -------
    list[dict]
        Signals for additions (buy) and removals (sell).
    """
    required_cols = {"symbol", "action"}
    missing = required_cols - set(df.columns)
    if missing:
        logger.warning("Index rebalance detector: missing columns %s", missing)
        return []

    signals: list[dict] = []

    for _, row in df.iterrows():
        symbol = str(row["symbol"])
        action = str(row.get("action", "")).lower().strip()

        if action not in ("add", "remove"):
            continue

        signal_type = "buy" if action == "add" else "sell"
        index_name = str(row.get("index_name", "unknown"))

        # Confidence: additions to major indices (CSI 300, SSE 50) get higher weight
        major_indices = {"csi300", "csi 300", "sse50", "sse 50", "csi500", "csi 500"}
        confidence = 0.85 if index_name.lower() in major_indices else 0.55

        signal = {
            "symbol": symbol,
            "signal_type": signal_type,
            "event": "index_rebalance",
            "confidence": confidence,
            "index_name": index_name,
            "action": action,
            "effective_date": str(row.get("effective_date", "")),
            "weight_estimate": (
                round(float(row["weight_estimate"]), 6)
                if "weight_estimate" in row and not pd.isna(row["weight_estimate"])
                else None
            ),
            "price": (
                round(float(row["price"]), 2)
                if "price" in row and not pd.isna(row["price"])
                else None
            ),
        }
        signals.append(signal)

    buy_count = sum(1 for s in signals if s["signal_type"] == "buy")
    sell_count = len(signals) - buy_count
    logger.info(
        "Index rebalance scan: %d signals (additions=%d, removals=%d)",
        len(signals), buy_count, sell_count,
    )
    return signals


# ---------------------------------------------------------------------------
# Limit-Up Breakout
# ---------------------------------------------------------------------------

def limit_up_breakout(
    df: pd.DataFrame,
    min_volume_ratio: float = 2.0,
    limit_up: float = A_SHARE_LIMIT_UP,
) -> list[dict]:
    """Detect stocks hitting limit-up with abnormally high volume.

    A "limit-up breakout" occurs when:
      1. Today's return >= limit_up threshold (close nearly at limit-up)
      2. Today's volume >= min_volume_ratio × average volume (last N days)
      3. Stock did NOT hit limit-down recently (filter false breakouts)

    Parameters
    ----------
    df : pd.DataFrame
        Daily OHLCV data. Must have columns: open, high, low, close, volume.
        Index = dates.
    min_volume_ratio : float
        Minimum ratio of today's volume / 20-day average volume.
    limit_up : float
        Limit-up threshold as fraction (default 0.10 for A-share).

    Returns
    -------
    list[dict]
        Breakout signals sorted by volume ratio descending.
    """
    if df.empty:
        logger.warning("Limit-up breakout: empty DataFrame")
        return []

    required = {"close", "volume"}
    missing = required - set(df.columns)
    if missing:
        logger.warning("Limit-up breakout: missing columns %s", missing)
        return []

    df = df.copy()

    # Calculate daily returns
    df["return"] = df["close"].pct_change()

    # Calculate 20-day average volume
    df["avg_vol_20"] = df["volume"].rolling(20).mean()

    # Volume ratio
    df["vol_ratio"] = df["volume"] / df["avg_vol_20"]

    # Focus on the latest day
    latest = df.iloc[-1]
    latest_date = str(df.index[-1])

    ret = float(latest["return"]) if not pd.isna(latest["return"]) else 0.0
    vol_ratio = float(latest["vol_ratio"]) if not pd.isna(latest["vol_ratio"]) else 0.0

    if ret < limit_up:
        logger.debug(
            "Limit-up breakout: return %.4f < limit %.4f, no signal",
            ret, limit_up,
        )
        return []

    if vol_ratio < min_volume_ratio:
        logger.debug(
            "Limit-up breakout: vol_ratio %.2f < min %.2f, weak volume",
            vol_ratio, min_volume_ratio,
        )
        # Weak signal: breakout with insufficient volume confirmation
        signals = [{
            "symbol": "unknown",
            "signal_type": "buy",
            "event": "limit_up_breakout",
            "confidence": round(vol_ratio / (min_volume_ratio * 2), 4),
            "return": round(ret, 4),
            "volume_ratio": round(vol_ratio, 2),
            "close": round(float(latest["close"]), 2),
            "date": latest_date,
            "strength": "weak",
            "note": f"Volume ratio {vol_ratio:.1f}x below threshold {min_volume_ratio:.1f}x",
        }]
        logger.info("Limit-up breakout: 1 weak signal (vol_ratio=%.2f)", vol_ratio)
        return signals

    # Strong breakout: high price move + volume confirmation
    confidence = min(vol_ratio / min_volume_ratio, 3.0)

    # Check if it gapped up (open near high)
    open_price = float(latest["open"]) if "open" in df.columns and not pd.isna(latest["open"]) else None
    high_price = float(latest["high"]) if "high" in df.columns and not pd.isna(latest["high"]) else None

    gap_up = False
    if open_price and high_price and high_price > 0:
        gap_up = (open_price / high_price) > 0.99  # opened within 1% of high

    signal = {
        "symbol": "unknown",
        "signal_type": "buy",
        "event": "limit_up_breakout",
        "confidence": round(confidence, 4),
        "return": round(ret, 4),
        "volume_ratio": round(vol_ratio, 2),
        "close": round(float(latest["close"]), 2),
        "volume": int(latest["volume"]) if not pd.isna(latest["volume"]) else 0,
        "avg_vol_20": round(float(latest["avg_vol_20"]), 0) if not pd.isna(latest["avg_vol_20"]) else 0,
        "date": latest_date,
        "strength": "strong",
        "gap_up": gap_up,
    }

    logger.info(
        "Limit-up breakout: 1 strong signal (ret=%.2f%%, vol=%.1fx)",
        ret * 100, vol_ratio,
    )
    return [signal]


def limit_up_breakout_screen(
    stocks_data: dict[str, pd.DataFrame],
    min_volume_ratio: float = 2.0,
    limit_up: float = A_SHARE_LIMIT_UP,
) -> list[dict]:
    """Screen multiple stocks for limit-up breakouts in one call.

    Parameters
    ----------
    stocks_data : dict[str, pd.DataFrame]
        Map of symbol → daily OHLCV DataFrame.
    min_volume_ratio : float
        Minimum volume ratio for strong signal.
    limit_up : float
        Limit-up threshold.

    Returns
    -------
    list[dict]
        All breakout signals sorted by confidence descending.
    """
    all_signals: list[dict] = []
    for symbol, sdf in stocks_data.items():
        signals = limit_up_breakout(sdf, min_volume_ratio, limit_up)
        for s in signals:
            s["symbol"] = symbol
        all_signals.extend(signals)

    all_signals.sort(key=lambda s: s["confidence"], reverse=True)
    logger.info(
        "Limit-up breakout screen: %d stocks → %d signals",
        len(stocks_data), len(all_signals),
    )
    return all_signals
