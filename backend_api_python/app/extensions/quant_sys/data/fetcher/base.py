"""Base fetcher with rate limiting and retry logic."""

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class BaseFetcher:
    """Base class for data fetchers with rate limiting and retry with exponential backoff."""

    def __init__(
        self,
        rate_limit: int = 200,
        max_retries: int = 3,
        retry_delay: float = 5.0,
    ):
        """
        Args:
            rate_limit: Max calls per minute.
            max_retries: Max retry attempts on failure.
            retry_delay: Initial delay (seconds) between retries (doubles each retry).
        """
        self.rate_limit = rate_limit
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._last_call_time: float = 0.0

    def _rate_limit_wait(self) -> None:
        """Wait if necessary to respect the rate limit."""
        if self.rate_limit <= 0:
            return
        min_interval = 60.0 / self.rate_limit
        elapsed = time.time() - self._last_call_time
        if elapsed < min_interval:
            sleep_time = min_interval - elapsed
            logger.debug("Rate limiting: sleeping %.2fs", sleep_time)
            time.sleep(sleep_time)
        self._last_call_time = time.time()

    def _fetch_with_retry(
        self,
        func: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call func(*args, **kwargs) with rate limiting and retry on failure.

        Uses exponential backoff: delay doubles each retry attempt.
        """
        last_exception: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._rate_limit_wait()
            try:
                result = func(*args, **kwargs)
                return result
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries:
                    wait = self.retry_delay * (2 ** attempt)
                    logger.warning(
                        "Attempt %d/%d failed: %s. Retrying in %.1fs...",
                        attempt + 1,
                        self.max_retries + 1,
                        e,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "All %d attempts failed. Last error: %s",
                        self.max_retries + 1,
                        e,
                    )

        raise last_exception  # type: ignore[misc]