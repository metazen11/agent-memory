#!/bin/bash
# Scheduled lesson distillation. Mines recurring failures into lessons.
#
# Invoked by launchd (see com.metazen.agent-memory-distill.plist). Runs
# with --apply because the whole point of the schedule is unattended
# lesson creation; every created lesson still passes the validation gate
# in app/lesson_distill.py, and thin/vague/broad-match rules are refused.
#
# Review what it created:
#   curl -s localhost:3377/api/lessons?limit=20 | jq '.[].title'
# Deactivate a bad one:
#   curl -X PATCH localhost:3377/api/lessons/<id> -d '{"active":false}' \
#        -H 'content-type: application/json'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PY="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "FAIL: $PY not found" >&2
    exit 1
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') lesson distillation ==="

# min-occurrences 15 for unattended runs: a stricter bar than the manual
# default (10) because nobody reviews the result before it reaches a
# session. --limit caps LLM calls per run.
exec "$PY" scripts/distill_lessons.py \
    --apply \
    --min-occurrences 15 \
    --lookback-days 90 \
    --limit 5 \
    --quiet
