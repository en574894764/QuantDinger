"""Data cleaning routines for the QuantDinger pipeline."""

from __future__ import annotations

import pandas as pd
import logging

logger = logging.getLogger(__name__)


class DataCleaner:
    """Cleans raw A-share data and builds tradable universes."""

    def __init__(self, store):
        self.store = store

    def clean_a_shares_daily(
        self, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Read, filter and return a cleaned daily DataFrame.

        Filters: remove ST stocks, zero volume, OHLC <= 0.
        """
        try:
            df = self.store.read_partitioned(
                'a_shares/daily', start_date, end_date
            )
        except FileNotFoundError:
            logger.warning(
                'No daily data found for %s – %s, returning empty DataFrame.',
                start_date, end_date,
            )
            return pd.DataFrame()

        total_before = len(df)
        logger.info('Loaded %d raw daily rows [%s – %s].', total_before, start_date, end_date)

        # 1. ST stocks
        if 'name' in df.columns:
            st_mask = df['name'].str.contains('ST', na=False)
            df = df[~st_mask]
            if st_mask.sum():
                logger.info('Removed %d ST rows.', st_mask.sum())

        # 2. Zero volume
        if 'vol' in df.columns:
            df = df[df['vol'] > 0]
            if (df['vol'] == 0).any():
                logger.info('Removed zero-volume rows.')

        # 3. OHLC > 0
        for col in ['open', 'high', 'low', 'close']:
            if col in df.columns:
                df = df[df[col] > 0]

        total_after = len(df)
        logger.info(
            'Cleaning complete: %d rows → %d rows (%d removed).',
            total_before, total_after, total_before - total_after,
        )
        return df

    def build_tradable_universe(self) -> pd.DataFrame:
        """Build tradable stocks DataFrame from stock_basic Parquet.

        Filters: non-ST, non-delisted. Saves to a_shares/meta/tradable.parquet.
        """
        candidates = [
            self.store.raw / 'a_shares' / 'meta' / 'stock_basic.parquet',
            self.store.raw / 'stock_basic' / 'data.parquet',
        ]
        found = None
        for c in candidates:
            if c.exists():
                found = c
                break

        if found is None:
            logger.warning('stock_basic Parquet not found, returning empty DataFrame.')
            return pd.DataFrame()

        df = pd.read_parquet(found)
        total = len(df)

        # Filter out ST stocks by name
        if 'name' in df.columns:
            st_mask = df['name'].str.contains('ST', na=False)
            df = df[~st_mask]
            logger.info('Removed %d ST stocks by name.', st_mask.sum())

        # Filter out delisted stocks
        if 'delist_date' in df.columns:
            delisted = df['delist_date'].notna().sum()
            df = df[df['delist_date'].isna()]
            if delisted:
                logger.info('Removed %d delisted stocks.', delisted)
        elif 'list_status' in df.columns:
            df = df[df['list_status'] == 'L']

        tradable = df
        logger.info(
            'Tradable universe: %d / %d stocks.',
            len(tradable), total,
        )

        out_path = self.store.raw / 'a_shares' / 'meta' / 'tradable.parquet'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tradable.to_parquet(out_path, index=False)
        logger.debug('Saved tradable universe to %s.', out_path)

        return tradable