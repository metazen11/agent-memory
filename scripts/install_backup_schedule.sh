#!/bin/bash
# Install the agent-memory daily backup schedule.
#
# macOS: launchd job at ~/Library/LaunchAgents/com.metazen.agent-memory-backup.plist
#        runs scripts/backup.sh daily at 03:14 local time.
# Linux: crontab entry (cron not yet implemented here — manual install for now).
#
# Idempotent: re-running rewrites the plist and re-loads the job.
#
# Usage:
#   ./scripts/install_backup_schedule.sh           # install
#   ./scripts/install_backup_schedule.sh --check   # show status
#   ./scripts/install_backup_schedule.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/com.metazen.agent-memory-backup.plist"
LABEL="com.metazen.agent-memory-backup"

case "${1:-}" in
    --check)
        echo "Project dir:   $PROJECT_DIR"
        echo "Backup script: $SCRIPT_DIR/backup.sh"
        echo "Template:      $TEMPLATE"
        if [[ "$OSTYPE" == "darwin"* ]]; then
            TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
            echo "Target plist:  $TARGET"
            if [ -f "$TARGET" ]; then
                echo "Plist installed:    YES"
                # Capture into a variable rather than piping — set -o pipefail
                # turns a closed-pipe grep -q into a script-fatal nonzero.
                JOB_LIST="$(launchctl list 2>/dev/null || true)"
                if echo "$JOB_LIST" | grep -F "${LABEL}" >/dev/null 2>&1; then
                    echo "Job loaded:    YES (waiting for scheduled fire)"
                else
                    echo "Job loaded:    NO"
                fi
            else
                echo "Plist installed:    NO"
            fi
            echo "Recent backups:"
            ls -lht "$PROJECT_DIR/data/backups"/daily_*.sql.gz 2>/dev/null | head -5 \
                || echo "  (none yet)"
        else
            echo "Non-macOS host detected; check crontab -l for entries."
        fi
        exit 0
        ;;
    --uninstall)
        if [[ "$OSTYPE" == "darwin"* ]]; then
            TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
            if [ -f "$TARGET" ]; then
                launchctl bootout "gui/$(id -u)" "$TARGET" 2>/dev/null || true
                rm -f "$TARGET"
                echo "Uninstalled: $TARGET"
            else
                echo "Not installed; nothing to remove."
            fi
        fi
        exit 0
        ;;
esac

# Sanity: the backup script must exist and be executable.
if [ ! -x "$SCRIPT_DIR/backup.sh" ]; then
    echo "ERROR: $SCRIPT_DIR/backup.sh not executable" >&2
    exit 1
fi
if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: template not found: $TEMPLATE" >&2
    exit 1
fi

if [[ "$OSTYPE" == "darwin"* ]]; then
    TARGET_DIR="$HOME/Library/LaunchAgents"
    TARGET="$TARGET_DIR/${LABEL}.plist"
    mkdir -p "$TARGET_DIR" "$HOME/Library/Logs"

    # Render the plist with absolute paths.
    sed \
        -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__HOME__|$HOME|g" \
        "$TEMPLATE" > "$TARGET"

    # Reload the job if it's already loaded.
    launchctl bootout "gui/$(id -u)" "$TARGET" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$TARGET"

    echo "Installed: $TARGET"
    echo "Next run:  daily at 03:14 local time"
    echo
    echo "Inspect:"
    echo "  $0 --check"
    echo "  launchctl list | grep ${LABEL}"
    echo "  tail -f ~/Library/Logs/agent-memory-backup.log"
else
    echo "Non-macOS host — falling back to crontab."
    CRON_LINE="14 3 * * * $SCRIPT_DIR/backup.sh >> $HOME/.agent-memory-backup.log 2>&1"
    if crontab -l 2>/dev/null | grep -F "$SCRIPT_DIR/backup.sh" >/dev/null; then
        echo "crontab entry already present:"
        crontab -l 2>/dev/null | grep -F "$SCRIPT_DIR/backup.sh"
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        echo "crontab entry added:"
        echo "  $CRON_LINE"
    fi
fi
