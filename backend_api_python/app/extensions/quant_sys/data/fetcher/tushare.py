"""Tushare data fetcher — A-share daily, stock basics, trade calendar, ETF, HK stocks."""

import logging
from typing import Any

import pandas as pd

from .base import BaseFetcher

logger = logging.getLogger(__name__)

# Default fields for daily quote
DAILY_FIELDS = (
    "ts_code,trade_date,open,high,low,close,vol,amount,pre_close,change,pct_chg"
)

# Default fields for stock basic
STOCK_BASIC_FIELDS = (
    "ts_code,symbol,name,area,industry,market,list_status,list_date"
)


class TushareFetcher(BaseFetcher):
    """Fetcher wrapping tushare pro_api with rate limiting and retry."""

    def __init__(self, token: str):
        import tushare as ts

        ts.set_token(token)
        self.pro: Any = ts.pro_api()
        super().__init__(rate_limit=200, max_retries=3, retry_delay=5)

    def fetch_a_shares_daily(self, trade_date: str) -> pd.DataFrame:
        """Fetch daily OHLCV for all A-shares on a given trade_date.

        Args:
            trade_date: Trade date in 'YYYYMMDD' format.

        Returns:
            DataFrame with columns matching DAILY_FIELDS; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.daily,
                ts_code="",
                trade_date=trade_date,
                fields=DAILY_FIELDS,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning(
                    "fetch_a_shares_daily returned empty for %s", trade_date
                )
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_a_shares_daily failed for %s: %s", trade_date, e)
            return pd.DataFrame()

    def fetch_stock_basic(self, list_status: str = "L") -> pd.DataFrame:
        """Fetch stock basic info.

        Args:
            list_status: 'L' = listed, 'D' = delisted, 'P' = paused.

        Returns:
            DataFrame with stock basic fields; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.stock_basic,
                exchange="",
                list_status=list_status,
                fields=STOCK_BASIC_FIELDS,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning(
                    "fetch_stock_basic returned empty (list_status=%s)", list_status
                )
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error(
                "fetch_stock_basic failed (list_status=%s): %s", list_status, e
            )
            return pd.DataFrame()

    def fetch_trade_cal(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Fetch trading calendar for SSE exchange.

        Args:
            start_date: Start date 'YYYYMMDD'.
            end_date: End date 'YYYYMMDD'.

        Returns:
            DataFrame of open trading days; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.trade_cal,
                exchange="SSE",
                start_date=start_date,
                end_date=end_date,
                is_open="1",
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning(
                    "fetch_trade_cal returned empty (%s → %s)", start_date, end_date
                )
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error(
                "fetch_trade_cal failed (%s → %s): %s", start_date, end_date, e
            )
            return pd.DataFrame()

    def fetch_etf_daily(self, trade_date: str) -> pd.DataFrame:
        """Fetch daily ETF data (fund_daily) for a given trade_date.

        Args:
            trade_date: Trade date in 'YYYYMMDD'.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.fund_daily,
                trade_date=trade_date,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_etf_daily returned empty for %s", trade_date)
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_etf_daily failed for %s: %s", trade_date, e)
            return pd.DataFrame()

    def fetch_fund_basic(self, market: str = "E") -> pd.DataFrame:
        """Fetch fund basic info.

        Args:
            market: Market type — 'E' (ETF), 'LOF', etc.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.fund_basic,
                market=market,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning(
                    "fetch_fund_basic returned empty (market=%s)", market
                )
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_fund_basic failed (market=%s): %s", market, e)
            return pd.DataFrame()

    def fetch_hk_daily(self, trade_date: str) -> pd.DataFrame:
        """Fetch daily HK stock data.

        Args:
            trade_date: Trade date in 'YYYYMMDD'.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.hk_daily,
                trade_date=trade_date,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_hk_daily returned empty for %s", trade_date)
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_hk_daily failed for %s: %s", trade_date, e)
            return pd.DataFrame()

    def fetch_hk_basic(self) -> pd.DataFrame:
        """Fetch HK stock basic info.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            result = self._fetch_with_retry(self.pro.hk_basic)
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_hk_basic returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_hk_basic failed: %s", e)
            return pd.DataFrame()

    # ── Financial statement methods ──────────────────────────────────────────

    def fetch_income(
        self,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> pd.DataFrame:
        """Fetch income statement data.

        Args:
            ts_code: Stock code (empty = all).
            start_date: Start date 'YYYYMMDD'.
            end_date: End date 'YYYYMMDD'.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.income,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_income returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_income failed: %s", e)
            return pd.DataFrame()

    def fetch_balance_sheet(
        self,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> pd.DataFrame:
        """Fetch balance sheet data.

        Args:
            ts_code: Stock code (empty = all).
            start_date: Start date 'YYYYMMDD'.
            end_date: End date 'YYYYMMDD'.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.balancesheet,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_balance_sheet returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_balance_sheet failed: %s", e)
            return pd.DataFrame()

    def fetch_cashflow(
        self,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> pd.DataFrame:
        """Fetch cashflow statement data.

        Args:
            ts_code: Stock code (empty = all).
            start_date: Start date 'YYYYMMDD'.
            end_date: End date 'YYYYMMDD'.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.cashflow,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_cashflow returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_cashflow failed: %s", e)
            return pd.DataFrame()

    def fetch_fina_indicator(
        self,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> pd.DataFrame:
        """Fetch financial indicator data (PE, PB, ROE, etc.).

        Args:
            ts_code: Stock code (empty = all).
            start_date: Start date 'YYYYMMDD'.
            end_date: End date 'YYYYMMDD'.

        Returns:
            DataFrame; empty on failure.
        """
        try:
            result = self._fetch_with_retry(
                self.pro.fina_indicator,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )
            if result is None or (hasattr(result, "empty") and result.empty):
                logger.warning("fetch_fina_indicator returned empty")
                return pd.DataFrame()
            return result
        except Exception as e:
            logger.error("fetch_fina_indicator failed: %s", e)
            return pd.DataFrame()