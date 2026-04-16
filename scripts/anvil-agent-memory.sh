#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -lt 1 ]]; then
  echo "usage: ./scripts/anvil-agent-memory.sh <anvil-command> [args...]"
  echo "usage: ./scripts/anvil-agent-memory.sh /tool-hints <status|on|off|toggle>"
  echo "example: ./scripts/anvil-agent-memory.sh anvil"
  exit 2
fi

exec node "$ROOT_DIR/scripts/anvil-agent-memory.js" "$@"
