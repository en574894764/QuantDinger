"""Weekly report generator — markdown report with portfolio, strategy, risk.

Port from quant_sys/src/scheduler/weekly_report.py.
Reads from SQLite /quant_sys_data/system.db.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SQLITE_PATH = os.environ.get(
    "QUANT_SYS_SQLITE_PATH",
    "/quant_sys_data/system.db",
)

REPORT_DIR = Path(os.environ.get(
    "QUANT_WEEKLY_REPORT_DIR",
    "/quant_sys_data/reports",
))


def _get_conn(readonly: bool = True) -> sqlite3.Connection:
    """Get a read-only connection to system.db."""
    uri = f"file:{SQLITE_PATH}?mode=ro" if readonly else SQLITE_PATH
    conn = sqlite3.connect(uri, uri=readonly)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _safe_query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a query and return results as list of dicts, or empty on error."""
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.debug("Query skipped (table may not exist): %s", e)
        return []


def _safe_scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int | float | None:
    """Execute a scalar query; return None on error."""
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug("Scalar query skipped: %s", e)
        return None


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------


def _section_portfolio_summary(conn: sqlite3.Connection) -> str:
    """Portfolio summary: positions, PnL, drawdown."""
    lines: list[str] = ["## 一、组合概况", ""]

    # Latest snapshot
    snaps = _safe_query(
        conn,
        "SELECT * FROM daily_snapshots ORDER BY trade_date DESC LIMIT 1",
    )
    if snaps:
        s = snaps[0]
        lines.append(f"- **最新交易日**: {s.get('trade_date', 'N/A')}")
        lines.append(f"- **总市值**: {s.get('total_value', 'N/A'):,}" if isinstance(s.get('total_value'), (int, float)) else f"- **总市值**: {s.get('total_value', 'N/A')}")
        lines.append(f"- **累计PnL**: {s.get('cumulative_pnl', 'N/A')}")
        lines.append(f"- **最大回撤**: {s.get('max_drawdown', 'N/A')}")
    else:
        lines.append("*(无快照数据)*")

    # Open positions
    pos_count = _safe_scalar(
        conn,
        "SELECT COUNT(*) FROM positions WHERE status = 'open'",
    )
    lines.append(f"- **当前持仓数**: {pos_count or 0}")

    # Positions by sleeve
    pos_by_sleeve = _safe_query(
        conn,
        "SELECT sleeve, COUNT(*) as cnt FROM positions WHERE status = 'open' GROUP BY sleeve",
    )
    if pos_by_sleeve:
        lines.append("- **各Sleeve持仓**:")
        for r in pos_by_sleeve:
            lines.append(f"  - {r['sleeve']}: {r['cnt']}")

    # Recent trades (7 days)
    trades_7d = _safe_scalar(
        conn,
        "SELECT COUNT(*) FROM trades WHERE trade_date >= date('now', '-7 days')",
    )
    lines.append(f"- **近7日交易数**: {trades_7d or 0}")

    lines.append("")
    return "\n".join(lines)


def _section_strategy_performance(conn: sqlite3.Connection) -> str:
    """Strategy experiment performance summary."""
    lines: list[str] = ["## 二、策略表现", ""]

    # Recent experiments
    experiments = _safe_query(
        conn,
        "SELECT * FROM strategy_experiment_log ORDER BY created_at DESC LIMIT 10",
    )
    if not experiments:
        lines.append("*(无策略实验数据)*")
        lines.append("")
        return "\n".join(lines)

    lines.append("| Sleeve | 策略名称 | 夏普 | 年化收益 | 最大回撤 | 胜率 |")
    lines.append("|--------|----------|------|----------|----------|------|")
    for e in experiments:
        sleeve = e.get("sleeve", "-")
        name = (e.get("name") or e.get("id", "-"))[:20]
        sharpe = f"{e['sharpe']:.2f}" if isinstance(e.get("sharpe"), (int, float)) else "-"
        ann_ret = f"{e.get('annual_return', 0) * 100:.1f}%" if isinstance(e.get("annual_return"), (int, float)) else "-"
        max_dd = f"{e.get('max_drawdown', 0) * 100:.1f}%" if isinstance(e.get("max_drawdown"), (int, float)) else "-"
        win_rate = f"{e.get('win_rate', 0) * 100:.1f}%" if isinstance(e.get("win_rate"), (int, float)) else "-"
        lines.append(f"| {sleeve} | {name} | {sharpe} | {ann_ret} | {max_dd} | {win_rate} |")

    # Best Sharpe
    best = _safe_query(
        conn,
        "SELECT id, sleeve, name, sharpe FROM strategy_experiment_log "
        "WHERE sharpe IS NOT NULL ORDER BY sharpe DESC LIMIT 1",
    )
    if best:
        b = best[0]
        lines.append("")
        lines.append(f"🏆 **最佳夏普**: {b.get('sleeve', '-')} / {b.get('name', b.get('id', '-'))} — {b['sharpe']:.2f}")

    lines.append("")
    return "\n".join(lines)


def _section_risk_status(conn: sqlite3.Connection) -> str:
    """Risk status: events, alerts, strategy states."""
    lines: list[str] = ["## 三、风控状态", ""]

    # Active strategy states
    strategy_states = _safe_query(
        conn,
        "SELECT * FROM strategy_state ORDER BY entered_at DESC LIMIT 20",
    )
    if strategy_states:
        running = [s for s in strategy_states if s.get("status") == "running"]
        stopped = [s for s in strategy_states if s.get("status") == "stopped"]
        error = [s for s in strategy_states if s.get("status") == "error"]
        lines.append(f"- **运行中策略**: {len(running)}")
        lines.append(f"- **已停止策略**: {len(stopped)}")
        lines.append(f"- **异常策略**: {len(error)}")
    else:
        lines.append("*(无策略状态数据)*")

    # Recent risk events
    events = _safe_query(
        conn,
        "SELECT * FROM risk_events ORDER BY created_at DESC LIMIT 5",
    )
    if events:
        lines.append("")
        lines.append("### 近期风控事件")
        lines.append("")
        lines.append("| 时间 | 严重级别 | 描述 |")
        lines.append("|------|----------|------|")
        for ev in events:
            ts = str(ev.get("created_at", "-"))[:19]
            severity = ev.get("severity", "-")
            desc = (ev.get("description") or ev.get("message", "-"))[:80]
            lines.append(f"| {ts} | {severity} | {desc} |")
    else:
        lines.append("")
        lines.append("✅ *近期无风控事件*")

    # Active alerts
    alerts = _safe_query(
        conn,
        "SELECT * FROM alerts ORDER BY created_at DESC LIMIT 5",
    )
    if alerts:
        lines.append("")
        lines.append("### 活跃告警")
        lines.append("")
        for a in alerts:
            lines.append(f"- [{a.get('level', 'info').upper()}] {a.get('message', str(a))[:120]}")

    lines.append("")
    return "\n".join(lines)


def _section_ideas(conn: sqlite3.Connection) -> str:
    """Idea Pool summary."""
    lines: list[str] = ["## 四、Idea池", ""]

    total = _safe_scalar(conn, "SELECT COUNT(*) FROM ideas")
    if not total:
        lines.append("*(暂无Idea)*")
        lines.append("")
        return "\n".join(lines)

    by_status = _safe_query(
        conn,
        "SELECT status, COUNT(*) as cnt FROM ideas GROUP BY status",
    )
    lines.append(f"**总计**: {total} 个Idea")
    if by_status:
        for r in by_status:
            lines.append(f"- {r['status']}: {r['cnt']}")

    # Recent ideas
    recent = _safe_query(
        conn,
        "SELECT * FROM ideas ORDER BY created_at DESC LIMIT 5",
    )
    if recent:
        lines.append("")
        lines.append("### 最近提交")
        lines.append("")
        for idea in recent:
            desc = (idea.get("description") or "-")[:60]
            status = idea.get("status", "-")
            market = idea.get("market", "-")
            lines.append(f"- [{status}] {desc} ({market})")

    lines.append("")
    return "\n".join(lines)


def _section_pipeline_status(conn: sqlite3.Connection) -> str:
    """Data pipeline status summary."""
    lines: list[str] = ["## 五、数据管道状态", ""]

    # Read pipeline state from JSON file
    state_path = Path(os.environ.get(
        "PIPELINE_STATE_PATH",
        "/quant_sys_data/pipeline_state.json",
    ))
    if state_path.exists():
        import json
        try:
            state = json.loads(state_path.read_text())
            lines.append(f"- **状态**: {state.get('status', 'unknown')}")
            lines.append(f"- **数据日期**: {state.get('as_of', 'N/A')}")
            lines.append(f"- **更新时间**: {state.get('updated_at', 'N/A')[:19]}")
            missing = state.get("missing", [])
            if missing:
                lines.append(f"- **缺失数据集**: {', '.join(missing)}")
            if state.get("error"):
                lines.append(f"- **错误**: {state['error'][:200]}")
            degradation = state.get("degradation_level")
            if degradation:
                lines.append(f"- **降级级别**: {degradation}")
        except Exception:
            lines.append("*(管道状态文件读取失败)*")
    else:
        lines.append("*(无管道状态文件)*")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report generator
# ---------------------------------------------------------------------------


def generate_weekly_report(report_date: str = "") -> str:
    """Generate a weekly markdown report.

    Args:
        report_date: ISO date string (YYYY-MM-DD). Defaults to today.

    Returns:
        str: Markdown-formatted report.
    """
    if not report_date:
        report_date = datetime.now().strftime("%Y-%m-%d")

    conn = _get_conn()
    try:
        sections: list[str] = []

        # Header
        week_start = (datetime.strptime(report_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        sections.append(f"# QuantDinger 周报")
        sections.append(f"")
        sections.append(f"**报告日期**: {report_date}")
        sections.append(f"**覆盖周期**: {week_start} ~ {report_date}")
        sections.append(f"**生成时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        sections.append("")
        sections.append("---")
        sections.append("")

        # Sections
        sections.append(_section_portfolio_summary(conn))
        sections.append(_section_strategy_performance(conn))
        sections.append(_section_risk_status(conn))
        sections.append(_section_ideas(conn))
        sections.append(_section_pipeline_status(conn))

        # Footer
        sections.append("---")
        sections.append("")
        sections.append(f"*本报告由 QuantDinger 自动生成 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        report = "\n".join(sections)
        logger.info("Weekly report generated for %s (%d chars)", report_date, len(report))
        return report

    finally:
        conn.close()


def save_report(report_date: str = "") -> str:
    """Generate and save the weekly report to disk.

    Returns:
        str: Path to the saved report file.
    """
    report = generate_weekly_report(report_date)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if not report_date:
        report_date = datetime.now().strftime("%Y-%m-%d")

    filename = f"weekly_report_{report_date}.md"
    filepath = REPORT_DIR / filename
    filepath.write_text(report, encoding="utf-8")

    logger.info("Weekly report saved to %s", filepath)
    return str(filepath)
