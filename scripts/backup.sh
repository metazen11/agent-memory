#!/bin/bash
# agent-memory daily pg_dump backup
# Saves compressed backups to Dropbox-synced directory
# Retains last 7 daily + last 4 weekly backups
#
# Install: crontab -e → 0 3 * * * /Users/mz/Dropbox/_CODING/agentMemory/scripts/backup.sh

set -euo pipefail

BACKUP_DIR="/Users/mz/Dropbox/_CODING/agentMemory/data/backups"
CONTAINER="agent-memory-db"
DB_USER="agentmem"
DB_NAME="agent_memory"
DATE=$(date +%Y%m%d_%H%M%S)
DAY_OF_WEEK=$(date +%u)  # 1=Monday, 7=Sunday

mkdir -p "$BACKUP_DIR"

# Check container is running
if ! docker ps --filter "name=$CONTAINER" --format '{{.Names}}' | grep -q "$CONTAINER"; then
    echo "[$(date)] ERROR: Container $CONTAINER not running" >&2
    exit 1
fi

# Daily backup (compressed)
BACKUP_FILE="$BACKUP_DIR/daily_${DATE}.sql.gz"
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-acl | gzip > "$BACKUP_FILE"
SIZE=$(ls -lh "$BACKUP_FILE" | awk '{print $5}')
echo "[$(date)] Daily backup: $BACKUP_FILE ($SIZE)"

# Weekly backup on Sundays (copy of daily)
if [ "$DAY_OF_WEEK" = "7" ]; then
    WEEKLY_FILE="$BACKUP_DIR/weekly_${DATE}.sql.gz"
    cp "$BACKUP_FILE" "$WEEKLY_FILE"
    echo "[$(date)] Weekly backup: $WEEKLY_FILE"
fi

# Rotate: keep last 7 daily backups
ls -t "$BACKUP_DIR"/daily_*.sql.gz 2>/dev/null | tail -n +8 | xargs rm -f 2>/dev/null || true

# Rotate: keep last 4 weekly backups
ls -t "$BACKUP_DIR"/weekly_*.sql.gz 2>/dev/null | tail -n +5 | xargs rm -f 2>/dev/null || true

echo "[$(date)] Backup complete. Dailies: $(ls "$BACKUP_DIR"/daily_*.sql.gz 2>/dev/null | wc -l | tr -d ' '), Weeklies: $(ls "$BACKUP_DIR"/weekly_*.sql.gz 2>/dev/null | wc -l | tr -d ' ')"
