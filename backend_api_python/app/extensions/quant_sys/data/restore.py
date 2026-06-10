"""Database restore from backup — port of restore_and_verify.sh logic.

Supports:
- PG restore from pg_dump (gzipped)
- SQLite restore from .backup
- Verify row counts post-restore
- Endpoint: POST /api/quant/data/restore?file=
"""

from __future__ import annotations

import gzip
import logging
import os
import subprocess
import sqlite3
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKUP_DIR = Path(os.getenv("QUANT_BACKUP_DIR", "/app/data/backups"))
PG_HOST = os.getenv("PG_BACKUP_HOST", "host.docker.internal")
PG_PORT = os.getenv("PG_BACKUP_PORT", "5432")
PG_USER = os.getenv("PG_BACKUP_USER", "james")
PG_DB = os.getenv("PG_BACKUP_DB", "investassist")
SQLITE_DST = Path(os.getenv("SQLITE_BACKUP_PATH", "/data/system.db"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 600, env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as e:
        return -2, "", str(e)
    except Exception as e:
        return -3, "", str(e)


def _resolve_backup_file(file_param: str, prefix: str = "") -> Path:
    """Resolve a backup file path from query param.

    Args:
        file_param: Filename or relative/absolute path.
        prefix: Optional prefix filter (e.g. ``pg_`` or ``sqlite_``).

    Returns:
        Path to the resolved file.

    Raises:
        FileNotFoundError: If the file doesn't exist.
    """
    # If absolute path
    candidate = Path(file_param)
    if candidate.is_absolute() and candidate.exists():
        return candidate

    # Search in backup directory tree
    search_name = candidate.name
    matches: list[Path] = []
    for mode_dir in sorted(BACKUP_DIR.glob("*")):
        if not mode_dir.is_dir():
            continue
        for f in sorted(mode_dir.glob(f"{prefix}*")):
            if f.name == search_name or f.name.startswith(search_name):
                matches.append(f)

    if not matches:
        raise FileNotFoundError(
            f"Backup file not found: {file_param} "
            f"(searched {BACKUP_DIR}/{{daily,weekly,monthly}}/)"
        )

    # Return newest match
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def _pg_row_count(conn_str: str, table: str) -> int | None:
    """Get row count for a PG table."""
    import psycopg2
    try:
        conn = psycopg2.connect(conn_str)
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.warning("PG row count failed for %s: %s", table, e)
        return None


def _sqlite_row_count(db_path: Path, table: str) -> int | None:
    """Get row count for a SQLite table."""
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.warning("SQLite row count failed for %s: %s", table, e)
        return None


# ---------------------------------------------------------------------------
# Core restore logic
# ---------------------------------------------------------------------------


def restore_pg(file_param: str) -> dict:
    """Restore PostgreSQL from a pg_dump file (.sql or .sql.gz).

    Args:
        file_param: Backup file name or path (relative to BACKUP_DIR).

    Returns:
        dict: Result with success/error and verification info.
    """
    filepath = _resolve_backup_file(file_param, prefix="pg_")
    logger.info("Restoring PG from: %s", filepath)

    conn_str = f"postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/{PG_DB}"
    password = os.getenv("PGPASSWORD", "")

    # Determine if gzipped
    is_gz = filepath.suffix == ".gz"

    try:
        if is_gz:
            # Decompress and pipe to psql
            with gzip.open(filepath, "rb") as f_in:
                proc = subprocess.Popen(
                    ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
                     "--no-password", "-v", "ON_ERROR_STOP=1"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, "PGPASSWORD": password},
                )
                stdout, stderr = proc.communicate(input=f_in.read(), timeout=600)
                rc = proc.returncode
                stdout_text = stdout.decode(errors="replace").strip()
                stderr_text = stderr.decode(errors="replace").strip()
        else:
            rc, stdout_text, stderr_text = _run(
                ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER, "-d", PG_DB,
                 "--no-password", "-v", "ON_ERROR_STOP=1", "-f", str(filepath)],
                timeout=600,
                env_extra={"PGPASSWORD": password} if password else None,
            )

        if rc != 0:
            return {
                "success": False,
                "type": "postgresql",
                "file": str(filepath),
                "error": stderr_text or f"psql returned {rc}",
            }

        # Verify key tables
        tables_to_verify = ["daily_quote", "etf_quote", "hk_quote"]
        verification: dict[str, int | None] = {}
        for table in tables_to_verify:
            verification[table] = _pg_row_count(conn_str, table)

        logger.info("PG restore complete from %s", filepath)
        return {
            "success": True,
            "type": "postgresql",
            "file": str(filepath),
            "row_counts": verification,
        }

    except FileNotFoundError as e:
        return {"success": False, "type": "postgresql", "file": str(filepath), "error": str(e)}
    except subprocess.TimeoutExpired:
        return {"success": False, "type": "postgresql", "file": str(filepath), "error": "timeout"}
    except Exception as e:
        logger.error("PG restore failed: %s", e, exc_info=True)
        return {"success": False, "type": "postgresql", "file": str(filepath), "error": str(e)}


def restore_sqlite(file_param: str) -> dict:
    """Restore SQLite from a .db backup file.

    Args:
        file_param: Backup file name or path (relative to BACKUP_DIR).

    Returns:
        dict: Result with success/error and verification info.
    """
    filepath = _resolve_backup_file(file_param, prefix="sqlite_")
    logger.info("Restoring SQLite from: %s", filepath)

    try:
        # Use sqlite3 .restore or direct file copy
        # .restore is safer: sqlite3 target.db ".restore backup.db"
        rc, stdout, stderr = _run(
            ["sqlite3", str(SQLITE_DST), f".restore '{filepath}'"],
            timeout=120,
        )
        if rc != 0:
            # Fallback: direct file copy
            logger.warning(".restore failed (rc=%d), trying file copy: %s", rc, stderr)
            import shutil
            shutil.copy2(str(filepath), str(SQLITE_DST))

        # Verify key tables
        tables_to_verify = ["positions", "trades", "risk_events", "ideas",
                            "strategy_experiment_log", "daily_snapshots"]
        verification: dict[str, int | None] = {}
        for table in tables_to_verify:
            verification[table] = _sqlite_row_count(SQLITE_DST, table)

        logger.info("SQLite restore complete from %s", filepath)
        return {
            "success": True,
            "type": "sqlite",
            "file": str(filepath),
            "row_counts": verification,
        }

    except Exception as e:
        logger.error("SQLite restore failed: %s", e, exc_info=True)
        return {"success": False, "type": "sqlite", "file": str(filepath), "error": str(e)}


def restore_from_backup(file_param: str, db_type: str = "") -> dict:
    """Restore database from a backup file.

    Args:
        file_param: Backup file name or path.
        db_type: ``pg``, ``sqlite``, or empty (auto-detect).

    Returns:
        dict: Restore result.
    """
    if not file_param:
        return {"success": False, "error": "file parameter is required"}

    # Auto-detect type
    if not db_type:
        fname = Path(file_param).name.lower()
        if fname.startswith("pg_") or fname.endswith(".sql") or fname.endswith(".sql.gz"):
            db_type = "pg"
        elif fname.startswith("sqlite_") or fname.endswith(".db"):
            db_type = "sqlite"
        else:
            return {"success": False, "error": f"Cannot auto-detect DB type from filename: {file_param}. Use ?type=pg|sqlite"}

    if db_type == "pg":
        return restore_pg(file_param)
    elif db_type == "sqlite":
        return restore_sqlite(file_param)
    else:
        return {"success": False, "error": f"Invalid type '{db_type}'. Use 'pg' or 'sqlite'."}