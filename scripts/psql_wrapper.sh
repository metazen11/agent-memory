#!/usr/bin/env bash
# Safe psql wrapper. Reads DATABASE_URL from .env, never echoes the password.
# Usage:
#   scripts/psql_wrapper.sh -c "SELECT 1"
#   scripts/psql_wrapper.sh -f scripts/migrations/014-normalize-dropbox-paths.sql
#   echo "SELECT 1" | scripts/psql_wrapper.sh
#
# Why: any agent or human running psql in this repo MUST use this wrapper
# so DB credentials never appear in shell history, transcripts, agent
# memory, or chat logs.

set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "FAIL: .env not found in $(pwd)" >&2
    exit 1
fi

# Extract DATABASE_URL (single line, no quotes). Format expected:
# DATABASE_URL=postgresql://user:url-encoded-pass@host:port/db
DB_URL=$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
if [ -z "$DB_URL" ]; then
    echo "FAIL: DATABASE_URL not set in .env" >&2
    exit 1
fi

# psql understands postgres:// URLs natively, including url-encoded passwords.
# Pass DB_URL as the conninfo arg so it never appears in process listings
# (-w disables interactive prompts; -X skips ~/.psqlrc for reproducibility).
exec psql -X -w "$DB_URL" "$@"
