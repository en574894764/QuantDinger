"""Pre‑market secondary risk check.

Runs before market open: re‑evaluates all pending signals against current
risk limits, positions, and cooldown state. Updates signal states and
audit log accordingly.
"""

import logging
import os
import sqlite3
from datetime import datetime

from app.extensions.quant_sys.risk.checker import RiskChecker
from app.extensions.quant_sys.risk.signals import (
    get_pending_signals,
    pass_pre_check,
    block_signal,
    get_signal_detail,
)
from app.extensions.quant_sys.risk.cooldown import check_cooldown, is_strategy_in_cooldown
from app.extensions.quant_sys.risk.audit import log_event

logger = logging.getLogger(__name__)

_SQLITE_PATH = os.environ.get(
    "QUANT_SYS_SQLITE_PATH", "/quant_sys_data/system.db"
)


def _get_conn(readonly: bool = True) -> sqlite3.Connection:
    uri = f"file:{_SQLITE_PATH}?mode=ro" if readonly else _SQLITE_PATH
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _load_portfolio_snapshot() -> dict:
    """Load latest portfolio value and peak value from daily_snapshots."""
    conn = _get_conn(readonly=True)
    try:
        snap = conn.execute(
            "SELECT * FROM daily_snapshots ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        if not snap:
            return {"portfolio_value": 0.0, "peak_value": 0.0, "trade_date": ""}

        portfolio_value = float(snap.get("total_value", 0) or 0)
        peak_value = float(snap.get("peak_value", 0) or snap.get("total_value", 0) or 0)
        trade_date = snap.get("trade_date", "")

        return {
            "portfolio_value": portfolio_value,
            "peak_value": peak_value,
            "trade_date": trade_date,
        }
    finally:
        conn.close()


def run_pre_market_checks(trade_date: str = "") -> dict:
    """Re‑check all pending signals against current risk state.

    Parameters
    ----------
    trade_date : str
        Target trade date in YYYYMMDD format. Defaults to today.

    Returns
    -------
    dict
        Summary of actions taken.
    """
    if not trade_date:
        trade_date = datetime.utcnow().strftime("%Y%m%d")

    checker = RiskChecker()
    snapshot = _load_portfolio_snapshot()
    portfolio_value = snapshot["portfolio_value"]
    peak_value = snapshot["peak_value"]

    logger.info("Pre‑market check for %s: portfolio=%.2f peak=%.2f",
                 trade_date, portfolio_value, peak_value)

    # 1. Check cooldown state
    cooldown_result = check_cooldown()
    blocked_scopes = set(cooldown_result.get("blocked_scopes", []))

    # 2. Load all pending/confirmed signals
    signals_resp = get_pending_signals(limit=500)
    signals = signals_resp.get("data", [])

    results = {
        "trade_date": trade_date,
        "portfolio_value": portfolio_value,
        "peak_value": peak_value,
        "cooldown_scopes": list(blocked_scopes),
        "total_signals": len(signals),
        "passed": 0,
        "blocked_hard": 0,
        "blocked_soft": 0,
        "skipped_cooldown": 0,
        "errors": 0,
        "details": [],
    }

    for sig in signals:
        sig_id = sig["id"]
        sleeve = sig.get("sleeve", "default")
        scope_key = f"sleeve:{sleeve}"

        detail_entry = {
            "signal_id": sig_id,
            "symbol": sig.get("symbol"),
            "sleeve": sleeve,
            "current_state": sig.get("state"),
            "action": "unknown",
            "reason": "",
        }

        # 3a. Cooldown check — if sleeve or account is in cooldown, skip
        if scope_key in blocked_scopes or "account" in blocked_scopes:
            detail_entry["action"] = "skipped_cooldown"
            detail_entry["reason"] = "Sleeve or account in cooldown"
            results["skipped_cooldown"] += 1
            results["details"].append(detail_entry)
            log_event(
                event_type="pre_market_skip",
                summary=f"Signal {sig_id} skipped: cooldown active",
                severity="info",
                scope=scope_key,
                signal_id=sig_id,
                symbol=sig.get("symbol", ""),
            )
            continue

        # 3b. Full risk check
        try:
            # Reconstruct signal dict for checker
            signal_for_check = {
                "symbol": sig.get("symbol", ""),
                "direction": sig.get("direction", "buy"),
                "order_size_pct": sig.get("order_size_pct", 0),
                "sleeve": sig.get("sleeve", "default"),
            }

            # Also check account-level drawdown
            account_check = checker.check_account(portfolio_value, peak_value)
            signal_check = checker.check_signal(signal_for_check, portfolio_value=portfolio_value)

            # Determine outcome
            if account_check["status"] == "block":
                block_signal(sig_id, hard=True, reason=account_check["reason"])
                detail_entry["action"] = "blocked_hard"
                detail_entry["reason"] = account_check["reason"]
                results["blocked_hard"] += 1
                log_event(
                    event_type="pre_market_block",
                    summary=f"Signal {sig_id} blocked: {account_check['reason']}",
                    severity="high",
                    scope=scope_key,
                    signal_id=sig_id,
                    symbol=sig.get("symbol", ""),
                    detail=account_check,
                )
            elif signal_check["status"] == "block":
                block_signal(sig_id, hard=True, reason=signal_check["reason"])
                detail_entry["action"] = "blocked_hard"
                detail_entry["reason"] = signal_check["reason"]
                results["blocked_hard"] += 1
                log_event(
                    event_type="pre_market_block",
                    summary=f"Signal {sig_id} blocked: {signal_check['reason']}",
                    severity="high",
                    scope=scope_key,
                    signal_id=sig_id,
                    symbol=sig.get("symbol", ""),
                    detail=signal_check,
                )
            elif signal_check["status"] == "warn":
                # Soft block: still let through but flag
                block_signal(sig_id, hard=False, reason=signal_check["reason"])
                detail_entry["action"] = "blocked_soft"
                detail_entry["reason"] = signal_check["reason"]
                results["blocked_soft"] += 1
                log_event(
                    event_type="pre_market_warn",
                    summary=f"Signal {sig_id} soft‑blocked: {signal_check['reason']}",
                    severity="medium",
                    scope=scope_key,
                    signal_id=sig_id,
                    symbol=sig.get("symbol", ""),
                    detail=signal_check,
                )
            else:
                # Passed risk check → promote to pre_check_passed
                if sig.get("state") in ("pending", "confirmed"):
                    pass_pre_check(sig_id)
                detail_entry["action"] = "passed"
                detail_entry["reason"] = "All risk checks passed"
                results["passed"] += 1
                log_event(
                    event_type="pre_market_pass",
                    summary=f"Signal {sig_id} passed pre‑market check",
                    severity="info",
                    scope=scope_key,
                    signal_id=sig_id,
                    symbol=sig.get("symbol", ""),
                )

        except Exception:
            logger.exception("Pre‑market check failed for signal %s", sig_id)
            detail_entry["action"] = "error"
            detail_entry["reason"] = "Exception during check"
            results["errors"] += 1

        results["details"].append(detail_entry)

    logger.info("Pre‑market complete: %d passed, %d blocked (hard), "
                 "%d blocked (soft), %d skipped (cooldown), %d errors",
                 results["passed"], results["blocked_hard"],
                 results["blocked_soft"], results["skipped_cooldown"],
                 results["errors"])

    return {"success": True, **results}
