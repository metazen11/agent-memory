#!/usr/bin/env bash
# Launcher for the agent-memory stdio MCP server.
#
# Plugin loader copies the repo into ~/.claude/plugins/cache/... but skips the
# venv's python symlinks, so $CLAUDE_PLUGIN_ROOT/.venv/bin/python doesn't
# resolve in the cached copy. Resolve the venv via these strategies in order:
#
#   1. $AGENT_MEMORY_VENV_PYTHON      explicit override
#   2. $CLAUDE_PLUGIN_ROOT/.venv      if python binary actually exists there
#   3. Discover via .source-of-truth file written at install time
#   4. Hardcoded fallback to ~/_CODING/agentMemory/.venv
#
# This script is the command field for the agent-memory MCP server in .mcp.json.

set -euo pipefail

CANDIDATES=()

if [[ -n "${AGENT_MEMORY_VENV_PYTHON:-}" ]]; then
  CANDIDATES+=("$AGENT_MEMORY_VENV_PYTHON")
fi

if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
  CANDIDATES+=("$CLAUDE_PLUGIN_ROOT/.venv/bin/python")
fi

# Read source-of-truth pointer if the plugin author wrote one
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" && -f "$CLAUDE_PLUGIN_ROOT/.source-path" ]]; then
  SRC=$(cat "$CLAUDE_PLUGIN_ROOT/.source-path")
  CANDIDATES+=("$SRC/.venv/bin/python")
fi

CANDIDATES+=("$HOME/_CODING/agentMemory/.venv/bin/python")
CANDIDATES+=("$HOME/Dropbox/_CODING/agentMemory/.venv/bin/python")

PYTHON=""
for c in "${CANDIDATES[@]}"; do
  if [[ -x "$c" ]]; then
    PYTHON="$c"
    break
  fi
done

if [[ -z "$PYTHON" ]]; then
  echo "agent-memory: no working python venv found. Tried:" >&2
  printf '  %s\n' "${CANDIDATES[@]}" >&2
  echo "Set AGENT_MEMORY_VENV_PYTHON=/path/to/.venv/bin/python." >&2
  exit 1
fi

# Resolve script: prefer the same dir as this launcher (source repo)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_SCRIPT="$SCRIPT_DIR/../mcp_server.py"

# If the plugin cache doesn't have mcp_server.py (shouldn't happen, it's a plain .py),
# fall back to the source repo.
if [[ ! -f "$MCP_SCRIPT" ]]; then
  MCP_SCRIPT="$HOME/_CODING/agentMemory/mcp_server.py"
fi

exec "$PYTHON" "$MCP_SCRIPT" "$@"
