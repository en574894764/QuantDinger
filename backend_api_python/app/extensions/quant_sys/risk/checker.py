"""Risk control checker — independent module with veto power.

Checks every trade signal against position limits, drawdown, stop-loss,
and correlation constraints. Returns pass/block/warn verdicts.
"""

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLite helpers (reuse shared module path)
# ---------------------------------------------------------------------------
_SQLITE_PATH = os.environ.get(
    "QUANT_SYS_SQLITE_PATH", "/quant_sys_data/system.db"
)


def _get_conn(readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{_SQLITE_PATH}?mode=ro" if readonly else _SQLITE_PATH
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# RiskChecker
# ---------------------------------------------------------------------------


class RiskChecker:
    """Stateless risk engine. Configured via env vars, reads position data from
    SQLite, returns verdicts for signals and accounts."""

    def __init__(self):
        # Position‑size limits (fraction of portfolio)
        self.max_position_pct = float(
            os.environ.get("RISK_MAX_POSITION_PCT", 0.25)
        )
        self.max_single_position_pct = float(
            os.environ.get("RISK_MAX_SINGLE_POSITION_PCT", 0.10)
        )
        # Drawdown limits
        self.max_drawdown_pct = float(
            os.environ.get("RISK_MAX_DRAWDOWN_PCT", 0.20)
        )
        self.soft_drawdown_pct = float(
            os.environ.get("RISK_SOFT_DRAWDOWN_PCT", 0.10)
        )
        # Stop‑loss
        self.stop_loss_pct = float(
            os.environ.get("RISK_STOP_LOSS_PCT", 0.08)
        )
        # Correlation cap
        self.max_correlation = float(
            os.environ.get("RISK_MAX_CORRELATION", 0.70)
        )
        # Total position cap
        self.max_total_exposure_pct = float(
            os.environ.get("RISK_MAX_TOTAL_EXPOSURE_PCT", 0.95)
        )

    # ------------------------------------------------------------------
    # Position helpers
    # ------------------------------------------------------------------

    def _load_positions(self) -> list:
        """Load current positions from SQLite daily_snapshots / strategy_state."""
        conn = _get_conn(readonly=True)
        try:
            # Try strategy_state first (most granular)
            rows = conn.execute(
                "SELECT symbol, quantity, avg_cost, current_price, sleeve "
                "FROM strategy_state "
                "WHERE state = 'active' OR state = 'running'"
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]

            # Fallback: read latest snapshot and extract holdings
            snap = conn.execute(
                "SELECT * FROM daily_snapshots ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
            if snap and snap["holdings_json"]:
                import json
                holdings = json.loads(snap["holdings_json"])
                return holdings if isinstance(holdings, list) else []
            return []
        except Exception:
            logger.warning("Could not load positions from SQLite", exc_info=True)
            return []
        finally:
            conn.close()

    def _compute_position_metrics(
        self, positions: list, portfolio_value: float
    ) -> dict:
        """Compute total exposure, per‑symbol weights, and concentration."""
        if not positions or portfolio_value <= 0:
            return {"total_exposure_pct": 0.0, "weights": {}, "num_positions": 0}

        total_mv = 0.0
        weights = {}
        for p in positions:
            qty = float(p.get("quantity", 0))
            price = float(p.get("current_price", 0) or p.get("avg_cost", 0))
            mv = qty * price
            total_mv += mv
            symbol = p.get("symbol", "UNKNOWN")
            weights[symbol] = weights.get(symbol, 0.0) + mv

        total_exposure_pct = total_mv / portfolio_value if portfolio_value > 0 else 0.0
        for sym in weights:
            weights[sym] = weights[sym] / portfolio_value if portfolio_value > 0 else 0.0

        return {
            "total_exposure_pct": min(total_exposure_pct, 1.0),
            "weights": weights,
            "num_positions": len(positions),
        }

    # ------------------------------------------------------------------
    # Signal‑level check
    # ------------------------------------------------------------------

    def check_signal(
        self,
        signal: dict,
        positions: list | None = None,
        portfolio_value: float = 0.0,
    ) -> dict:
        """Check a trade signal against all risk limits.

        Parameters
        ----------
        signal : dict
            Must contain at least: ``symbol``, ``direction`` (buy/sell),
            ``order_size_pct`` (fraction of portfolio), ``sleeve`` (optional).
        positions : list, optional
            Current positions (loaded from DB if omitted).
        portfolio_value : float
            Current total portfolio value.

        Returns
        -------
        dict
            ``{"status": "pass"|"block"|"warn", "reason": str, "checks": [...]}``
        """
        if positions is None:
            positions = self._load_positions()

        symbol = signal.get("symbol", "").upper()
        direction = signal.get("direction", signal.get("side", "buy")).lower()
        order_size_pct = float(signal.get("order_size_pct", 0))
        sleeve = signal.get("sleeve", "default")

        checks = []
        block_reasons = []
        warn_reasons = []

        # --- 1. Position size check ---
        metrics = self._compute_position_metrics(positions, portfolio_value)
        existing_weight = metrics["weights"].get(symbol, 0.0)
        proposed_weight = existing_weight + order_size_pct if direction == "buy" else max(existing_weight - order_size_pct, 0.0)

        if proposed_weight > self.max_single_position_pct:
            block_reasons.append(
                f"Single position {proposed_weight:.1%} > {self.max_single_position_pct:.0%} limit for {symbol}"
            )
            checks.append({"check": "single_position", "status": "block", "detail": f"{proposed_weight:.2%}"})
        elif proposed_weight > self.max_single_position_pct * 0.9:
            warn_reasons.append(f"Single position approaching limit for {symbol}")
            checks.append({"check": "single_position", "status": "warn", "detail": f"{proposed_weight:.2%}"})
        else:
            checks.append({"check": "single_position", "status": "pass", "detail": f"{proposed_weight:.2%}"})

        # --- 2. Total exposure check ---
        total_after = metrics["total_exposure_pct"] + (order_size_pct if direction == "buy" else -order_size_pct)

        if total_after > self.max_total_exposure_pct:
            block_reasons.append(
                f"Total exposure {total_after:.1%} > {self.max_total_exposure_pct:.0%}"
            )
            checks.append({"check": "total_exposure", "status": "block", "detail": f"{total_after:.2%}"})
        elif total_after > self.max_total_exposure_pct * 0.9:
            warn_reasons.append("Total exposure approaching cap")
            checks.append({"check": "total_exposure", "status": "warn", "detail": f"{total_after:.2%}"})
        else:
            checks.append({"check": "total_exposure", "status": "pass", "detail": f"{total_after:.2%}"})

        # --- 3. Position‑level stop‑loss check (for existing positions) ---
        for p in positions:
            if p.get("symbol", "").upper() == symbol:
                cost = float(p.get("avg_cost", 0))
                price = float(p.get("current_price", 0))
                if cost > 0 and price > 0:
                    pnl_pct = (price - cost) / cost
                    if pnl_pct <= -self.stop_loss_pct:
                        block_reasons.append(
                            f"Stop-loss triggered for {symbol}: {pnl_pct:.1%} (limit {self.stop_loss_pct:.0%})"
                        )
                        checks.append({"check": "stop_loss", "status": "block", "detail": f"{pnl_pct:.2%}"})
                    elif pnl_pct <= -self.stop_loss_pct * 0.7:
                        warn_reasons.append(f"Stop-loss approaching for {symbol}")
                        checks.append({"check": "stop_loss", "status": "warn", "detail": f"{pnl_pct:.2%}"})
                    else:
                        checks.append({"check": "stop_loss", "status": "pass", "detail": f"{pnl_pct:.2%}"})

        # --- 4. Correlation check (simplified — sleeve concentration) ---
        sleeve_positions = [p for p in positions if p.get("sleeve") == sleeve]
        if sleeve and len(sleeve_positions) >= 5:
            checks.append({"check": "correlation", "status": "warn", "detail": f"{len(sleeve_positions)} positions in sleeve {sleeve}"})
            warn_reasons.append(f"High concentration in sleeve {sleeve}: {len(sleeve_positions)} symbols")
        else:
            checks.append({"check": "correlation", "status": "pass", "detail": "ok"})

        # --- Determine final status ---
        if block_reasons:
            return {
                "status": "block",
                "reason": "; ".join(block_reasons),
                "checks": checks,
                "block_reasons": block_reasons,
                "warn_reasons": warn_reasons,
            }
        if warn_reasons:
            return {
                "status": "warn",
                "reason": "; ".join(warn_reasons),
                "checks": checks,
                "block_reasons": block_reasons,
                "warn_reasons": warn_reasons,
            }
        return {
            "status": "pass",
            "reason": "All checks passed",
            "checks": checks,
            "block_reasons": [],
            "warn_reasons": [],
        }

    # ------------------------------------------------------------------
    # Account‑level check
    # ------------------------------------------------------------------

    def check_account(
        self, portfolio_value: float, peak_value: float
    ) -> dict:
        """Check account‑level risk: drawdown from peak value.

        Parameters
        ----------
        portfolio_value : float
            Current portfolio value.
        peak_value : float
            All‑time or period peak value.

        Returns
        -------
        dict
            ``{"status": "pass"|"block"|"warn", "reason": str, "drawdown_pct": float}``
        """
        if peak_value <= 0:
            return {
                "status": "pass",
                "reason": "No peak value available",
                "drawdown_pct": 0.0,
                "checks": [{"check": "drawdown", "status": "pass", "detail": "n/a"}],
            }

        drawdown_pct = (peak_value - portfolio_value) / peak_value

        if drawdown_pct >= self.max_drawdown_pct:
            return {
                "status": "block",
                "reason": f"Drawdown {drawdown_pct:.1%} exceeds hard limit {self.max_drawdown_pct:.0%}",
                "drawdown_pct": round(drawdown_pct, 4),
                "peak_value": peak_value,
                "current_value": portfolio_value,
                "checks": [{"check": "drawdown", "status": "block", "detail": f"{drawdown_pct:.2%}"}],
            }
        if drawdown_pct >= self.soft_drawdown_pct:
            return {
                "status": "warn",
                "reason": f"Drawdown {drawdown_pct:.1%} exceeds soft limit {self.soft_drawdown_pct:.0%}",
                "drawdown_pct": round(drawdown_pct, 4),
                "peak_value": peak_value,
                "current_value": portfolio_value,
                "checks": [{"check": "drawdown", "status": "warn", "detail": f"{drawdown_pct:.2%}"}],
            }
        return {
            "status": "pass",
            "reason": "Drawdown within limits",
            "drawdown_pct": round(drawdown_pct, 4),
            "peak_value": peak_value,
            "current_value": portfolio_value,
            "checks": [{"check": "drawdown", "status": "pass", "detail": f"{drawdown_pct:.2%}"}],
        }

    # ------------------------------------------------------------------
    # Convenience: combined signal + account check
    # ------------------------------------------------------------------

    def check_all(
        self,
        signal: dict,
        portfolio_value: float,
        peak_value: float,
        positions: list | None = None,
    ) -> dict:
        """Run both signal and account checks, returning a combined verdict."""
        signal_result = self.check_signal(signal, positions, portfolio_value)
        account_result = self.check_account(portfolio_value, peak_value)

        # If either blocks, overall is blocked
        if signal_result["status"] == "block" or account_result["status"] == "block":
            overall = "block"
        elif signal_result["status"] == "warn" or account_result["status"] == "warn":
            overall = "warn"
        else:
            overall = "pass"

        return {
            "status": overall,
            "signal_check": signal_result,
            "account_check": account_result,
        }
