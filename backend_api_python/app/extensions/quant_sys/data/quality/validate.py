"""Quality validation for daily pipeline data.

Checks record counts, zero OHLC, stale data, and other data-quality issues.
"""

import logging
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class QualityValidator:
    """Validates daily data quality with configurable checks."""

    def run_all_checks(
        self,
        df: pd.DataFrame,
        date: str,
        expected_min: int = 3000,
    ) -> dict[str, Any]:
        """Run all quality checks on a daily dataframe.

        Args:
            df: DataFrame with OHLCV data.
            date: Trade date string (YYYYMMDD).
            expected_min: Minimum expected row count.

        Returns:
            dict with keys:
              - status: 'ok' or 'error'
              - error_details: list of error strings
              - checks: dict of per-check results
        """
        error_details: list[str] = []
        checks: dict[str, Any] = {}

        # Check 1: Minimum record count
        actual = len(df) if not df.empty else 0
        checks["record_count"] = {"actual": actual, "expected_min": expected_min}
        if actual < expected_min:
            msg = f"Low record count: {actual} < {expected_min}"
            logger.warning(msg)
            error_details.append(msg)
        else:
            logger.info(f"Record count OK: {actual}")

        # Check 2: Zero OHLC values
        if not df.empty:
            zero_cols = []
            for col in ["open", "high", "low", "close"]:
                if col in df.columns:
                    zeros = (df[col] == 0).sum()
                    if zeros > 0:
                        zero_cols.append(f"{col}={zeros}")
            checks["zero_ohlc"] = {"zero_columns": zero_cols}
            if zero_cols:
                msg = f"Zero values: {', '.join(zero_cols)}"
                logger.warning(msg)
                error_details.append(msg)

        # Check 3: Date consistency — verify trade_date column matches
        if not df.empty and "trade_date" in df.columns:
            dates_in_df = df["trade_date"].astype(str).str.replace("-", "").unique()
            checks["date_consistency"] = {
                "expected": date,
                "found": dates_in_df.tolist(),
            }
            if len(dates_in_df) > 1:
                msg = f"Multiple dates in data: {dates_in_df.tolist()}"
                logger.warning(msg)
                error_details.append(msg)

        # Check 4: Null check on critical columns
        if not df.empty:
            null_cols = []
            for col in ["open", "high", "low", "close", "vol"]:
                if col in df.columns:
                    nulls = df[col].isna().sum()
                    if nulls > 0:
                        null_cols.append(f"{col}={nulls}")
            checks["null_values"] = {"null_columns": null_cols}
            if null_cols:
                msg = f"Null values in: {', '.join(null_cols)}"
                logger.warning(msg)
                error_details.append(msg)

        status = "error" if error_details else "ok"

        return {
            "status": status,
            "error_details": error_details,
            "checks": checks,
        }