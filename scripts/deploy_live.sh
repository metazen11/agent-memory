#!/usr/bin/env bash
# deploy_live.sh — keep the live checkout on origin/main.
#
# Run every 5 minutes by launchd (scripts/install_deploy_schedule.py).
# Idempotent: a no-op when the live checkout already equals origin/main.
# On a new main: detach-checkout it, restart the server, wait for readiness via
# scripts/wait_for_health.py (single source of the health URL/criteria); if it
# never comes up, restore the previous SHA, restart, exit 1.
#
# Env (all optional): LIVE_DIR, HEALTH_URL, HEALTH_TIMEOUT (s), LAUNCHD_LABEL
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE_DIR="${LIVE_DIR:-$HOME/_CODING/agentMemory-live}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
LABEL="${LAUNCHD_LABEL:-com.metazen.agent-memory-server}"
LOG="$HOME/.agent-memory/logs/deploy.log"
mkdir -p "$(dirname "$LOG")"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG"; }
restart() { launchctl kickstart -k "gui/$(id -u)/$LABEL"; }
healthy() {
    python3 "$SCRIPT_DIR/wait_for_health.py" --timeout "$HEALTH_TIMEOUT" \
        ${HEALTH_URL:+--url "$HEALTH_URL"} >/dev/null 2>&1
}

cd "$LIVE_DIR"
git fetch -q origin
PREV="$(git rev-parse HEAD)"
NEW="$(git rev-parse origin/main)"
[ "$PREV" = "$NEW" ] && exit 0

log "DEPLOY start $PREV -> $NEW"
git checkout -q --detach "$NEW"
restart

if healthy; then
    log "DEPLOY ok before=$PREV after=$NEW"
    exit 0
fi

log "ROLLBACK health failed after ${HEALTH_TIMEOUT}s; before=$PREV attempted=$NEW"
git checkout -q --detach "$PREV"
restart
log "ROLLBACK done now=$(git rev-parse HEAD)"
echo "deploy_live: $NEW unhealthy, rolled back to $PREV" >&2
exit 1
