"""Macro data access — reads Chinese macro indicators from Parquet files."""

import os
import pandas as pd

MACRO_DIR = os.environ.get(
    "QUANT_SYS_MACRO_DIR",
    "/quant_sys_data/raw/macro",
)


def _read_parquet(indicator: str) -> pd.DataFrame:
    """Read a single macro indicator Parquet file."""
    path = os.path.join(MACRO_DIR, f"{indicator}.parquet")
    if not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_parquet(path)


def get_macro_indicators() -> list:
    """List available Parquet files in the macro directory."""
    if not os.path.isdir(MACRO_DIR):
        return []
    return sorted([
        f.replace(".parquet", "")
        for f in os.listdir(MACRO_DIR)
        if f.endswith(".parquet")
    ])


def get_macro_indicator(indicator: str, limit: int = 200) -> list:
    """Return all rows for an indicator as list of dicts."""
    df = _read_parquet(indicator)
    if df.empty:
        return []
    df = df.sort_values(
        by=[c for c in ("date", "trade_date", "data_date") if c in df.columns],
        ascending=False,
    )
    if limit:
        df = df.head(limit)
    # Convert timestamps to ISO strings
    for col in df.select_dtypes(include=["datetime64", "datetimetz"]).columns:
        df[col] = df[col].dt.strftime("%Y-%m-%d")
    return df.where(pd.notna(df), None).to_dict(orient="records")


def get_macro_latest(indicator: str) -> dict | None:
    """Return the single latest row for an indicator."""
    data = get_macro_indicator(indicator, limit=1)
    return data[0] if data else None
