"""Quant System backup API — pg_dump + sqlite3 .backup with daily/weekly/monthly rotation.

Endpoints
---------
POST /api/quant/backup/run?mode=daily|weekly|monthly    Trigger a backup now.
GET  /api/quant/backup/status                            List recent backups.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import jsonify, request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env-var overridable)
# ---------------------------------------------------------------------------
BACKUP_DIR = Path(os.getenv("QUANT_BACKUP_DIR", "/app/data/backups"))
PG_HOST = os.getenv("PG_BACKUP_HOST", "host.docker.internal")
PG_PORT = os.getenv("PG_BACKUP_PORT", "5432")
PG_USER = os.getenv("PG_BACKUP_USER", "james")
PG_DB = os.getenv("PG_BACKUP_DB", "investassist")
SQLITE_SRC = Path(os.getenv("SQLITE_BACKUP_PATH", "/data/system.db"))

RETENTION_DAYS: dict[str, int] = {
    "daily": 3,
    "weekly": 28,
    "monthly": 365,
}

VALID_MODES = frozenset(RETENTION_DAYS)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as e:
        return -2, "", str(e)
    except Exception as e:
        return -3, "", str(e)


def _rotate(target_dir: Path, mode: str) -> int:
    """Delete files older than the retention threshold.  Returns count deleted."""
    retention = RETENTION_DAYS[mode]
    cutoff = time.time() - retention * 86400
    deleted = 0
    for f in target_dir.glob(f"*_{mode}_*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted


def _latest_symlink(target_dir: Path, prefix: str) -> None:
    """Point  target_dir/latest_{prefix}  at the newest matching file."""
    candidates = sorted(target_dir.glob(f"{prefix}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        link = target_dir / f"latest_{prefix}"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(candidates[0].name)


# ---------------------------------------------------------------------------
# Core backup logic
# ---------------------------------------------------------------------------
def run_backup(mode: str = "daily") -> dict:
    """Execute a full backup: pg_dump + sqlite3 .backup → {daily,weekly,monthly}/."""
    if mode not in VALID_MODES:
        return {"success": False, "error": f"Invalid mode '{mode}'. Use: daily, weekly, monthly"}

    target_dir = BACKUP_DIR / mode
    target_dir.mkdir(parents=True, exist_ok=True)

    ts = _timestamp()
    result: dict = {"success": True, "mode": mode, "timestamp": ts, "pg": None, "sqlite": None, "rotated": 0}

    # ---- 1. PostgreSQL ----------------------------------------------------
    pg_file = f"pg_{PG_DB}_{mode}_{ts}.sql.gz"
    pg_path = target_dir / pg_file
    pg_cmd = [
        "pg_dump",
        "-h", PG_HOST,
        "-p", PG_PORT,
        "-U", PG_USER,
        "-d", PG_DB,
        "--no-password",
    ]
    env = {**os.environ, "PGPASSWORD": os.getenv("PGPASSWORD", "")}

    try:
        import gzip as gz
        proc = None
        proc = subprocess.Popen(
            pg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=False,  # binary mode for pg_dump output
        )
        # Stream stdout through gzip to avoid buffering 1G+ in memory
        with gz.open(pg_path, "wb") as f_out:
            if proc.stdout:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    f_out.write(chunk)
        proc.wait(timeout=600)
        stderr_text = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        if proc.returncode == 0:
            size = pg_path.stat().st_size
            result["pg"] = {"file": str(pg_path), "size_bytes": size}
            logger.info("PG dump: %s (%d bytes)", pg_path, size)
        else:
            result["pg"] = {"error": stderr_text or f"pg_dump returned {proc.returncode}"}
            logger.warning("PG dump FAILED (non-fatal): rc=%d stderr=%s", proc.returncode, stderr_text)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
        result["pg"] = {"error": "timeout (600s)"}
        logger.warning("PG dump FAILED: timeout")
        try:
            pg_path.unlink(missing_ok=True)
        except OSError:
            pass
    except FileNotFoundError as e:
        result["pg"] = {"error": f"pg_dump not found: {e}"}
        logger.error("pg_dump not found: %s", e)
    except Exception as e:
        result["pg"] = {"error": str(e)}
        logger.error("PG dump failed: %s", e)

    # ---- 2. SQLite --------------------------------------------------------
    if SQLITE_SRC.exists():
        sqlite_file = f"sqlite_system_{mode}_{ts}.db"
        sqlite_path = target_dir / sqlite_file
        sqlite_cmd = ["sqlite3", str(SQLITE_SRC), f".backup '{sqlite_path}'"]
        rc, stdout, stderr = _run(sqlite_cmd, timeout=120)
        if rc == 0:
            size = sqlite_path.stat().st_size
            result["sqlite"] = {"file": str(sqlite_path), "size_bytes": size}
            logger.info("SQLite backup: %s (%d bytes)", sqlite_path, size)
        else:
            result["sqlite"] = {"error": stderr or f"sqlite3 returned {rc}"}
            logger.warning("SQLite backup FAILED (non-fatal): rc=%d stderr=%s", rc, stderr)
    else:
        logger.info("SQLite: %s not found — skipping", SQLITE_SRC)
        result["sqlite"] = {"skipped": True, "reason": f"{SQLITE_SRC} not found"}

    # ---- 3. Rotate --------------------------------------------------------
    rotated = _rotate(target_dir, mode)
    result["rotated"] = rotated
    if rotated:
        logger.info("Rotated %d old file(s) in %s", rotated, target_dir)

    # ---- 4. Latest symlinks -----------------------------------------------
    _latest_symlink(target_dir, "pg")
    _latest_symlink(target_dir, "sqlite")

    # ---- 5. History log ---------------------------------------------------
    history_line = f"{ts} {mode} pg={result['pg'].get('file', 'FAIL')} sqlite={result['sqlite'].get('file', result['sqlite'].get('skipped', 'FAIL'))}"
    history_log = BACKUP_DIR / "backup_history.log"
    history_log.parent.mkdir(parents=True, exist_ok=True)
    with open(history_log, "a") as f:
        f.write(history_line + "\n")

    result["success"] = True
    return result


# ---------------------------------------------------------------------------
# Cron / scheduler
# ---------------------------------------------------------------------------
_scheduler = None


def _start_scheduler():
    """Start APScheduler background scheduler (idempotent)."""
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; cron backups disabled.")
        return

    _scheduler = BackgroundScheduler(daemon=True)

    _scheduler.add_job(
        lambda: run_backup("daily"),
        CronTrigger(hour=2, minute=0),
        id="backup_daily",
        name="Daily quant backup",
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        lambda: run_backup("weekly"),
        CronTrigger(day_of_week="sun", hour=3, minute=0),
        id="backup_weekly",
        name="Weekly quant backup",
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        lambda: run_backup("monthly"),
        CronTrigger(day=1, hour=4, minute=0),
        id="backup_monthly",
        name="Monthly quant backup",
        misfire_grace_time=3600,
    )

    _scheduler.start()
    logger.info("Backup scheduler started (daily 02:00, weekly Sun 03:00, monthly 1st 04:00)")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
def register_routes(blueprint):
    """Attach backup endpoints to *blueprint* (the quant_sys blueprint)."""

    @blueprint.route("/backup/run", methods=["POST"])
    def backup_run():
        """Trigger a backup.  Query-param ?mode=daily|weekly|monthly (default: daily)."""
        mode = request.args.get("mode", "daily").strip().lower()
        result = run_backup(mode)
        status_code = 200 if result["success"] else 400
        return jsonify(result), status_code

    @blueprint.route("/backup/status")
    def backup_status():
        """List recent backups — scans the backup directory tree."""
        entries: list[dict] = []
        if BACKUP_DIR.exists():
            for mode_dir in sorted(BACKUP_DIR.iterdir()):
                if not mode_dir.is_dir() or mode_dir.name.startswith("."):
                    continue
                for f in sorted(mode_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                    if f.name.startswith("latest_") or f.name == "backup_history.log":
                        continue
                    try:
                        st = f.stat()
                        entries.append({
                            "mode": mode_dir.name,
                            "file": str(f.relative_to(BACKUP_DIR)),
                            "size_bytes": st.st_size,
                            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                        })
                    except OSError:
                        pass

        # Read history log (last 50 lines)
        history: list[str] = []
        history_log = BACKUP_DIR / "backup_history.log"
        if history_log.exists():
            try:
                lines = history_log.read_text().strip().splitlines()
                history = lines[-50:]
            except OSError:
                pass

        return jsonify({
            "backup_dir": str(BACKUP_DIR),
            "files": entries[:100],
            "history": history,
        })

    logger.info("Backup routes registered on %s", blueprint.name)

    # Start the cron scheduler after routes are registered
    _start_scheduler()