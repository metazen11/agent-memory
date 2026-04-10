#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
ANVIL_ENV_FILE="$ROOT_DIR/.anvil/agent-memory.env"

# Load project env first, then Anvil-specific overrides.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi
if [[ -f "$ANVIL_ENV_FILE" ]]; then
  set -a
  source "$ANVIL_ENV_FILE"
  set +a
fi

if [[ $# -lt 1 ]]; then
  echo "usage: ./scripts/anvil-agent-memory.sh <anvil-command> [args...]"
  echo "example: ./scripts/anvil-agent-memory.sh anvil"
  exit 2
fi

exec "$@"
