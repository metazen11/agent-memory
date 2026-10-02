#!/bin/bash
# Install the agent-memory lesson distillation schedule.
#
# macOS: launchd job at ~/Library/LaunchAgents/com.metazen.agent-memory-distill.plist
#        runs scripts/distill-lessons.sh weekly (Sunday 04:07 local).
# Linux: crontab entry (cron not yet implemented here — manual install for now).
#
# Idempotent: re-running rewrites the plist and re-loads the job.
#
# Usage:
#   ./scripts/install_distill_schedule.sh           # install
#   ./scripts/install_distill_schedule.sh --check   # show status
#   ./scripts/install_distill_schedule.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/com.metazen.agent-memory-distill.plist"
LABEL="com.metazen.agent-memory-distill"

case "${1:-}" in
    --check)
        echo "Project dir:   $PROJECT_DIR"
        echo "Distill script: $SCRIPT_DIR/distill-lessons.sh"
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
            echo "Recent distillation log:"
            tail -20 "$HOME/Library/Logs/agent-memory-distill.log" 2>/dev/null \
                || echo "  (no runs yet)"
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
if [ ! -x "$SCRIPT_DIR/distill-lessons.sh" ]; then
    echo "ERROR: $SCRIPT_DIR/distill-lessons.sh not executable" >&2
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
    echo "Next run:  weekly (Sunday 04:07 local)"
    echo
    echo "Inspect:"
    echo "  $0 --check"
    echo "  launchctl list | grep ${LABEL}"
    echo "  tail -f ~/Library/Logs/agent-memory-distill.log"
else
    echo "Non-macOS host — falling back to crontab."
    CRON_LINE="7 4 * * 0 $SCRIPT_DIR/distill-lessons.sh >> $HOME/.agent-memory-distill.log 2>&1"
    if crontab -l 2>/dev/null | grep -F "$SCRIPT_DIR/distill-lessons.sh" >/dev/null; then
        echo "crontab entry already present:"
        crontab -l 2>/dev/null | grep -F "$SCRIPT_DIR/distill-lessons.sh"
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
        echo "crontab entry added:"
        echo "  $CRON_LINE"
    fi
fi
