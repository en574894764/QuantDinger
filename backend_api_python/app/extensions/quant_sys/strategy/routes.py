"""
Strategy experiments + L3 strategy API routes.

Routes:
  GET  /api/quant/strategy/experiments           — list experiments
  GET  /api/quant/strategy/experiments/<id>      — experiment detail
  GET  /api/quant/strategy/experiments/stats     — experiment statistics
  GET  /api/quant/strategy/factors               — list available factors
  POST /api/quant/strategy/factors/compute       — compute factors for symbols
  GET  /api/quant/strategy/factors/analysis      — IC/IR analysis
  POST /api/quant/strategy/signals/generate      — generate signals
  POST /api/quant/strategy/backtest/run          — run backtest
  GET  /api/quant/strategy/backtest/<id>/result  — backtest result
  GET  /api/quant/strategy/backtest/<id>/trades  — trade log
  POST /api/quant/strategy/sleeve-b/rotation     — run ETF rotation
  POST /api/quant/strategy/sleeve-b/rebalance    — calculate rebalance orders
  POST /api/quant/strategy/sleeve-c/earnings     — earnings surprise scan
  POST /api/quant/strategy/sleeve-c/events       — event-driven strategy scan
"""

import logging

from flask import jsonify, request

from app.extensions.quant_sys.strategy import strategy_bp
from app.extensions.quant_sys.strategy.data import (
    get_experiments,
    get_experiment_by_id,
    get_experiment_stats,
)
from app.extensions.quant_sys.strategy.factors.library import (
    FACTOR_REGISTRY,
    compute_all_factors,
    list_factors,
)
from app.extensions.quant_sys.strategy.factors.analysis import (
    compute_ic_for_factors,
    factor_correlation,
    factor_returns,
    forward_returns,
)
from app.extensions.quant_sys.strategy.signal.generator import (
    SignalConfig,
    generate_signals,
)
from app.extensions.quant_sys.strategy.backtest.runner import (
    BacktestConfig,
    get_backtest_result,
    run_backtest,
)
from app.extensions.quant_sys.strategy.backtest.config import (
    A_SHARE_COMMISSION_BUY,
    A_SHARE_COMMISSION_SELL,
    A_SHARE_MIN_COMMISSION,
    A_SHARE_SLIPPAGE,
)
from app.extensions.quant_sys.strategy.sleeve_b.rotation import (
    momentum_rotation,
    risk_parity,
    equal_weight,
    backtest_rotation,
)
from app.extensions.quant_sys.strategy.sleeve_b.executor import (
    RebalanceConfig,
    calculate_rebalance,
)
from app.extensions.quant_sys.strategy.sleeve_c.events import (
    earnings_surprise,
    index_rebalance_detector,
    limit_up_breakout,
)
from app.extensions.quant_sys.strategy.lifecycle import (
    StrategyDef,
    archive,
    create_strategy,
    get_strategy,
    go_live,
    list_strategies,
    pause,
    start_paper,
    stop,
    transition_strategy,
)
from app.extensions.quant_sys.strategy.live.broker import (
    Order,
    OrderSide,
    OrderType,
    create_broker,
)
from app.extensions.quant_sys.strategy.paper_trading.engine import (
    get_paper_session,
    start_paper_session,
    stop_paper_session,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Existing routes
# ---------------------------------------------------------------------------

@strategy_bp.route("/experiments")
def experiments():
    """List strategy experiments, optionally filtered by sleeve."""
    sleeve = request.args.get("sleeve", "")
    limit = request.args.get("limit", 50, type=int)
    data = get_experiments(sleeve=sleeve, limit=limit)
    return jsonify({"count": len(data), "data": data})


@strategy_bp.route("/experiments/<experiment_id>")
def experiment_detail(experiment_id: str):
    """Get a single experiment by ID."""
    record = get_experiment_by_id(experiment_id)
    if record is None:
        return jsonify({"error": f"Experiment not found: {experiment_id}"}), 404
    return jsonify({"data": record})


@strategy_bp.route("/experiments/stats")
def experiment_stats():
    """Aggregated statistics across all experiments."""
    stats = get_experiment_stats()
    return jsonify(stats)


# ---------------------------------------------------------------------------
# Shared data-loading helper
# ---------------------------------------------------------------------------

def _load_price_data(symbol: str, start_date: str = "", end_date: str = ""):
    """Load OHLCV data for a single symbol from the Parquet store."""
    import pandas as pd
    import numpy as np
    from app.extensions.quant_sys.data.store.parquet import ParquetStore

    store = ParquetStore()
    try:
        df = store.read_partitioned(
            f"a_shares/daily/{symbol}",
            start_date=start_date or "20200101",
            end_date=end_date or "20991231",
            storage="raw",
        )
        if df is not None and not df.empty:
            col_map = {
                "trade_date": "date", "ts_code": "symbol",
                "amount": "vol", "vol": "volume",
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
            for col in ["open", "high", "low", "close"]:
                if col not in df.columns:
                    df[col] = np.nan
            if "vol" not in df.columns and "volume" in df.columns:
                df["vol"] = df["volume"]
            if "vol" not in df.columns:
                df["vol"] = 0.0
            return df
    except Exception:
        logger.debug("ParquetStore read failed for %s", symbol)
    return None


def _load_multi_price_data(symbols: list[str], start_date: str = "", end_date: str = ""):
    """Load OHLCV data for multiple symbols into a dict of DataFrames."""
    data = {}
    for sym in symbols:
        df = _load_price_data(sym, start_date=start_date, end_date=end_date)
        if df is not None and not df.empty:
            data[sym] = df
    return data


# ---------------------------------------------------------------------------
# Factor routes
# ---------------------------------------------------------------------------

@strategy_bp.route("/factors")
def factors_list():
    """List all available factors with descriptions."""
    return jsonify({
        "count": len(FACTOR_REGISTRY),
        "data": list_factors(),
    })


@strategy_bp.route("/factors/compute", methods=["POST"])
def factors_compute():
    """
    Compute factors for a list of symbols.

    Request JSON body:
    {
        "symbols": ["600519", "000001"],
        "factor_names": ["momentum_20", "volatility_20", "rsi_14"],
        "date": "2024-01-15"  // optional, default: latest
    }

    Returns factor values per symbol per factor.
    """
    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols", [])
    factor_names = body.get("factor_names", [])
    target_date = body.get("date", None)

    if not symbols:
        return jsonify({"error": "symbols is required"}), 400
    if not factor_names:
        factor_names = list(FACTOR_REGISTRY.keys())

    # Validate factor names
    invalid = [f for f in factor_names if f not in FACTOR_REGISTRY]
    if invalid:
        return jsonify({"error": f"Unknown factors: {invalid}"}), 400

    # Load price data for each symbol
    import pandas as pd

    results = {}
    for sym in symbols:
        try:
            df = _load_price_data(sym)
            if df is None or df.empty:
                results[sym] = {"error": "No data available"}
                continue

            if target_date:
                # Slice data up to target_date
                df = df[df.index <= target_date]

            factor_df = compute_all_factors(df, factor_names)
            latest = factor_df.iloc[-1].to_dict()
            results[sym] = {
                k: round(float(v), 6) if not (v is None or (isinstance(v, float) and pd.isna(v))) else None
                for k, v in latest.items()
            }
        except Exception as e:
            logger.exception("Factor compute failed for %s", sym)
            results[sym] = {"error": str(e)}

    return jsonify({
        "date": target_date or "latest",
        "factors_computed": factor_names,
        "data": results,
    })


@strategy_bp.route("/factors/analysis", methods=["GET"])
def factors_analysis():
    """
    Run IC / IR / factor returns analysis.

    Query params:
        symbols: comma-separated list
        factor_names: comma-separated list
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        forward_period: int (default 5)

    Returns IC mean/std/IR per factor plus factor return series.
    """
    symbols_str = request.args.get("symbols", "")
    factor_names_str = request.args.get("factor_names", ",".join(list(FACTOR_REGISTRY.keys())[:10]))
    start_date = request.args.get("start_date", "2023-01-01")
    end_date = request.args.get("end_date", "2024-12-31")
    forward_period = request.args.get("forward_period", 5, type=int)

    symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
    factor_names = [f.strip() for f in factor_names_str.split(",") if f.strip()]

    if not symbols:
        return jsonify({"error": "symbols query param is required"}), 400

    # Validate factor names
    invalid = [f for f in factor_names if f not in FACTOR_REGISTRY]
    if invalid:
        return jsonify({"error": f"Unknown factors: {invalid}"}), 400

    # Load price data and compute factors
    import pandas as pd

    price_data = {}
    for sym in symbols:
        try:
            df = _load_price_data(sym, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                price_data[sym] = df
        except Exception:
            logger.debug("No data for %s in range", sym)

    if not price_data:
        return jsonify({"error": "No price data for any symbol in range"}), 404

    # Build factor panels (N symbols × T dates per factor)
    factor_panels: dict[str, pd.DataFrame] = {}
    fwd_returns: dict[str, pd.Series] = {}

    for name in factor_names:
        panels = {}
        for sym, pdf in price_data.items():
            try:
                factor_series = FACTOR_REGISTRY[name](pdf)
                panels[sym] = factor_series
            except Exception:
                continue
        if panels:
            factor_panels[name] = pd.DataFrame(panels)

        # Forward returns
        for sym, pdf in price_data.items():
            if sym not in fwd_returns:
                try:
                    fr = forward_returns(pdf, period=forward_period)
                    fwd_returns[sym] = fr
                except Exception:
                    continue

    if not factor_panels:
        return jsonify({"error": "Could not compute any factors"}), 500

    # Build forward returns DataFrame
    fwd_df = pd.DataFrame(fwd_returns) if fwd_returns else pd.DataFrame()

    # Compute IC/IR
    ic_results = compute_ic_for_factors(factor_panels, fwd_df)

    # Factor returns for the first factor
    first_factor_name = factor_names[0]
    first_factor_df = factor_panels.get(first_factor_name)
    factor_ret = {}
    if first_factor_df is not None and not fwd_df.empty:
        factor_ret = factor_returns(first_factor_df, fwd_df)

    # Factor correlation
    corr = factor_correlation(factor_panels)

    return jsonify({
        "params": {
            "symbols": symbols,
            "factor_names": factor_names,
            "start_date": start_date,
            "end_date": end_date,
            "forward_period": forward_period,
        },
        "ic_ir": ic_results,
        "factor_returns": factor_ret,
        "correlation": corr,
    })


# ---------------------------------------------------------------------------
# Signal routes
# ---------------------------------------------------------------------------

@strategy_bp.route("/signals/generate", methods=["POST"])
def signals_generate():
    """
    Generate buy/sell signals from factor-driven scoring.

    Request JSON body:
    {
        "symbols": ["600519", "000001"],
        "factor_weights": {"momentum_20": 0.5, "volatility_20": -0.3, "rsi_14": 0.2},
        "date": "2024-01-15",       // optional
        "top_n_buy": 20,
        "top_n_sell": 0,
        "buy_threshold": 0.5,
        "sell_threshold": -0.5,
        "direction": "long_only",   // "long_only", "short_only", "long_short"
        "sleeve": "A"
    }
    """
    body = request.get_json(silent=True) or {}

    symbols = body.get("symbols", [])
    factor_weights = body.get("factor_weights", {})
    target_date = body.get("date", None)

    if not symbols:
        return jsonify({"error": "symbols is required"}), 400
    if not factor_weights:
        # Default weights for demonstration
        factor_weights = {
            "momentum_20": 0.4,
            "momentum_60": 0.2,
            "volatility_20": -0.2,
            "rsi_14": 0.1,
            "volume_ratio": 0.1,
        }

    # Build config
    config = SignalConfig(
        factor_weights=factor_weights,
        top_n_buy=body.get("top_n_buy", 20),
        top_n_sell=body.get("top_n_sell", 0),
        buy_threshold=body.get("buy_threshold", 0.5),
        sell_threshold=body.get("sell_threshold", -0.5),
        direction=body.get("direction", "long_only"),
        sleeve=body.get("sleeve", "A"),
    )

    # Build factor panel from price data
    import pandas as pd

    factor_panel: dict[str, pd.DataFrame] = {}
    factor_names = list(factor_weights.keys())

    for sym in symbols:
        try:
            df = _load_price_data(sym)
            if df is None or df.empty:
                continue
            if target_date:
                df = df[df.index <= target_date]

            factor_df = compute_all_factors(df, factor_names)

            # Append to panel
            for fname in factor_names:
                if fname not in factor_panel:
                    factor_panel[fname] = pd.DataFrame()
                factor_panel[fname][sym] = factor_df[fname]
        except Exception as e:
            logger.warning("Failed to build factor data for %s: %s", sym, e)

    if not factor_panel:
        return jsonify({"error": "Could not build factor panel for any symbol"}), 500

    signals = generate_signals(factor_panel, config, date=target_date)

    return jsonify({
        "count": len(signals),
        "config": {
            "factor_weights": config.factor_weights,
            "top_n_buy": config.top_n_buy,
            "top_n_sell": config.top_n_sell,
            "direction": config.direction,
            "sleeve": config.sleeve,
        },
        "signals": signals,
    })


# ---------------------------------------------------------------------------
# Backtest routes
# ---------------------------------------------------------------------------

@strategy_bp.route("/backtest/run", methods=["POST"])
def backtest_run():
    """
    Run a standalone backtest.

    Request JSON body:
    {
        "symbols": ["600519", "000001", ...],
        "start_date": "2023-01-01",
        "end_date": "2024-12-31",
        "rebalance_days": 20,
        "sizing": "equal_weight",
        "max_holdings": 20,
        "initial_capital": 1000000,
        "commission_buy": 0.001,
        "commission_sell": 0.0015,
        "min_commission": 5.0,
        "slippage": 0.001,
        "stop_loss": 0.0,
        "factor_weights": {"momentum_20": 0.5, "volatility_20": -0.2},
        "signal_source": "composite_rank",
        "signals": []   // predefined signals (when signal_source='predefined')
    }
    """
    body = request.get_json(silent=True) or {}

    symbols = body.get("symbols", [])
    if not symbols:
        return jsonify({"error": "symbols is required"}), 400

    factor_weights = body.get("factor_weights", {})
    if not factor_weights:
        factor_weights = {
            "momentum_20": 0.4,
            "momentum_60": 0.2,
            "volatility_20": -0.2,
            "rsi_14": 0.1,
            "volume_ratio": 0.1,
        }

    config = BacktestConfig(
        start_date=body.get("start_date", "2023-01-01"),
        end_date=body.get("end_date", "2024-12-31"),
        symbols=symbols,
        rebalance_days=body.get("rebalance_days", 20),
        sizing=body.get("sizing", "equal_weight"),
        max_holdings=body.get("max_holdings", 20),
        initial_capital=float(body.get("initial_capital", 1_000_000)),
        commission_buy=float(body.get("commission_buy", A_SHARE_COMMISSION_BUY)),
        commission_sell=float(body.get("commission_sell", A_SHARE_COMMISSION_SELL)),
        min_commission=float(body.get("min_commission", A_SHARE_MIN_COMMISSION)),
        slippage=float(body.get("slippage", A_SHARE_SLIPPAGE)),
        stop_loss=float(body.get("stop_loss", 0.0)),
        signal_source=body.get("signal_source", "composite_rank"),
        signals=body.get("signals", []),
        factor_weights=factor_weights,
    )

    result = run_backtest(config)

    return jsonify({
        "task_id": result.task_id,
        "metrics": result.metrics,
        "config": result.config_summary,
        "equity_curve_length": len(result.equity_curve),
        "trade_count": len(result.trade_log),
    })


@strategy_bp.route("/backtest/<task_id>/result")
def backtest_result(task_id: str):
    """Get full backtest result including equity curve and metrics."""
    result = get_backtest_result(task_id)
    if result is None:
        return jsonify({"error": f"Backtest not found: {task_id}"}), 404

    return jsonify({
        "task_id": result.task_id,
        "created_at": result.created_at,
        "config": result.config_summary,
        "metrics": result.metrics,
        "equity_curve": result.equity_curve,
    })


@strategy_bp.route("/backtest/<task_id>/trades")
def backtest_trades(task_id: str):
    """Get the trade log for a backtest."""
    result = get_backtest_result(task_id)
    if result is None:
        return jsonify({"error": f"Backtest not found: {task_id}"}), 404

    return jsonify({
        "task_id": result.task_id,
        "count": len(result.trade_log),
        "trades": result.trade_log,
    })


# ---------------------------------------------------------------------------
# Sleeve B — ETF Rotation routes
# ---------------------------------------------------------------------------

@strategy_bp.route("/sleeve-b/rotation", methods=["POST"])
def sleeve_b_rotation():
    """
    Run ETF rotation strategy.

    Request JSON body:
    {
        "symbols": ["510050", "510300", "159915", ...],
        "strategy": "momentum" | "risk_parity" | "equal_weight",
        "lookback": 20,             // for momentum / risk_parity
        "top_k": 5,                 // for momentum
        "rebalance_freq": 20,       // for backtest
        "start_date": "2023-01-01", // optional
        "end_date": "2024-12-31",   // optional
        "backtest": true,           // run backtest too?
        "initial_capital": 1000000
    }
    """
    body = request.get_json(silent=True) or {}

    symbols = body.get("symbols", [])
    strategy = body.get("strategy", "momentum").lower()
    lookback = body.get("lookback", 20)
    top_k = body.get("top_k", 5)
    rebalance_freq = body.get("rebalance_freq", 20)
    run_backtest_flag = body.get("backtest", True)
    initial_capital = float(body.get("initial_capital", 1_000_000))

    if not symbols:
        return jsonify({"error": "symbols is required"}), 400

    # Load price data for all symbols
    start_date = body.get("start_date", "20230101")
    end_date = body.get("end_date", "20991231")

    import pandas as pd

    price_data = {}
    for sym in symbols:
        df = _load_price_data(sym, start_date=start_date, end_date=end_date)
        if df is not None and not df.empty:
            price_data[sym] = df

    if not price_data:
        return jsonify({"error": "No price data for any symbol"}), 404

    # Build combined price DataFrame (MultiIndex columns: symbol × OHLCV)
    close_panels = {}
    for sym, pdf in price_data.items():
        if "close" in pdf.columns:
            close_panels[sym] = pdf["close"]

    if not close_panels:
        return jsonify({"error": "No close price data available"}), 500

    # Build a wide DataFrame: dates × symbols
    wide_df = pd.DataFrame(close_panels).sort_index()

    # Select and run strategy
    strategy_map = {
        "momentum": lambda df: momentum_rotation(df, lookback=lookback, top_k=top_k),
        "risk_parity": lambda df: risk_parity(df, lookback=lookback),
        "risk_parity_rotation": lambda df: risk_parity(df, lookback=lookback),
        "equal_weight": equal_weight,
        "equal": equal_weight,
    }

    strategy_fn = strategy_map.get(strategy)
    if strategy_fn is None:
        return jsonify({
            "error": f"Unknown strategy '{strategy}'. "
                     f"Choose: {list(strategy_map.keys())}"
        }), 400

    try:
        target_weights = strategy_fn(wide_df)
    except Exception as e:
        logger.exception("Rotation strategy failed: %s", e)
        return jsonify({"error": f"Strategy execution failed: {e}"}), 500

    response_data = {
        "strategy": strategy,
        "target_weights": target_weights,
        "n_selected": len(target_weights),
        "params": {
            "lookback": lookback,
            "top_k": top_k,
        },
    }

    # Optionally run backtest
    if run_backtest_flag:
        try:
            bt_result = backtest_rotation(
                wide_df,
                strategy_fn=strategy_fn,
                rebalance_freq=rebalance_freq,
                initial_capital=initial_capital,
            )
            response_data["backtest"] = {
                "metrics": bt_result["metrics"],
                "equity_curve_length": len(bt_result["equity_curve"]),
                "final_weights": bt_result["final_weights"],
            }
        except Exception as e:
            logger.exception("Backtest failed: %s", e)
            response_data["backtest"] = {"error": str(e)}

    return jsonify(response_data)


@strategy_bp.route("/sleeve-b/rebalance", methods=["POST"])
def sleeve_b_rebalance():
    """
    Calculate ETF rebalance orders from current positions to target weights.

    Request JSON body:
    {
        "current_positions": {"510050": 0.3, "510300": 0.4, ...},
        "target_weights": {"510050": 0.25, "510300": 0.35, ...},
        "capital": 1000000,
        "min_trade_value": 5000,
        "max_turnover": 0.5,
        "max_single_weight": 0.2,
        "commission_buy": 0.0003,
        "commission_sell": 0.0013,
        "min_commission": 5.0,
        "slippage": 0.001,
        "lot_size": 100,
        "allow_partial": true,
        "strategy": "momentum",   // optional: auto-calculate target from rotation
        "symbols": [...],          // required if 'strategy' is set
        "lookback": 20,
        "top_k": 5
    }
    """
    body = request.get_json(silent=True) or {}

    current_positions = body.get("current_positions", {})
    target_weights = body.get("target_weights", {})
    capital = float(body.get("capital", 1_000_000))

    # Optionally compute target weights from a rotation strategy
    strategy_name = body.get("strategy", "")
    if strategy_name and not target_weights:
        symbols = body.get("symbols", [])
        if not symbols:
            return jsonify({"error": "symbols required when using 'strategy' param"}), 400

        lookback = body.get("lookback", 20)
        top_k = body.get("top_k", 5)

        import pandas as pd

        close_panels = {}
        for sym in symbols:
            df = _load_price_data(sym)
            if df is not None and not df.empty and "close" in df.columns:
                close_panels[sym] = df["close"]

        if not close_panels:
            return jsonify({"error": "No price data for symbols"}), 404

        wide_df = pd.DataFrame(close_panels).sort_index()
        strategy_map = {
            "momentum": lambda df: momentum_rotation(df, lookback=lookback, top_k=top_k),
            "risk_parity": lambda df: risk_parity(df, lookback=lookback),
            "equal_weight": equal_weight,
        }
        strategy_fn = strategy_map.get(strategy_name.lower())
        if strategy_fn is None:
            return jsonify({"error": f"Unknown strategy: {strategy_name}"}), 400

        try:
            target_weights = strategy_fn(wide_df)
        except Exception as e:
            return jsonify({"error": f"Strategy execution failed: {e}"}), 500

    if not target_weights:
        return jsonify({"error": "target_weights is required (or provide strategy + symbols)"}), 400

    # Build rebalance config
    config = RebalanceConfig(
        min_trade_value=float(body.get("min_trade_value", 5_000)),
        commission_buy=float(body.get("commission_buy", 0.0003)),
        commission_sell=float(body.get("commission_sell", 0.0013)),
        min_commission=float(body.get("min_commission", 5.0)),
        slippage=float(body.get("slippage", 0.001)),
        lot_size=int(body.get("lot_size", 100)),
        max_turnover=float(body.get("max_turnover", 0.50)),
        max_single_weight=float(body.get("max_single_weight", 0.20)),
        allow_partial=bool(body.get("allow_partial", True)),
    )

    try:
        result = calculate_rebalance(
            current_positions=current_positions,
            target_weights=target_weights,
            capital=capital,
            config=config,
        )
    except Exception as e:
        logger.exception("Rebalance calculation failed: %s", e)
        return jsonify({"error": f"Rebalance calculation failed: {e}"}), 500

    return jsonify({
        "current_positions": result.current_positions,
        "target_weights": result.target_weights,
        "orders": [
            {
                "symbol": o.symbol,
                "side": o.side,
                "target_weight": round(o.target_weight, 6),
                "current_weight": round(o.current_weight, 6),
                "delta_weight": round(o.delta_weight, 6),
                "notional": o.notional,
                "shares": o.shares,
                "reason": o.reason,
            }
            for o in result.orders
        ],
        "summary": {
            "total_buy_notional": result.total_buy_notional,
            "total_sell_notional": result.total_sell_notional,
            "turnover": result.turnover,
            "remaining_cash_weight": result.remaining_cash_weight,
            "order_count": len(result.orders),
        },
        "notes": result.notes,
    })


# ---------------------------------------------------------------------------
# Sleeve C — Event-Driven routes
# ---------------------------------------------------------------------------

@strategy_bp.route("/sleeve-c/earnings", methods=["POST"])
def sleeve_c_earnings():
    """
    Scan for earnings surprise signals.

    Request JSON body:
    {
        "data": [
            {
                "symbol": "600519",
                "eps_actual": 12.5,
                "eps_prior": 10.2,
                "eps_consensus": 11.0,
                "report_date": "2024-03-31",
                "price": 1800.0
            },
            ...
        ],
        "threshold": 0.05   // minimum surprise fraction (default 0.05 = 5%)
    }
    """
    body = request.get_json(silent=True) or {}

    data_rows = body.get("data", [])
    threshold = float(body.get("threshold", 0.05))

    if not data_rows:
        return jsonify({"error": "data is required (list of earnings records)"}), 400

    try:
        import pandas as pd
        df = pd.DataFrame(data_rows)
    except Exception as e:
        return jsonify({"error": f"Invalid data format: {e}"}), 400

    try:
        signals = earnings_surprise(df, threshold=threshold)
    except Exception as e:
        logger.exception("Earnings surprise scan failed: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "count": len(signals),
        "threshold": threshold,
        "buy_count": sum(1 for s in signals if s["signal_type"] == "buy"),
        "sell_count": sum(1 for s in signals if s["signal_type"] == "sell"),
        "signals": signals,
    })


@strategy_bp.route("/sleeve-c/events", methods=["POST"])
def sleeve_c_events():
    """
    Scan for event-driven strategy signals (index rebalance, limit-up breakout, etc.).

    Request JSON body:
    {
        "event_type": "index_rebalance" | "limit_up_breakout",
        // For index_rebalance:
        "data": [
            {
                "symbol": "600519",
                "action": "add",
                "index_name": "CSI 300",
                "effective_date": "2024-06-15",
                "weight_estimate": 0.015,
                "price": 1800.0
            },
            ...
        ],
        // For limit_up_breakout:
        "symbols": ["600519", "000001", ...],
        "min_volume_ratio": 2.0,
        "limit_up": 0.10
    }
    """
    body = request.get_json(silent=True) or {}

    event_type = body.get("event_type", "").lower()
    if not event_type:
        return jsonify({"error": "event_type is required (index_rebalance, limit_up_breakout)"}), 400

    try:
        if event_type == "index_rebalance":
            data_rows = body.get("data", [])
            if not data_rows:
                return jsonify({"error": "data required for index_rebalance"}), 400

            import pandas as pd
            df = pd.DataFrame(data_rows)
            signals = index_rebalance_detector(df)

        elif event_type == "limit_up_breakout":
            symbols = body.get("symbols", [])
            min_volume_ratio = float(body.get("min_volume_ratio", 2.0))
            limit_up = float(body.get("limit_up", 0.10))

            if not symbols:
                return jsonify({"error": "symbols required for limit_up_breakout"}), 400

            all_signals = []
            for sym in symbols:
                df = _load_price_data(sym)
                if df is None or df.empty:
                    continue
                sigs = limit_up_breakout(df, min_volume_ratio=min_volume_ratio, limit_up=limit_up)
                for s in sigs:
                    s["symbol"] = sym
                all_signals.extend(sigs)

            # Sort by confidence descending
            all_signals.sort(key=lambda s: s.get("confidence", 0), reverse=True)
            signals = all_signals

        else:
            return jsonify({
                "error": f"Unknown event_type '{event_type}'. "
                         f"Choose: index_rebalance, limit_up_breakout"
            }), 400

    except Exception as e:
        logger.exception("Event-driven scan failed: %s", e)
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "event_type": event_type,
        "count": len(signals),
        "buy_count": sum(1 for s in signals if s.get("signal_type") == "buy"),
        "sell_count": sum(1 for s in signals if s.get("signal_type") == "sell"),
        "signals": signals,
    })


# ---------------------------------------------------------------------------
# Strategy lifecycle routes (L3)
# ---------------------------------------------------------------------------

@strategy_bp.route("/lifecycle/list", methods=["GET"])
def lifecycle_list():
    """List all registered strategies."""
    data = list_strategies()
    return jsonify({"count": len(data), "data": data})


@strategy_bp.route("/lifecycle/get/<strategy_id>", methods=["GET"])
def lifecycle_get(strategy_id: str):
    """Get a single strategy by ID."""
    record = get_strategy(strategy_id)
    if record is None:
        return jsonify({"error": f"Strategy not found: {strategy_id}"}), 404
    return jsonify({"data": record})


@strategy_bp.route("/lifecycle/create", methods=["POST"])
def lifecycle_create():
    """Create a new strategy definition."""
    body = request.get_json(silent=True) or {}
    try:
        sd = StrategyDef(
            name=body.get("name", "Untitled"),
            sleeve=body.get("sleeve", "A"),
            description=body.get("description", ""),
            rule=body.get("rule", {}),
            risk=body.get("risk", {}),
            execution=body.get("execution", {}),
        )
        strategy_id = create_strategy(sd)
        return jsonify({"strategy_id": strategy_id}), 201
    except Exception as e:
        logger.exception("Failed to create strategy")
        return jsonify({"error": str(e)}), 400


@strategy_bp.route("/lifecycle/transition", methods=["POST"])
def lifecycle_transition():
    """Transition a strategy to another state."""
    body = request.get_json(silent=True) or {}
    strategy_id = body.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"error": "strategy_id is required"}), 400
    try:
        transition_strategy(strategy_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.exception("Transition failed")
        return jsonify({"error": str(e)}), 400


@strategy_bp.route("/lifecycle/pause", methods=["POST"])
def lifecycle_pause():
    """Pause a running strategy."""
    body = request.get_json(silent=True) or {}
    strategy_id = body.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"error": "strategy_id is required"}), 400
    try:
        pause(strategy_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@strategy_bp.route("/lifecycle/stop", methods=["POST"])
def lifecycle_stop():
    """Stop a strategy entirely."""
    body = request.get_json(silent=True) or {}
    strategy_id = body.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"error": "strategy_id is required"}), 400
    try:
        stop(strategy_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@strategy_bp.route("/lifecycle/archive", methods=["POST"])
def lifecycle_archive():
    """Archive a strategy."""
    body = request.get_json(silent=True) or {}
    strategy_id = body.get("strategy_id", "")
    if not strategy_id:
        return jsonify({"error": "strategy_id is required"}), 400
    try:
        archive(strategy_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# DSR / FDR Statistical Analysis Routes
# ---------------------------------------------------------------------------

from app.extensions.quant_sys.strategy.dsr import (
    benjamini_hochberg,
    compute_dsr_report,
    deflated_sharpe_ratio,
    fdr_filter_factors,
    minimum_backtest_length,
)


@strategy_bp.route("/dsr/compute", methods=["POST"])
def dsr_compute():
    """Compute Deflated Sharpe Ratio and minimum backtest length.

    POST JSON body:
    {
        "observed_sharpe": 1.5,       // strategy's Sharpe ratio
        "n_trials": 100,              // total experiments attempted
        "n_observations": 252,        // number of return periods
        "returns": [0.01, -0.02, ...],// optional: period returns for skew/kurtosis
        "skewness": 0,                // optional override
        "kurtosis": 3                 // optional override
    }
    """
    body = request.get_json(silent=True) or {}

    observed_sharpe = float(body.get("observed_sharpe", 0))
    if observed_sharpe <= 0:
        return jsonify({"error": "observed_sharpe must be > 0"}), 400

    n_trials = int(body.get("n_trials", 1))
    n_observations = int(body.get("n_observations", 252))
    returns = body.get("returns")
    skewness = float(body.get("skewness", 0))
    kurtosis = float(body.get("kurtosis", 3))

    try:
        report = compute_dsr_report(
            observed_sharpe=observed_sharpe,
            n_trials=n_trials,
            n_observations=n_observations,
            returns=returns,
            skewness=skewness,
            kurtosis=kurtosis,
        )
        return jsonify(report)
    except Exception as e:
        logger.exception("DSR compute failed")
        return jsonify({"error": str(e)}), 500


@strategy_bp.route("/dsr/min-length", methods=["POST"])
def dsr_min_length():
    """Compute minimum backtest length for a target Sharpe.

    POST JSON body:
    {
        "target_sharpe": 1.0,
        "n_trials": 50,
        "skewness": 0,
        "kurtosis": 3,
        "significance": 0.05
    }
    """
    body = request.get_json(silent=True) or {}

    target_sharpe = float(body.get("target_sharpe", 1.0))
    if target_sharpe <= 0:
        return jsonify({"error": "target_sharpe must be > 0"}), 400

    try:
        min_obs = minimum_backtest_length(
            target_sharpe=target_sharpe,
            n_trials=int(body.get("n_trials", 1)),
            skewness=float(body.get("skewness", 0)),
            kurtosis=float(body.get("kurtosis", 3)),
            significance=float(body.get("significance", 0.05)),
        )
        return jsonify({
            "target_sharpe": target_sharpe,
            "n_trials": int(body.get("n_trials", 1)),
            "min_observations": min_obs,
            "min_trading_days": min_obs,
            "min_calendar_years": round(min_obs / 252, 1),
        })
    except Exception as e:
        logger.exception("DSR min-length failed")
        return jsonify({"error": str(e)}), 500


@strategy_bp.route("/fdr/filter", methods=["POST"])
def fdr_filter():
    """Apply FDR correction to factor IC results.

    POST JSON body:
    {
        "factors": [
            {"factor_name": "momentum_20", "ic_mean": 0.05, "ir": 0.8, "p_value": 0.001},
            ...
        ],
        "alpha": 0.05
    }
    """
    body = request.get_json(silent=True) or {}
    factors = body.get("factors", [])
    if not factors:
        return jsonify({"error": "factors list is required"}), 400

    alpha = float(body.get("alpha", 0.05))

    try:
        result = fdr_filter_factors(factors, alpha=alpha)
        n_sig = sum(1 for r in result if r["fdr_significant"])
        return jsonify({
            "alpha": alpha,
            "total_factors": len(result),
            "significant_factors": n_sig,
            "factors": result,
        })
    except Exception as e:
        logger.exception("FDR filter failed")
        return jsonify({"error": str(e)}), 500


@strategy_bp.route("/fdr/validate-experiments", methods=["POST"])
def fdr_validate_experiments():
    """Validate experiment results using DSR + FDR.

    POST JSON body:
    {
        "experiments": [
            {"id": 1, "sharpe": 1.2, "n_observations": 252},
            ...
        ],
        "best_id": 1  // optional
    }
    """
    body = request.get_json(silent=True) or {}
    experiments = body.get("experiments", [])
    if not experiments:
        return jsonify({"error": "experiments list is required"}), 400

    try:
        import numpy as np
        from scipy.stats import norm

        best_id = body.get("best_id")
        if best_id is not None:
            best = next((e for e in experiments if e.get("id") == best_id), None)
            if not best:
                return jsonify({"error": f"best_id not found"}), 400
        else:
            best = max(experiments, key=lambda e: e.get("sharpe", 0))

        n_trials = len(experiments)
        best_sharpe = best.get("sharpe", 0)
        n_obs = best.get("n_observations", 252)

        dsr_report = compute_dsr_report(
            observed_sharpe=best_sharpe,
            n_trials=n_trials,
            n_observations=n_obs,
        )

        for exp in experiments:
            z = exp.get("sharpe", 0) * np.sqrt(exp.get("n_observations", 252) - 1)
            exp["approx_p_value"] = round(float(1 - norm.cdf(abs(z))), 6)

        fdr_result = fdr_filter_factors([
            {
                "factor_name": f"exp_{e.get('id', i)}",
                "ic_mean": e.get("sharpe", 0),
                "ir": e.get("sharpe", 0),
                "p_value": e.get("approx_p_value", 1.0),
            }
            for i, e in enumerate(experiments)
        ])

        return jsonify({
            "n_experiments": n_trials,
            "best_experiment": {"id": best.get("id"), "sharpe": best_sharpe},
            "dsr_report": dsr_report,
            "fdr_filter": {
                "total": len(fdr_result),
                "significant": sum(1 for r in fdr_result if r["fdr_significant"]),
                "factors": fdr_result,
            },
        })
    except Exception as e:
        logger.exception("FDR validate-experiments failed")
        return jsonify({"error": str(e)}), 500
