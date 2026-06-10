from __future__ import annotations

import os
import pandas as pd
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default: use the mounted quant_sys data directory in Docker
DEFAULT_DATA_DIR = Path(os.environ.get('QUANT_SYS_DATA_DIR', '/quant_sys_data'))


class ParquetStore:
    """Read and write DataFrames to Parquet files on /quant_sys_data."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.raw = self.data_dir / 'raw'

    def write_raw(self, df: pd.DataFrame, path: str) -> None:
        """Write a DataFrame to a raw Parquet path.

        Args:
            df: DataFrame to persist.
            path: Relative path under self.raw (e.g. 'a_shares/daily/date=20250608/data.parquet').
        """
        full_path = self.raw / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(full_path, index=False)
        logger.debug('Wrote %d rows to %s', len(df), full_path)

    def read_partitioned(
        self,
        base_path: str,
        start_date: str,
        end_date: Optional[str] = None,
        storage: str = 'raw',
    ) -> pd.DataFrame:
        """Read date-partitioned Parquet files within a date range.

        Expects a directory layout like:
            {storage}/a_shares/daily/date=20250101/data.parquet

        Args:
            base_path: Sub-directory under storage (e.g. 'a_shares/daily').
            start_date: Earliest date partition to include (YYYYMMDD).
            end_date: Latest date partition to include (defaults to start_date).
            storage: 'raw' or 'processed'.

        Returns:
            Concatenated DataFrame, or empty DataFrame if no matching files found.
        """
        import glob

        target = self.raw if storage == 'raw' else (self.data_dir / 'processed')
        pattern = str(target / base_path / 'date=*' / 'data.parquet')
        files = sorted(glob.glob(pattern))

        if not files:
            raise FileNotFoundError(f'No files found: {pattern}')

        frames = []
        end = end_date or start_date
        for f in files:
            parts = f.split('/')
            for p in parts:
                if p.startswith('date='):
                    d = p[5:]
                    if start_date <= d <= end:
                        frames.append(pd.read_parquet(f))
                    break

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def get_latest_date(self, base_path: str) -> str | None:
        """Return the most recent date partition for a given base path.

        Args:
            base_path: Sub-directory under raw storage (e.g. 'a_shares/daily').

        Returns:
            YYYYMMDD string or None if no partitions exist.
        """
        import glob

        target = self.raw / base_path
        dirs = sorted(glob.glob(str(target / 'date=*')))
        if not dirs:
            return None
        return Path(dirs[-1]).name.replace('date=', '')