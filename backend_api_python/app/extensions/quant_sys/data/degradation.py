"""Pipeline degradation rules — evaluate missing categories → signal eligibility.

Port from quant_sys/src/scheduler/degradation.py.

Evaluates which data categories are missing or stale, then determines the
degradation level and whether signal generation should proceed.

Degradation levels:
    - ``normal``  — All critical data present; full signal generation.
    - ``degraded`` — Some non-critical data missing; signals for available sleeves only.
    - ``minimal``  — Only core data present; conservative signals only.
    - ``off``      — Critical data missing; no signal generation.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class DegradationLevel(str, Enum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    MINIMAL = "minimal"
    OFF = "off"


# ---------------------------------------------------------------------------
# Category definitions
# ---------------------------------------------------------------------------

# Critical categories: pipeline signal generation is OFF if any are missing
CRITICAL_CATEGORIES = frozenset({
    "a_shares_daily",
})

# Important categories: pipeline operates in degraded mode if any are missing
IMPORTANT_CATEGORIES = frozenset({
    "etf_daily",
    "financials",
})

# Optional categories: non-blocking, just logged
OPTIONAL_CATEGORIES = frozenset({
    "hk_daily",
    "macro",
    "index_constituents",
})

# Sleeve data requirements: which sleeves can produce signals given available data
SLEEVE_REQUIREMENTS: dict[str, frozenset[str]] = {
    "A": frozenset({"a_shares_daily"}),
    "B": frozenset({"a_shares_daily", "financials"}),
    "C": frozenset({"a_shares_daily", "financials", "macro"}),
}


# ---------------------------------------------------------------------------
# Degradation manager
# ---------------------------------------------------------------------------


class DegradationManager:
    """Evaluate pipeline degradation based on missing data categories.

    Usage::

        mgr = DegradationManager()
        result = mgr.evaluate(["a_shares_daily"])
        # result["level"] == "normal"

        result = mgr.evaluate(["a_shares_daily", "etf_daily"])
        # result["level"] == "degraded"
    """

    def __init__(
        self,
        critical: frozenset[str] | None = None,
        important: frozenset[str] | None = None,
        optional: frozenset[str] | None = None,
    ) -> None:
        self.critical: frozenset[str] = critical or CRITICAL_CATEGORIES
        self.important: frozenset[str] = important or IMPORTANT_CATEGORIES
        self.optional: frozenset[str] = optional or OPTIONAL_CATEGORIES

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate(self, missing_categories: list[str] | None = None) -> dict[str, Any]:
        """Evaluate degradation level from missing categories.

        Args:
            missing_categories: List of data category names that failed to fetch.

        Returns:
            dict with keys:
                - level: ``DegradationLevel`` value
                - missing_critical: critical categories that are missing
                - missing_important: important categories that are missing
                - missing_optional: optional categories that are missing
                - eligible_sleeves: list of sleeves that can still generate signals
                - can_generate_signals: bool
        """
        missing: set[str] = set(missing_categories or [])

        missing_critical = missing & self.critical
        missing_important = missing & self.important
        missing_optional = missing & self.optional
        unknown = missing - self.critical - self.important - self.optional

        if unknown:
            logger.debug("Unknown missing categories (treated as optional): %s", unknown)

        # Determine level
        if missing_critical:
            level = DegradationLevel.OFF
        elif missing_important:
            level = DegradationLevel.DEGRADED
        elif missing:
            level = DegradationLevel.MINIMAL
        else:
            level = DegradationLevel.NORMAL

        # Determine eligible sleeves
        available = self.critical | self.important | self.optional - missing
        eligible_sleeves = self._eligible_sleeves(available)

        result = {
            "level": level.value,
            "missing_critical": sorted(missing_critical),
            "missing_important": sorted(missing_important),
            "missing_optional": sorted(missing_optional),
            "eligible_sleeves": eligible_sleeves,
            "can_generate_signals": level != DegradationLevel.OFF and len(eligible_sleeves) > 0,
        }

        logger.info(
            "Degradation eval: level=%s critical=%s important=%s sleeves=%s signals=%s",
            level.value,
            sorted(missing_critical),
            sorted(missing_important),
            eligible_sleeves,
            result["can_generate_signals"],
        )

        return result

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _eligible_sleeves(self, available: set[str]) -> list[str]:
        """Return sleeves whose data requirements are fully satisfied."""
        eligible: list[str] = []
        for sleeve, required in SLEEVE_REQUIREMENTS.items():
            if required.issubset(available):
                eligible.append(sleeve)
        return eligible

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def is_normal(self, missing_categories: list[str] | None = None) -> bool:
        """Return True if no degradation."""
        return self.evaluate(missing_categories)["level"] == DegradationLevel.NORMAL

    def is_off(self, missing_categories: list[str] | None = None) -> bool:
        """Return True if signal generation should be disabled."""
        return self.evaluate(missing_categories)["level"] == DegradationLevel.OFF