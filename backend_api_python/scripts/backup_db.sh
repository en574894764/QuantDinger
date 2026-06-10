#!/bin/bash
# quant_sys backup — daily full backup of authoritative storage layers only:
#   1. PostgreSQL (pg_dump) — ~1.3G — authoritative market data
#   2. SQLite (.backup)     — ~108K — operational state (signals/positions/risk)
#
# Parquet is NOT backed up — it is a derivative cache rebuilt from PG on restore.
#
# Usage: bash scripts/backup_db.sh [daily|weekly|monthly]
#
# Retention:
#   daily:   3 days
#   weekly:  4 weeks (kept on Sunday)
#   monthly: 12 months (kept on 1st)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="$PROJECT_DIR/backups"
DATA_DIR="$PROJECT_DIR/data"

DB_NAME="${DB_NAME:-investassist}"
DB_HOST="${DB_HOST:-localhost}"
DB_USER="${DB_USER:-james}"
SQLITE_DB="$DATA_DIR/system.db"

MODE="${1:-daily}"

case "$MODE" in
    daily)   SUBDIR="daily";   RETENTION_DAYS=3;;
    weekly)  SUBDIR="weekly";  RETENTION_DAYS=28;;
    monthly) SUBDIR="monthly"; RETENTION_DAYS=365;;
    *)       echo "Usage: $0 [daily|weekly|monthly]"; exit 1;;
esac

TARGET_DIR="$BACKUP_DIR/$SUBDIR"
mkdir -p "$TARGET_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
echo "[$(date -Iseconds)] Starting $MODE backup (retention: ${RETENTION_DAYS}d)"

# ═══════════════════════════════════════════════════════════════════
# 1. PostgreSQL
# ═══════════════════════════════════════════════════════════════════
PG_FILE="pg_${DB_NAME}_${MODE}_${TIMESTAMP}.sql.gz"
PG_PATH="$TARGET_DIR/$PG_FILE"
if pg_dump -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" 2>/dev/null | gzip > "$PG_PATH"; then
    echo "[$(date -Iseconds)] PG:  $PG_PATH ($(du -h "$PG_PATH" | cut -f1))"
else
    echo "[$(date -Iseconds)] PG:  FAILED (non-fatal)"
    PG_PATH=""
fi

# ═══════════════════════════════════════════════════════════════════
# 2. SQLite
# ═══════════════════════════════════════════════════════════════════
SQLITE_FILE="sqlite_system_${MODE}_${TIMESTAMP}.db"
SQLITE_PATH="$TARGET_DIR/$SQLITE_FILE"
if [ -f "$SQLITE_DB" ]; then
    if sqlite3 "$SQLITE_DB" ".backup '$SQLITE_PATH'" 2>/dev/null; then
        echo "[$(date -Iseconds)] SQLite: $SQLITE_PATH ($(du -h "$SQLITE_PATH" | cut -f1))"
    else
        echo "[$(date -Iseconds)] SQLite: FAILED (non-fatal)"
        SQLITE_PATH=""
    fi
else
    echo "[$(date -Iseconds)] SQLite: no system.db — skipping"
    SQLITE_PATH=""
fi

# ═══════════════════════════════════════════════════════════════════
# Rotate
# ═══════════════════════════════════════════════════════════════════
DELETED=$(find "$TARGET_DIR" -name "*_${MODE}_*" -mtime "+${RETENTION_DAYS}" -delete -print 2>/dev/null | wc -l | tr -d ' ')
if [ "$DELETED" -gt 0 ]; then
    echo "[$(date -Iseconds)] Rotated $DELETED old file(s)"
fi

# Latest symlinks
for prefix in pg sqlite; do
    latest=$(ls -t "$TARGET_DIR/${prefix}_"* 2>/dev/null | head -1) || true
    [ -n "$latest" ] && ln -sf "$latest" "$TARGET_DIR/latest_${prefix}"
done

# Log
echo "$TIMESTAMP $MODE pg=${PG_PATH:-FAIL} sqlite=${SQLITE_PATH:-FAIL}" >> "$BACKUP_DIR/backup_history.log"
echo "[$(date -Iseconds)] Done"
