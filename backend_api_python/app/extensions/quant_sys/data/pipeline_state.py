"""Lightweight pipeline-state tracking stored as JSON on disk."""

from __future__ import annotations

import json
import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(
    os.environ.get('PIPELINE_STATE_PATH', '/quant_sys_data/pipeline_state.json')
)


class PipelineStateManager:
    """Read / write a simple JSON state file for the QuantDinger pipeline."""

    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path or DEFAULT_STATE_PATH

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(
        self,
        status: str,
        trade_date: str,
        missing: list | None = None,
        error: str | None = None,
    ) -> None:
        """Persist the current pipeline state to disk.

        Args:
            status: One of ``'complete'``, ``'partial'``, ``'failed'``.
            trade_date: The as-of trade date (YYYYMMDD).
            missing: Optional list of dataset names that are missing.
            error: Optional error message if status is ``'failed'``.
        """
        state: dict = {
            'status': status,
            'as_of': trade_date,
            'updated_at': datetime.now().isoformat(),
            'missing': missing or [],
            'error': error,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, 'w') as f:
            json.dump(state, f, indent=2)
        logger.info('Pipeline state saved: %s', status)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self) -> dict:
        """Return the current state dict, or a safe default if the file is missing."""
        if not self.state_path.exists():
            return {'status': 'unknown', 'as_of': None}
        with open(self.state_path) as f:
            return json.load(f)

    def get_status(self) -> str:
        """Convenience: return the ``status`` field only."""
        return self.load().get('status', 'unknown')