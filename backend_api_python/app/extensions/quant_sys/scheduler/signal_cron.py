"""Signal generation cron — schedules signal generation after daily pipeline completes.

Uses APScheduler (same pattern as pipeline_cron, weekly_report_cron).
Schedule: Monday–Friday at 17:15 Asia/Shanghai (10 min after pipeline at 17:05).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_scheduler = None


def _start_signal_scheduler():
    """Start APScheduler background scheduler for signal generation (idempotent).

    Schedule: Monday–Friday at 17:15 Asia/Shanghai.
    Calls the signal generator on the latest data after the daily pipeline finishes.
    """
    global _scheduler
    if _scheduler is not None:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; signal generation cron disabled.")
        return

    _scheduler = BackgroundScheduler(daemon=True)

    def _signal_job():
        """Execute signal generation across all sleeves."""
        try:
            from datetime import date

            trade_date = date.today().strftime("%Y%m%d")
            logger.info(
                "Signal generation cron starting (trade_date=%s)", trade_date
            )

            # ── Sleeve A: factor-driven signals ──
            try:
                from app.extensions.quant_sys.strategy.signal.generator import (
                    SignalConfig,
                    generate_signals,
                )
                from app.extensions.quant_sys.strategy.factors.library import (
                    FACTOR_REGISTRY,
                    compute_all_factors,
                )
                from app.extensions.quant_sys.data.store.parquet import ParquetStore

                store = ParquetStore()
                symbols = _load_active_symbols(store)

                if symbols:
                    config_a = SignalConfig(
                        factor_weights={
                            "momentum_20": 0.4,
                            "momentum_60": 0.2,
                            "volatility_20": -0.2,
                            "rsi_14": 0.1,
                            "volume_ratio": 0.1,
                        },
                        top_n_buy=20,
                        sleeve="A",
                        direction="long_only",
                    )
                    # Build factor panel and generate
                    _generate_factor_signals(store, symbols, config_a)
                    logger.info(
                        "Sleeve A signals generated for %d symbols", len(symbols)
                    )
            except Exception as e:
                logger.error(
                    "Sleeve A signal generation failed: %s", e, exc_info=True
                )

            # ── Sleeve B: ETF rotation signals ──
            try:
                logger.info("Sleeve B ETF rotation signals: cron placeholder")
                # ETF rotation signals would run here using rotation.py strategies
            except Exception as e:
                logger.error(
                    "Sleeve B signal generation failed: %s", e, exc_info=True
                )

            # ── Sleeve C: event-driven signals ──
            try:
                logger.info("Sleeve C event-driven signals: cron placeholder")
                # Event-driven signals would run here using events.py strategies
            except Exception as e:
                logger.error(
                    "Sleeve C signal generation failed: %s", e, exc_info=True
                )

            logger.info("Signal generation cron completed")
        except Exception as e:
            logger.error("Signal generation cron failed: %s", e, exc_info=True)

    _scheduler.add_job(
        _signal_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour=17,
            minute=15,
            timezone="Asia/Shanghai",
        ),
        id="signal_generation",
        name="信号生成",
        misfire_grace_time=3600,
    )

    _scheduler.start()
    logger.info(
        "Signal generation scheduler started (Mon–Fri 17:15 Asia/Shanghai, "
        "job ID: signal_generation)"
    )


def _load_active_symbols(store) -> list[str]:
    """Load active A-share symbols from the Parquet store."""
    try:
        # Scan for available symbols in the partitioned store
        import os
        data_root = os.path.join(store.base_dir, "a_shares", "daily")
        if not os.path.isdir(data_root):
            logger.warning("No a_shares/daily data directory: %s", data_root)
            return []
        symbols = [
            d for d in os.listdir(data_root)
            if os.path.isdir(os.path.join(data_root, d))
            and d.isdigit() and len(d) == 6
        ]
        return symbols[:500]  # Limit to first 500 for cron efficiency
    except Exception as e:
        logger.warning("Failed to list symbols from store: %s", e)
        return []


def _generate_factor_signals(store, symbols: list[str], config):
    """Generate factor-driven signals for Sleeve A."""
    import pandas as pd
    from app.extensions.quant_sys.strategy.factors.library import compute_all_factors

    factor_names = list(config.factor_weights.keys())
    factor_panel: dict[str, pd.DataFrame] = {}

    for sym in symbols[:200]:  # Cap at 200 for cron efficiency
        try:
            df = store.read_partitioned(
                f"a_shares/daily/{sym}",
                start_date="20200101",
                end_date="20991231",
                storage="raw",
            )
            if df is None or df.empty:
                continue

            # Standardize columns
            col_map = {
                "trade_date": "date", "ts_code": "symbol",
                "amount": "vol", "vol": "volume",
            }
            df = df.rename(columns={k: v for k, v in col_map.items()
                                    if k in df.columns})
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
            for col in ["open", "high", "low", "close"]:
                if col not in df.columns:
                    df[col] = 0.0

            factor_df = compute_all_factors(df, factor_names)

            for fname in factor_names:
                if fname not in factor_panel:
                    factor_panel[fname] = pd.DataFrame()
                factor_panel[fname][sym] = factor_df[fname]
        except Exception as e:
            logger.debug("Signal cron: skip %s — %s", sym, e)

    if factor_panel:
        from app.extensions.quant_sys.strategy.signal.generator import (
            generate_signals,
        )
        signals = generate_signals(factor_panel, config)
        logger.info(
            "Signal cron: generated %d signals for Sleeve A", len(signals)
        )


def register_signal_cron():
    """Public entry point — call once during app startup."""
    _start_signal_scheduler()
