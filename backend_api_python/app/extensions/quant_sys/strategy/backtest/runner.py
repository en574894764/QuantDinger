"""
Standalone backtest runner — anti-bias event-driven backtesting engine.

Core anti-bias rules:
  - Decision at T-day close uses data available at T-1 (no look-ahead).
  - Execution happens at T+1 open price.
  - Factor values are lagged by 1 day from the signal date.

Supports:
  - Equal-weight and inverse-volatility position sizing.
  - A-share commission schedule (buy 0.1%, sell 0.15%, min 5 RMB).
  - Slippage (0.1% default).
  - Stop-loss per position.
  - Cash reserve ratio.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from app.extensions.quant_sys.strategy.backtest.config import (
    BacktestConfig,
    is_trading_day,
    next_trading_day,
    prev_trading_day,
)
from app.extensions.quant_sys.strategy.factors.library import (
    compute_all_factors,
    FACTOR_REGISTRY,
)
from app.extensions.quant_sys.strategy.signal.generator import (
    SignalConfig,
    generate_signals,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price data access
# ---------------------------------------------------------------------------

def _load_price_data(symbol: str, start_date: str = "", end_date: str = ""):
    """
    Load OHLCV data for a single symbol from the Parquet store.

    Returns a DataFrame or None.
    """
    import pandas as pd
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
            # Standardize column names
            col_map = {
                "trade_date": "date", "ts_code": "symbol",
                "amount": "vol", "vol": "volume",
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
            # Ensure required columns
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


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

class BacktestResult:
    """Container for a completed backtest."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.equity_curve: list[dict] = []
        self.trade_log: list[dict] = []
        self.metrics: dict = {}
        self.config_summary: dict = {}
        self.created_at = datetime.now(timezone.utc).isoformat()


def _apply_commission(amount: float, side: str, config: BacktestConfig) -> float:
    """Apply commission + stamp duty + slippage to a trade amount."""
    if side == "buy":
        rate = config.commission_buy + config.slippage
    else:
        rate = config.commission_sell + config.slippage

    commission = max(amount * rate, config.min_commission)
    return commission


def _compute_metrics(
    equity_curve: pd.DataFrame,
    trade_log: list[dict],
    config: BacktestConfig,
) -> dict:
    """
    Compute standard performance metrics from equity curve and trade log.

    Returns dict with: cagr, sharpe, max_drawdown, calmar, win_rate,
    total_return, annual_volatility, n_trades, avg_return_per_trade,
    profit_factor.
    """
    if equity_curve.empty or "equity" not in equity_curve.columns:
        return {
            "cagr": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
            "win_rate": 0.0,
            "total_return": 0.0,
            "annual_volatility": 0.0,
            "n_trades": 0,
            "avg_return_per_trade": 0.0,
            "profit_factor": 0.0,
        }

    eq = equity_curve["equity"].values
    dates = equity_curve["date"].values

    # Total return
    initial = config.initial_capital
    final = eq[-1]
    total_return = (final - initial) / initial

    # CAGR
    n_days = len(eq)
    years = n_days / 252.0
    if years > 0 and eq[0] > 0:
        cagr = (final / eq[0]) ** (1.0 / years) - 1.0
    else:
        cagr = 0.0

    # Daily returns
    daily_ret = np.diff(eq) / eq[:-1]
    daily_ret = daily_ret[~np.isnan(daily_ret)]

    # Sharpe (annualised, assume 0 risk-free rate)
    if len(daily_ret) > 1 and daily_ret.std() > 0:
        sharpe = daily_ret.mean() / daily_ret.std() * np.sqrt(252)
    else:
        sharpe = 0.0

    # Max drawdown
    peak = np.maximum.accumulate(eq)
    drawdown = (eq - peak) / peak
    max_drawdown = float(drawdown.min())

    # Calmar ratio
    calmar = cagr / abs(max_drawdown) if abs(max_drawdown) > 0 else 0.0

    # Annual volatility
    annual_vol = float(daily_ret.std() * np.sqrt(252)) if len(daily_ret) > 1 else 0.0

    # Trade statistics
    if trade_log:
        trade_returns = [
            t["pnl_pct"] for t in trade_log if "pnl_pct" in t and t["pnl_pct"] is not None
        ]
        wins = [r for r in trade_returns if r > 0]
        losses = [r for r in trade_returns if r < 0]

        win_rate = len(wins) / len(trade_returns) if trade_returns else 0.0
        avg_return = np.mean(trade_returns) if trade_returns else 0.0
        total_profit = sum(wins) if wins else 0.0
        total_loss = abs(sum(losses)) if losses else 0.0
        profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")
    else:
        win_rate = 0.0
        avg_return = 0.0
        profit_factor = 0.0

    return {
        "cagr": round(float(cagr), 6),
        "sharpe": round(float(sharpe), 4),
        "max_drawdown": round(float(max_drawdown), 6),
        "calmar": round(float(calmar), 4),
        "win_rate": round(float(win_rate), 4),
        "total_return": round(float(total_return), 6),
        "annual_volatility": round(float(annual_vol), 6),
        "n_trades": len(trade_log),
        "avg_return_per_trade": round(float(avg_return), 6),
        "profit_factor": round(float(profit_factor), 4) if profit_factor != float("inf") else None,
    }


def run_backtest(config: BacktestConfig) -> BacktestResult:
    """
    Run a standalone backtest.

    Algorithm:
      1. Load price data for all symbols.
      2. For each rebalance date (spaced by config.rebalance_days trading days):
         a. Use T-1 data to compute factors (anti-bias).
         b. Generate signals from factors.
         c. Execute trades at T+1 open (next trading day open).
         d. Track P&L until next rebalance.
      3. Compute metrics.
    """
    task_id = str(uuid.uuid4())
    result = BacktestResult(task_id)
    result.config_summary = {
        "start_date": config.start_date,
        "end_date": config.end_date,
        "symbols_count": len(config.symbols),
        "rebalance_days": config.rebalance_days,
        "sizing": config.sizing,
        "max_holdings": config.max_holdings,
        "initial_capital": config.initial_capital,
    }

    # 1. Load price data
    price_data: dict[str, pd.DataFrame] = {}
    for sym in config.symbols:
        df = _load_price_data(sym, start_date=config.start_date, end_date=config.end_date)
        if df is not None and not df.empty:
            price_data[sym] = df
    if not price_data:
        logger.error("No price data available — aborting backtest")
        result.metrics = _compute_metrics(pd.DataFrame(), [], config)
        return result

    # Build a multi-symbol price DataFrame: columns = MultiIndex (symbol, field)
    all_dates = set()
    for sym, df in price_data.items():
        all_dates.update(df.index)
    all_dates = sorted(all_dates)

    # Filter to trading days within range
    trading_days = [d for d in all_dates if config.start_date <= d <= config.end_date and is_trading_day(str(d))]
    if not trading_days:
        logger.error("No trading days in range")
        result.metrics = _compute_metrics(pd.DataFrame(), [], config)
        return result

    logger.info(
        "Backtest %s: %d symbols, %d trading days from %s to %s",
        task_id,
        len(price_data),
        len(trading_days),
        trading_days[0],
        trading_days[-1],
    )

    # 2. Determine rebalance schedule
    rebalance_dates = []
    for i, d in enumerate(trading_days):
        if i == 0 or i % config.rebalance_days == 0:
            rebalance_dates.append(d)

    # 3. Initialize portfolio state
    cash = config.initial_capital * (1.0 - config.cash_reserve)
    reserve_cash = config.initial_capital * config.cash_reserve
    positions: dict[str, dict] = {}  # {symbol: {shares, cost_basis, entry_date}}
    equity_curve: list[dict] = []
    trade_log: list[dict] = []

    # 4. Run the simulation day by day
    for day_idx, today in enumerate(trading_days):
        today_str = str(today)

        # --- Mark-to-market existing positions ---
        portfolio_value = cash + reserve_cash
        for sym, pos in positions.items():
            if sym in price_data:
                pdf = price_data[sym]
                if today_str in pdf.index:
                    current_price = float(pdf.loc[today_str, "close"])
                    pos["current_price"] = current_price
                    pos["market_value"] = pos["shares"] * current_price
                    portfolio_value += pos["market_value"]
                else:
                    # Stale position — use last known price
                    portfolio_value += pos["market_value"]

        # Record equity
        equity_curve.append({
            "date": today_str,
            "equity": round(portfolio_value, 2),
            "cash": round(cash, 2),
            "positions_value": round(portfolio_value - cash - reserve_cash, 2),
        })

        # --- Stop-loss check ---
        if config.stop_loss > 0:
            closed_syms = []
            for sym, pos in positions.items():
                loss_pct = (pos["current_price"] - pos["cost_basis"]) / pos["cost_basis"]
                if loss_pct <= -config.stop_loss:
                    # Execute stop-loss at today's close
                    sell_value = pos["market_value"]
                    commission = _apply_commission(sell_value, "sell", config)
                    trade_pnl = sell_value - pos["shares"] * pos["cost_basis"] - commission
                    cash += sell_value - commission
                    trade_log.append({
                        "date": today_str,
                        "symbol": sym,
                        "side": "sell",
                        "signal": "stop_loss",
                        "price": pos["current_price"],
                        "shares": pos["shares"],
                        "value": round(sell_value, 2),
                        "commission": round(commission, 2),
                        "pnl": round(float(trade_pnl), 2),
                        "pnl_pct": round(float(loss_pct), 6),
                        "cost_basis": pos["cost_basis"],
                    })
                    closed_syms.append(sym)
            for sym in closed_syms:
                del positions[sym]

        # --- Rebalance on schedule ---
        if today not in rebalance_dates:
            continue

        # Anti-bias: use T-1 data for decision
        decision_date = prev_trading_day(today_str)
        if decision_date == today_str or decision_date not in trading_days:
            # Can't look back — skip this rebalance
            continue

        # Execution happens at next-day open = today's open (T+1 open)
        # Actually for the first rebalance, we use today's open as execution price
        # The decision_date data influences factor computation

        # Compute factors using data up to decision_date
        factor_panel: dict[str, pd.DataFrame] = {}
        # Build symbol x factor matrix at decision_date
        factor_values_per_symbol: dict[str, pd.Series] = {}

        for sym in config.symbols:
            if sym not in price_data:
                continue
            pdf = price_data[sym]
            # Slice data up to decision_date
            hist = pdf[pdf.index <= decision_date]
            if len(hist) < 60:
                continue  # not enough history for factors

            # Compute all factors the signal config needs
            _fw_keys = list(config.factor_weights.keys()) if config.factor_weights else []
            factor_names: list[str] = _fw_keys if _fw_keys else list(FACTOR_REGISTRY.keys())[:10]

            try:
                factor_df = compute_all_factors(hist, factor_names)
                latest = factor_df.iloc[-1]  # most recent row
                factor_values_per_symbol[sym] = latest
            except Exception:
                logger.debug("Factor computation failed for %s at %s", sym, decision_date)
                continue

        if not factor_values_per_symbol:
            continue

        # Build factor panel for the signal generator
        for fname in factor_names:
            col = {}
            for sym, fv in factor_values_per_symbol.items():
                if fname in fv and not pd.isna(fv[fname]):
                    col[sym] = fv[fname]
            if col:
                factor_panel[fname] = pd.DataFrame(
                    {decision_date: col}
                ).T  # 1-row DataFrame

        # Generate signals
        if config.signal_source == "predefined" and config.signals:
            trade_signals = [
                s for s in config.signals
                if s.get("date") == decision_date or s.get("date") == today_str
            ]
        else:
            sig_config = SignalConfig(
                factor_weights=config.factor_weights,
                top_n_buy=config.max_holdings,
                top_n_sell=0,
                buy_threshold=0.0,
                direction="long_only",
            )
            trade_signals = generate_signals(factor_panel, sig_config, date=decision_date)

        if not trade_signals:
            continue

        # --- Execute trades: sell first (raise cash), then buy ---
        target_symbols = set(
            s["ts_code"] for s in trade_signals if s["signal_type"] == "buy"
        )

        # Sell positions not in target list
        for sym in list(positions.keys()):
            if sym not in target_symbols:
                pos = positions[sym]
                sell_price = None
                if sym in price_data and today_str in price_data[sym].index:
                    sell_price = float(price_data[sym].loc[today_str, "open"])
                if sell_price is None:
                    sell_price = pos["current_price"]

                sell_value = pos["shares"] * sell_price
                commission = _apply_commission(sell_value, "sell", config)
                cash += sell_value - commission

                pnl = sell_value - pos["shares"] * pos["cost_basis"] - commission
                pnl_pct = (sell_price - pos["cost_basis"]) / pos["cost_basis"]

                trade_log.append({
                    "date": today_str,
                    "symbol": sym,
                    "side": "sell",
                    "signal": "rebalance",
                    "price": round(sell_price, 4),
                    "shares": pos["shares"],
                    "value": round(sell_value, 2),
                    "commission": round(commission, 2),
                    "pnl": round(float(pnl), 2),
                    "pnl_pct": round(float(pnl_pct), 6),
                    "cost_basis": pos["cost_basis"],
                })
                del positions[sym]

        # Buy new target positions (equal-weight)
        buy_signals = [s for s in trade_signals if s["signal_type"] == "buy"]
        buy_symbols = [s["ts_code"] for s in buy_signals if s["ts_code"] not in positions]
        n_buy = len(buy_symbols)

        if n_buy > 0 and cash > 0:
            allocs: dict[str, float] = {}
            # Determine sizing
            if config.sizing == "equal_weight":
                allocation_per_symbol: float | None = cash / n_buy
            elif config.sizing == "inverse_volatility":
                # Compute weights proportional to 1/volatility
                vols = {}
                for sym in buy_symbols:
                    if sym in price_data:
                        pdf = price_data[sym]
                        hist = pdf[pdf.index <= decision_date]
                        if len(hist) >= 20:
                            vol = hist["close"].pct_change().rolling(20).std().iloc[-1]
                            vols[sym] = vol if not pd.isna(vol) else None
                inv_vols = {s: 1.0 / v for s, v in vols.items() if v and v > 0}
                total_inv = sum(inv_vols.values())
                if total_inv > 0:
                    allocs = {s: cash * inv_vols[s] / total_inv for s in buy_symbols if s in inv_vols}
                else:
                    allocs = {s: cash / n_buy for s in buy_symbols}
                allocation_per_symbol = None  # dynamic below
            else:
                allocation_per_symbol = cash / n_buy

            for sym in buy_symbols:
                buy_price = None
                if sym in price_data and today_str in price_data[sym].index:
                    buy_price = float(price_data[sym].loc[today_str, "open"])

                if buy_price is None or buy_price <= 0:
                    continue

                if config.sizing == "inverse_volatility":
                    if sym in allocs:
                        alloc: float = allocs[sym]
                    else:
                        continue
                else:
                    assert allocation_per_symbol is not None
                    alloc = allocation_per_symbol

                commission = _apply_commission(alloc, "buy", config)
                spendable = alloc - commission
                shares = int(spendable / buy_price)
                if shares == 0:
                    continue

                actual_cost = shares * buy_price + commission
                cash -= actual_cost
                positions[sym] = {
                    "shares": shares,
                    "cost_basis": buy_price,
                    "current_price": buy_price,
                    "market_value": shares * buy_price,
                    "entry_date": today_str,
                }
                trade_log.append({
                    "date": today_str,
                    "symbol": sym,
                    "side": "buy",
                    "signal": "rebalance",
                    "price": round(buy_price, 4),
                    "shares": shares,
                    "value": round(shares * buy_price, 2),
                    "commission": round(commission, 2),
                    "pnl": None,
                    "pnl_pct": None,
                    "cost_basis": buy_price,
                })

    # --- Final: liquidate all positions at last day close ---
    last_date = trading_days[-1]
    last_date_str = str(last_date)
    for sym in list(positions.keys()):
        pos = positions[sym]
        sell_price = pos["current_price"]
        sell_value = pos["shares"] * sell_price
        commission = _apply_commission(sell_value, "sell", config)
        cash += sell_value - commission

        pnl = sell_value - pos["shares"] * pos["cost_basis"] - commission
        pnl_pct = (sell_price - pos["cost_basis"]) / pos["cost_basis"]

        trade_log.append({
            "date": last_date_str,
            "symbol": sym,
            "side": "sell",
            "signal": "liquidation",
            "price": round(sell_price, 4),
            "shares": pos["shares"],
            "value": round(sell_value, 2),
            "commission": round(commission, 2),
            "pnl": round(float(pnl), 2),
            "pnl_pct": round(float(pnl_pct), 6),
            "cost_basis": pos["cost_basis"],
        })
        del positions[sym]

    # Record final equity
    final_equity = cash + reserve_cash
    equity_curve.append({
        "date": last_date_str,
        "equity": round(final_equity, 2),
        "cash": round(cash, 2),
        "positions_value": 0.0,
    })

    # 5. Compute metrics
    eq_df = pd.DataFrame(equity_curve) if equity_curve else pd.DataFrame()
    metrics = _compute_metrics(eq_df, trade_log, config)

    result.equity_curve = equity_curve
    result.trade_log = trade_log
    result.metrics = metrics

    logger.info(
        "Backtest %s complete: CAGR=%.4f Sharpe=%.4f MaxDD=%.4f Trades=%d",
        task_id,
        metrics["cagr"],
        metrics["sharpe"],
        metrics["max_drawdown"],
        metrics["n_trades"],
    )

    # Store in in-memory cache for retrieval
    _backtest_store[task_id] = result

    return result


# ---------------------------------------------------------------------------
# In-memory result store (production should use Redis or DB)
# ---------------------------------------------------------------------------

_backtest_store: dict[str, BacktestResult] = {}


def get_backtest_result(task_id: str) -> Optional[BacktestResult]:
    """Retrieve a completed backtest result by ID."""
    return _backtest_store.get(task_id)


def list_backtest_results() -> list[dict]:
    """List all completed backtests (summary only)."""
    return [
        {
            "task_id": r.task_id,
            "created_at": r.created_at,
            "metrics": {k: v for k, v in r.metrics.items() if k in ("cagr", "sharpe", "max_drawdown", "n_trades")},
            "config": r.config_summary,
        }
        for r in _backtest_store.values()
    ]
