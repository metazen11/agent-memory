#!/bin/bash
# agent-memory daily pg_dump backup
# Supports both native (Homebrew) and Docker postgres
# Retains last 3 daily backups
#
# Auth resolution order:
#   1. DATABASE_URL (preferred — same DSN the FastAPI server uses)
#   2. POSTGRES_USER + POSTGRES_PASSWORD + POSTGRES_DB + POSTGRES_PORT from .env
#   3. fallback defaults: agentmem / agent_memory / 5432
#
# Install via scripts/install_backup_schedule.sh (macOS launchd) or
# crontab -e on Linux.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_DIR="$PROJECT_DIR/data/backups"

# Load .env if present
[ -f "$PROJECT_DIR/.env" ] && source "$PROJECT_DIR/.env"

CONTAINER="${DOCKER_CONTAINER:-agent-memory-db}"
DB_USER="${POSTGRES_USER:-agentmem}"
DB_NAME="${POSTGRES_DB:-agent_memory}"
DB_PORT="${POSTGRES_PORT:-5432}"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

# Detect backup method: native pg_dump vs Docker
BACKUP_FILE="$BACKUP_DIR/daily_${DATE}.sql.gz"

# Prefer DATABASE_URL when present — that's the canonical DSN the running
# server uses, so it always matches whatever auth path is wired up
# (trust, password, peer, etc.). pg_dump accepts a DSN as its sole arg.
if [ -n "${DATABASE_URL:-}" ] && command -v pg_dump &>/dev/null && pg_isready -p "$DB_PORT" -q 2>/dev/null; then
    echo "[$(date)] Using native pg_dump via DATABASE_URL (port $DB_PORT)"
    pg_dump "$DATABASE_URL" --no-owner --no-acl | gzip > "$BACKUP_FILE"
elif command -v pg_dump &>/dev/null && pg_isready -p "$DB_PORT" -q 2>/dev/null; then
    echo "[$(date)] Using native pg_dump (user=$DB_USER port=$DB_PORT)"
    # Use PGPASSWORD env var if POSTGRES_PASSWORD is set; else rely on
    # trust/peer auth.
    PGPASSWORD="${POSTGRES_PASSWORD:-}" \
        pg_dump -U "$DB_USER" -p "$DB_PORT" -d "$DB_NAME" \
            --no-owner --no-acl | gzip > "$BACKUP_FILE"
elif command -v docker &>/dev/null && docker ps --filter "name=$CONTAINER" --format '{{.Names}}' 2>/dev/null | grep -q "$CONTAINER"; then
    # Docker container fallback
    echo "[$(date)] Using Docker pg_dump (container $CONTAINER)"
    docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-acl | gzip > "$BACKUP_FILE"
else
    echo "[$(date)] ERROR: No postgres available (native port $DB_PORT not ready, container $CONTAINER not running)" >&2
    exit 1
fi

# Refuse to keep an empty backup — pg_dump can silently produce a near-empty
# file on auth failure with --no-fail.
MIN_BYTES=1024
ACTUAL_BYTES=$(stat -f%z "$BACKUP_FILE" 2>/dev/null || stat -c%s "$BACKUP_FILE" 2>/dev/null || echo 0)
if [ "$ACTUAL_BYTES" -lt "$MIN_BYTES" ]; then
    echo "[$(date)] ERROR: backup file is $ACTUAL_BYTES bytes (< $MIN_BYTES). Likely auth failed. Removing." >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

SIZE=$(ls -lh "$BACKUP_FILE" | awk '{print $5}')
echo "[$(date)] Daily backup: $BACKUP_FILE ($SIZE)"

# Rotate: keep last 3 daily backups
ls -t "$BACKUP_DIR"/daily_*.sql.gz 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null || true

echo "[$(date)] Backup complete. Dailies: $(ls "$BACKUP_DIR"/daily_*.sql.gz 2>/dev/null | wc -l | tr -d ' ')"
