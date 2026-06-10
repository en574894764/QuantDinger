"""Akshare data fetcher — macroeconomic indicators."""

import logging

import pandas as pd

from .base import BaseFetcher

logger = logging.getLogger(__name__)


class AkshareFetcher(BaseFetcher):
    """Fetcher wrapping akshare for China macro / bond data."""

    def __init__(self):
        super().__init__(rate_limit=50, max_retries=3, retry_delay=3)

    def fetch_shibor(self) -> pd.DataFrame:
        """Fetch SHIBOR all terms data.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            import akshare as ak

            result = self._fetch_with_retry(ak.macro_china_shibor_all)
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_shibor returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_shibor failed: %s", e)
            return pd.DataFrame()

    def fetch_lpr(self) -> pd.DataFrame:
        """Fetch LPR (Loan Prime Rate) data.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            import akshare as ak

            result = self._fetch_with_retry(ak.macro_china_lpr)
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_lpr returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_lpr failed: %s", e)
            return pd.DataFrame()

    def fetch_pmi(self) -> pd.DataFrame:
        """Fetch China PMI data.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            import akshare as ak

            result = self._fetch_with_retry(ak.macro_china_pmi)
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_pmi returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_pmi failed: %s", e)
            return pd.DataFrame()

    def fetch_cpi(self) -> pd.DataFrame:
        """Fetch China CPI monthly data.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            import akshare as ak

            result = self._fetch_with_retry(ak.macro_china_cpi_monthly)
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_cpi returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_cpi failed: %s", e)
            return pd.DataFrame()

    def fetch_money_supply(self) -> pd.DataFrame:
        """Fetch China money supply data.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            import akshare as ak

            result = self._fetch_with_retry(ak.macro_china_money_supply)
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_money_supply returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_money_supply failed: %s", e)
            return pd.DataFrame()

    def fetch_bond_yield_10y(self) -> pd.DataFrame:
        """Fetch China 10-year bond yield.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            import akshare as ak

            result = self._fetch_with_retry(
                lambda: ak.bond_china_yield(start_date="20000101")
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_bond_yield_10y returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_bond_yield_10y failed: %s", e)
            return pd.DataFrame()