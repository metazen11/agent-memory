#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCHER_PID_FILE="$ROOT_DIR/.agent-memory-codex/host-watch.pid"

cleanup() {
  if [[ -f "$WATCHER_PID_FILE" ]]; then
    WATCHER_PID="$(cat "$WATCHER_PID_FILE" 2>/dev/null || true)"
    if [[ -n "${WATCHER_PID:-}" ]]; then
      kill "$WATCHER_PID" >/dev/null 2>&1 || true
    fi
    rm -f "$WATCHER_PID_FILE" >/dev/null 2>&1 || true
  fi
  AGENT_MEMORY_CODEX_HOST_RECOVERY=1 node "$ROOT_DIR/integrations/codex/drain-spool.js" >/dev/null 2>&1 || true
  node "$ROOT_DIR/integrations/codex/session-end.js" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

START_JSON="$(cd "$ROOT_DIR" && AGENT_MEMORY_CODEX_HOST_RECOVERY=1 node integrations/codex/session-start.js)"
SESSION_ID="$(printf '%s' "$START_JSON" | node -e 'let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{try{console.log(JSON.parse(s).session_id||"")}catch{}})')"
CONTEXT_FILE="$(printf '%s' "$START_JSON" | node -e 'let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{try{console.log(JSON.parse(s).context_file||"")}catch{}})')"

export AGENT_MEMORY_SESSION_ID="${SESSION_ID:-}"

(
  cd "$ROOT_DIR"
  AGENT_MEMORY_CODEX_HOST_RECOVERY=1 nohup node integrations/codex/host-watch.js >/dev/null 2>&1 &
) || true

echo "agent-memory session: ${AGENT_MEMORY_SESSION_ID:-unknown}"
echo "agent-memory context: ${CONTEXT_FILE:-$ROOT_DIR/.agent-memory-codex/session-context.md}"
echo "host watcher: running (auto-restart services + drain spool + refresh snapshots)"
echo "tip: open the context file at session start; use pre/post helper scripts for trigger checks and queue capture"

cd "$ROOT_DIR"
codex "$@"
