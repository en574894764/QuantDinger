"""TA-Lib technical indicators API — MACD, RSI, BOLL, KDJ, ATR, WR, CCI, etc.

Reads OHLCV data from Parquet store, computes indicators via TA-Lib,
returns JSON.
"""

import logging

from flask import jsonify, request

from app.extensions.quant_sys import quant_bp

logger = logging.getLogger(__name__)

# All supported indicators with parameter descriptions
INDICATOR_DEFS = {
    "macd": {"fast": 12, "slow": 26, "signal": 9, "label": "MACD"},
    "rsi": {"period": 14, "label": "RSI"},
    "boll": {"period": 20, "nbdev": 2, "label": "BOLL"},
    "kdj": {"fastk": 9, "slowk": 3, "slowd": 3, "label": "KDJ"},
    "atr": {"period": 14, "label": "ATR"},
    "wr": {"period": 10, "label": "WR"},
    "cci": {"period": 14, "label": "CCI"},
    "sma": {"period": 20, "label": "SMA"},
    "ema": {"period": 20, "label": "EMA"},
    "obv": {"label": "OBV"},
    "volume": {"label": "Volume"},
    "mfi": {"period": 14, "label": "MFI"},
    "adx": {"period": 14, "label": "ADX"},
    "ROC": {"period": 12, "label": "ROC"},
    "stoch": {"fastk_period": 9, "slowk_period": 3, "slowd_period": 3, "label": "Stochastic"},
    "ultosc": {"period1": 7, "period2": 14, "period3": 28, "label": "Ultimate Oscillator"},
    "TRIX": {"period": 30, "label": "TRIX"},
    "WILLR": {"period": 14, "label": "Williams %R"},
    "NATR": {"period": 14, "label": "Normalized ATR"},
    "dx": {"period": 14, "label": "DX"},
    "MINUS_DI": {"period": 14, "label": "Minus DI"},
    "PLUS_DI": {"period": 14, "label": "Plus DI"},
    "MFI": {"period": 14, "label": "Money Flow Index"},
    "BBANDS": {"period": 20, "nbdevup": 2, "nbdevdn": 2, "label": "Bollinger Bands"},
}


def _load_ohlcv(symbol: str, limit: int = 200):
    """Load OHLCV data from Parquet store for a symbol."""
    import pandas as pd
    import numpy as np

    from app.extensions.quant_sys.data.store.parquet import ParquetStore

    store = ParquetStore()
    try:
        for prefix in ["a_shares/daily/", ""]:
            df = store.read_partitioned(
                f"{prefix}{symbol}",
                start_date="20200101",
                storage="processed",
            )
            if df is not None and not df.empty:
                break
    except Exception:
        df = None

    if df is None or df.empty:
        return None

    df = df.tail(limit)
    # Normalize column names
    for col in ["open", "high", "low", "close", "vol"]:
        if col not in df.columns:
            for alt in ["Open", "OPEN", "volume", "Volume", "VOL"]:
                if alt in df.columns:
                    df[col] = df[alt]
                    break
    return df


def _compute_indicator(indicator: str, df, params: dict):
    """Compute a TA-Lib indicator, return {values, config}."""
    import numpy as np
    import talib

    indicator_upper = indicator.upper()
    defaults = INDICATOR_DEFS.get(indicator, INDICATOR_DEFS.get(indicator_upper, {}))

    result = {"indicator": indicator, "values": [], "config": {}}

    try:
        close = df["close"].values.astype(np.float64)
        high = df["high"].values.astype(np.float64) if "high" in df.columns else close
        low = df["low"].values.astype(np.float64) if "low" in df.columns else close
        open_ = df["open"].values.astype(np.float64) if "open" in df.columns else close
        volume = df["vol"].values.astype(np.float64) if "vol" in df.columns else None

        idx = 0

        def _p(name, default):
            return params.get(name, defaults.get(name, default))

        if indicator_upper == "MACD":
            fast = _p("fast", 12)
            slow = _p("slow", 26)
            sig = _p("signal", 9)
            macd, signal, hist = talib.MACD(close, fast, slow, sig)
            result["values"] = [
                {"index": i, "macd": float(m) if not np.isnan(m) else None,
                 "signal": float(s) if not np.isnan(s) else None,
                 "histogram": float(h) if not np.isnan(h) else None}
                for i, (m, s, h) in enumerate(zip(macd, signal, hist))
            ]
            result["config"] = {"fast": fast, "slow": slow, "signal": sig}

        elif indicator_upper == "RSI":
            period = _p("period", 14)
            rsi = talib.RSI(close, period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(rsi)]
            result["config"] = {"period": period}

        elif indicator_upper in ("BOLL", "BBANDS"):
            period = _p("period", _p("nbdev", 20))
            nbdev = _p("nbdev", _p("nbdevup", 2))
            u, m, l = talib.BBANDS(close, period, nbdev, nbdev)
            result["values"] = [
                {"index": i, "upper": float(up) if not np.isnan(up) else None,
                 "middle": float(md) if not np.isnan(md) else None,
                 "lower": float(lo) if not np.isnan(lo) else None}
                for i, (up, md, lo) in enumerate(zip(u, m, l))
            ]
            result["config"] = {"period": period, "nbdev": nbdev}

        elif indicator_upper == "KDJ":
            fk = _p("fastk", 9)
            sk = _p("slowk", 3)
            sd = _p("slowd", 3)
            k, d = talib.STOCH(high, low, close, fk, sk, 0, sk, sd)
            result["values"] = [
                {"index": i, "k": float(kv) if not np.isnan(kv) else None,
                 "d": float(dv) if not np.isnan(dv) else None}
                for i, (kv, dv) in enumerate(zip(k, d))
            ]
            result["config"] = {"fastk": fk, "slowk": sk, "slowd": sd}

        elif indicator_upper == "ATR":
            period = _p("period", 14)
            atr = talib.ATR(high, low, close, period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(atr)]
            result["config"] = {"period": period}

        elif indicator_upper in ("WR", "WILLR"):
            period = _p("period", 10)
            wr = talib.WILLR(high, low, close, period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(wr)]
            result["config"] = {"period": period}

        elif indicator_upper == "CCI":
            period = _p("period", 14)
            cci = talib.CCI(high, low, close, period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(cci)]
            result["config"] = {"period": period}

        elif indicator_upper == "SMA":
            period = _p("period", 20)
            sma = talib.SMA(close, period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(sma)]
            result["config"] = {"period": period}

        elif indicator_upper == "EMA":
            period = _p("period", 20)
            ema = talib.EMA(close, period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(ema)]
            result["config"] = {"period": period}

        elif indicator_upper == "OBV":
            obv = talib.OBV(close, volume if volume is not None else np.ones_like(close))
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(obv)]

        elif indicator_upper == "VOLUME":
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else 0} for i, v in enumerate(volume)] if volume is not None else []

        elif indicator_upper == "MFI":
            period = _p("period", 14)
            mfi = talib.MFI(high, low, close, volume if volume is not None else np.ones_like(close), period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(mfi)]
            result["config"] = {"period": period}

        elif indicator_upper == "ADX":
            period = _p("period", 14)
            adx = talib.ADX(high, low, close, period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(adx)]
            result["config"] = {"period": period}

        elif indicator_upper == "DX":
            period = _p("period", 14)
            d = talib.DX(high, low, close, period)
            result["values"] = [{"index": i, "value": float(v) if not np.isnan(v) else None} for i, v in enumerate(d)]
            result["config"] = {"period": period}

        else:
            return {"error": f"Unsupported indicator: {indicator}", "available": list(INDICATOR_DEFS.keys())}

    except Exception as e:
        logger.exception("Indicator compute failed: %s", indicator)
        return {"error": str(e), "indicator": indicator}

    return result


# ── Routes ─────────────────────────────────────────────────────────────

@quant_bp.route("/indicator/list")
def indicator_list():
    """List all available TA-Lib indicators with default parameters."""
    return jsonify({"indicators": INDICATOR_DEFS, "count": len(INDICATOR_DEFS)})


@quant_bp.route("/indicator/<symbol>")
def indicator_compute(symbol: str):
    """Compute TA-Lib indicators for a stock.

    Query params:
        indicator  — indicator name (required): macd, rsi, boll, kdj, atr, etc.
        fast/slow/signal/period/nbdev — override default params
        limit      — max data points (default 200)
    """
    indicator = request.args.get("indicator", "").strip().lower()
    if not indicator:
        return jsonify({
            "error": "indicator query parameter required",
            "available": list(INDICATOR_DEFS.keys()),
        }), 400

    limit = request.args.get("limit", 200, type=int)
    limit = min(limit, 500)

    df = _load_ohlcv(symbol, limit=limit)
    if df is None:
        return jsonify({"error": f"No data for {symbol}"}), 404

    # Collect custom params
    params = {}
    for key in request.args:
        if key not in ("indicator", "symbol", "limit"):
            try:
                params[key] = float(request.args[key])
            except (ValueError, TypeError):
                params[key] = request.args[key]

    result = _compute_indicator(indicator, df, params)
    if "error" in result:
        code = 404 if "Unsupported" in result.get("error", "") else 500
        return jsonify(result), code

    result["symbol"] = symbol
    result["count"] = len(result["values"])
    return jsonify(result)


# Register at module import time
def init_app(app):
    """(No-op — routes are on quant_bp which is already registered.)"""
    pass
