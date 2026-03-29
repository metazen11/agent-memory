#!/bin/bash
# agent-memory daily pg_dump backup
# Supports both native (Homebrew) and Docker postgres
# Retains last 3 daily backups
#
# Install: crontab -e → 0 3 * * * /path/to/agentMemory/scripts/backup.sh

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

if command -v pg_dump &>/dev/null && pg_isready -p "$DB_PORT" -q 2>/dev/null; then
    # Native postgres — use local pg_dump directly
    echo "[$(date)] Using native pg_dump (port $DB_PORT)"
    pg_dump -U "$DB_USER" -p "$DB_PORT" -d "$DB_NAME" --no-owner --no-acl | gzip > "$BACKUP_FILE"
elif command -v docker &>/dev/null && docker ps --filter "name=$CONTAINER" --format '{{.Names}}' 2>/dev/null | grep -q "$CONTAINER"; then
    # Docker container fallback
    echo "[$(date)] Using Docker pg_dump (container $CONTAINER)"
    docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-acl | gzip > "$BACKUP_FILE"
else
    echo "[$(date)] ERROR: No postgres available (native port $DB_PORT not ready, container $CONTAINER not running)" >&2
    exit 1
fi

SIZE=$(ls -lh "$BACKUP_FILE" | awk '{print $5}')
echo "[$(date)] Daily backup: $BACKUP_FILE ($SIZE)"

# Rotate: keep last 3 daily backups
ls -t "$BACKUP_DIR"/daily_*.sql.gz 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null || true

echo "[$(date)] Backup complete. Dailies: $(ls "$BACKUP_DIR"/daily_*.sql.gz 2>/dev/null | wc -l | tr -d ' ')"
