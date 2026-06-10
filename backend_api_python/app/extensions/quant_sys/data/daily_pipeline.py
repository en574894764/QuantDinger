"""Daily data pipeline: fetch → validate → store.

Can be called from APScheduler cron or manually via CLI.
"""
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)


class StepTimeout(Exception):
    pass


@contextmanager
def step_timeout(minutes: int, step_name: str):
    """Timeout context manager using threading.Timer (thread-safe for APScheduler)."""
    timeout_seconds = minutes * 60
    timer_exc: list[StepTimeout | None] = [None]
    
    def _on_timeout():
        timer_exc[0] = StepTimeout(f"Step '{step_name}' exceeded {minutes}min timeout")
    
    timer = threading.Timer(timeout_seconds, _on_timeout)
    timer.daemon = True
    timer.start()
    try:
        yield
        if timer_exc[0] is not None:
            raise timer_exc[0]
    finally:
        timer.cancel()


def _get_tushare_token() -> str:
    """Get tushare token: 1) env TUSHARE_TOKEN 2) config YAML file 3) quant_sys_config mount"""
    token = os.environ.get("TUSHARE_TOKEN", "")
    if token:
        return token
    
    # Try reading from the mounted config file
    config_paths = [
        "/quant_sys_config/data_sources.local.yaml",
        "/app/data_sources.local.yaml",
    ]
    for cfg_path in config_paths:
        if os.path.exists(cfg_path):
            try:
                import yaml
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f)
                token = cfg.get("tushare", {}).get("token", "")
                if token:
                    logger.info(f"Loaded tushare token from {cfg_path}")
                    return token
            except Exception as e:
                logger.warning(f"Failed to parse {cfg_path}: {e}")
    
    raise ValueError(
        "Tushare token not found. Set TUSHARE_TOKEN env var or ensure "
        "/quant_sys_config/data_sources.local.yaml is mounted."
    )


def _get_db_connection():
    """Get PostgreSQL connection to investassist."""
    import psycopg2
    db_url = os.environ.get(
        "QUANT_SYS_DATABASE_URL",
        "postgresql://james@host.docker.internal:5432/investassist"
    )
    return psycopg2.connect(db_url)


def _insert_daily_quote(df: pd.DataFrame, conn) -> int:
    """Insert daily quote rows into PG using batch upsert.

    Uses psycopg2.extras.execute_values for fast batch insertion.
    The daily_quote table has a composite PK: (ts_code, trade_year, trade_date).
    """
    from psycopg2.extras import execute_values

    # Build rows: (ts_code, trade_year, trade_date, o, h, l, c, vol, amount, pre_close, change, pct_chg)
    rows = []
    for _, row in df.iterrows():
        trade_date = str(row['trade_date']).replace('-', '')
        trade_year = int(trade_date[:4])
        rows.append((
            str(row['ts_code']),
            trade_year,
            trade_date,
            float(row['open']),
            float(row['high']),
            float(row['low']),
            float(row['close']),
            float(row['vol']),
            float(row['amount']),
            float(row['pre_close']),
            float(row['change']),
            float(row['pct_chg']),
        ))

    cur = conn.cursor()
    execute_values(
        cur,
        """INSERT INTO daily_quote
               (ts_code, trade_year, trade_date, open, high, low, close, vol, amount, pre_close, change, pct_chg)
           VALUES %s
           ON CONFLICT (ts_code, trade_year, trade_date) DO UPDATE SET
             open = EXCLUDED.open,
             high = EXCLUDED.high,
             low = EXCLUDED.low,
             close = EXCLUDED.close,
             vol = EXCLUDED.vol,
             amount = EXCLUDED.amount,
             pre_close = EXCLUDED.pre_close,
             change = EXCLUDED.change,
             pct_chg = EXCLUDED.pct_chg""",
        rows,
        page_size=1000,
    )
    conn.commit()
    return len(rows)


def _insert_etf_quote(df: pd.DataFrame, conn) -> int:
    """Insert ETF daily quote rows into PG using batch upsert.

    ETF table has DATE type for trade_date (needs YYYY-MM-DD format).
    Columns: code, trade_year, trade_date, pre_close, open, high, low, close, change, pct_chg, vol, amount
    """
    from psycopg2.extras import execute_values

    rows = []
    for _, row in df.iterrows():
        td = str(row['trade_date']).replace('-', '')
        trade_year = int(td[:4])
        trade_date_fmt = f"{td[:4]}-{td[4:6]}-{td[6:8]}"  # YYYY-MM-DD for DATE column
        rows.append((
            str(row['ts_code']),
            trade_year,
            trade_date_fmt,
            float(row.get('pre_close', 0)),
            float(row['open']),
            float(row['high']),
            float(row['low']),
            float(row['close']),
            float(row.get('change', 0)),
            float(row.get('pct_chg', 0)),
            float(row['vol']),
            float(row['amount']),
        ))

    cur = conn.cursor()
    try:
        execute_values(
            cur,
            """INSERT INTO etf_quote
                   (code, trade_year, trade_date, pre_close, open, high, low, close, change, pct_chg, vol, amount)
               VALUES %s
               ON CONFLICT (code, trade_date) DO UPDATE SET
                 trade_year = EXCLUDED.trade_year,
                 pre_close = EXCLUDED.pre_close,
                 open = EXCLUDED.open,
                 high = EXCLUDED.high,
                 low = EXCLUDED.low,
                 close = EXCLUDED.close,
                 change = EXCLUDED.change,
                 pct_chg = EXCLUDED.pct_chg,
                 vol = EXCLUDED.vol,
                 amount = EXCLUDED.amount""",
            rows,
            page_size=1000,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def _insert_hk_quote(df: pd.DataFrame, conn) -> int:
    """Insert HK daily quote rows into PG using batch upsert.

    Uses psycopg2.extras.execute_values. hk_quote columns:
    ts_code, trade_date (DATE), open, high, low, close, vol, amount.
    """
    from psycopg2.extras import execute_values

    rows = []
    for _, row in df.iterrows():
        td = str(row['trade_date']).replace('-', '')
        trade_date_fmt = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
        rows.append((
            str(row['ts_code']),
            trade_date_fmt,
            float(row['open']),
            float(row['high']),
            float(row['low']),
            float(row['close']),
            float(row['vol']),
            float(row['amount']),
        ))

    cur = conn.cursor()
    try:
        execute_values(
            cur,
            """INSERT INTO hk_quote
                   (ts_code, trade_date, open, high, low, close, vol, amount)
               VALUES %s
               ON CONFLICT (ts_code, trade_date) DO UPDATE SET
                 open = EXCLUDED.open,
                 high = EXCLUDED.high,
                 low = EXCLUDED.low,
                 close = EXCLUDED.close,
                 vol = EXCLUDED.vol,
                 amount = EXCLUDED.amount""",
            rows,
            page_size=1000,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def run_daily_pipeline(date: str = "", skip_signal: bool = True, skip_hk: bool = False, timeout: int = 45):
    """Run the daily data pipeline.
    
    Steps:
    1. Fetch A-shares daily from tushare → PG + Parquet
    2. Fetch ETF daily from tushare → PG + Parquet
    3. Fetch HK daily from tushare → PG + Parquet (if not skip_hk)
    4. Validate basic quality
    5. Clean and build tradable universe
    6. Save pipeline state
    
    Args:
        date: Trade date YYYYMMDD. Default: today (skips weekends)
        skip_signal: Skip signal generation (default True for Phase 1a)
        skip_hk: Skip HK daily fetch (default False)
        timeout: Per-step timeout in minutes (default 45)
    """
    from app.extensions.quant_sys.data.fetcher.tushare import TushareFetcher
    from app.extensions.quant_sys.data.store.parquet import ParquetStore
    from app.extensions.quant_sys.data.cleaner.clean import DataCleaner
    from app.extensions.quant_sys.data.pipeline_state import PipelineStateManager
    
    if not date:
        today = datetime.now()
        if today.weekday() >= 5:
            logger.info("Weekend — skipping daily pipeline")
            return {"status": "skipped", "reason": "weekend"}
        date = today.strftime("%Y%m%d")
    
    logger.info(f"=== Starting daily pipeline for {date} ===")
    
    token = _get_tushare_token()
    ts = TushareFetcher(token)
    store = ParquetStore()
    state_mgr = PipelineStateManager()
    
    conn = None
    df = pd.DataFrame()
    missing_categories = []
    pipeline_error = None
    pipeline_status = "complete"
    
    try:
        conn = _get_db_connection()
        
        # Step 1: Fetch A-shares daily
        logger.info("STEP 1: Fetching A-shares daily")
        try:
            with step_timeout(timeout, "fetch_a_shares_daily"):
                df = ts.fetch_a_shares_daily(date)
                if df.empty:
                    logger.warning(f"No A-shares daily data for {date}")
                    missing_categories.append("a_shares_daily")
                else:
                    # Write to PG
                    n_pg = _insert_daily_quote(df, conn)
                    logger.info(f"PG daily_quote: {n_pg} rows for {date}")
                    # Write to Parquet
                    path = f"a_shares/daily/date={date}/data.parquet"
                    store.write_raw(df, path)
                    logger.info(f"Parquet: {len(df)} rows for {date}")
        except StepTimeout as e:
            logger.error(str(e))
            missing_categories.append("a_shares_daily")
        except Exception as e:
            logger.error(f"Fetch failed: {e}")
            missing_categories.append("a_shares_daily")
        
        # Step 2: Fetch ETF daily
        logger.info("STEP 2: Fetching ETF daily")
        try:
            with step_timeout(timeout, "fetch_etf_daily"):
                etf_df = ts.fetch_etf_daily(date)
                if etf_df.empty:
                    logger.warning(f"No ETF daily data for {date}")
                    missing_categories.append("etf_daily")
                else:
                    n_pg = _insert_etf_quote(etf_df, conn)
                    logger.info(f"PG etf_quote: {n_pg} rows for {date}")
                    path = f"etf/daily/date={date}/data.parquet"
                    store.write_raw(etf_df, path)
                    logger.info(f"Parquet ETF: {len(etf_df)} rows for {date}")
        except StepTimeout as e:
            logger.error(str(e))
            missing_categories.append("etf_daily")
        except Exception as e:
            logger.error(f"ETF fetch failed: {e}")
            missing_categories.append("etf_daily")
        
        # Step 3: Fetch HK daily
        if not skip_hk:
            logger.info("STEP 3: Fetching HK daily")
            try:
                with step_timeout(timeout, "fetch_hk_data"):
                    hk_df = ts.fetch_hk_daily(date)
                    if hk_df.empty:
                        logger.warning(f"No HK daily data for {date}")
                        missing_categories.append("hk_daily")
                    else:
                        n_pg = _insert_hk_quote(hk_df, conn)
                        logger.info(f"PG hk_quote: {n_pg} rows for {date}")
                        path = f"hk_connect/daily/date={date}/data.parquet"
                        store.write_raw(hk_df, path)
                        logger.info(f"Parquet HK: {len(hk_df)} rows for {date}")
            except StepTimeout as e:
                logger.error(str(e))
                missing_categories.append("hk_daily")
            except Exception as e:
                logger.error(f"HK fetch failed: {e}")
                missing_categories.append("hk_daily")
        else:
            logger.info("STEP 3: Skipping HK daily (skip_hk=True)")
        
        # Step 4: Validate
        logger.info("STEP 4: Validating")
        try:
            with step_timeout(min(15, timeout), "validate"):
                from app.extensions.quant_sys.data.quality.validate import QualityValidator
                validator = QualityValidator()
                val_result = validator.run_all_checks(df, date, expected_min=3000)
                if val_result["status"] == "error":
                    pipeline_status = "failed"
                    pipeline_error = "; ".join(val_result["error_details"])
        except Exception as e:
            logger.warning(f"Validation issue: {e}")
        
        # Step 5: Clean & build universe
        logger.info("STEP 5: Cleaning & building tradable universe")
        try:
            with step_timeout(min(15, timeout), "clean"):
                cleaner = DataCleaner(store)
                cleaned = cleaner.clean_a_shares_daily(date, date)
                tradable = cleaner.build_tradable_universe()
                logger.info(f"Cleaned: {len(cleaned)} rows, Tradable: {len(tradable)} stocks")
        except StepTimeout as e:
            logger.error(str(e))
        except Exception as e:
            logger.warning(f"Cleaner issue: {e}")
    
    except Exception as e:
        pipeline_error = str(e)
        pipeline_status = "failed"
        logger.exception(f"Pipeline failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    
    # --- Degradation evaluation ---
    degradation_result = {}
    try:
        from app.extensions.quant_sys.data.degradation import DegradationManager
        dm = DegradationManager()
        degradation_result = dm.evaluate(missing_categories)
        logger.info(
            "Degradation: level=%s signals=%s sleeves=%s",
            degradation_result.get("level"),
            degradation_result.get("can_generate_signals"),
            degradation_result.get("eligible_sleeves"),
        )
        # Override pipeline_status based on degradation
        if degradation_result.get("level") == "off" and pipeline_status == "complete":
            pipeline_status = "partial"
    except Exception as e:
        logger.warning("Degradation evaluation failed: %s", e)
        degradation_result = {"level": "unknown", "error": str(e)}

    # Save pipeline state (include degradation info)
    state_mgr.save(
        status=pipeline_status,
        trade_date=date,
        missing=missing_categories,
        error=pipeline_error,
    )
    # Inject degradation info into saved state
    try:
        from app.extensions.quant_sys.data.pipeline_state import DEFAULT_STATE_PATH
        import json
        if DEFAULT_STATE_PATH.exists():
            state = json.loads(DEFAULT_STATE_PATH.read_text())
            state["degradation"] = degradation_result
            DEFAULT_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.debug("Failed to write degradation to pipeline state: %s", e)

    logger.info(f"=== Pipeline {pipeline_status} for {date} ===")
    return {
        "status": pipeline_status,
        "date": date,
        "missing": missing_categories,
        "error": pipeline_error,
        "degradation": degradation_result,
    }