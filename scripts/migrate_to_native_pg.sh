#!/bin/bash
# ─────────────────────────────────────────────────────────────
# migrate_to_native_pg.sh — Migrate from Docker Postgres to native Homebrew
#
# Idempotent: every step checks preconditions, skips if already done.
# Safe to re-run at any time.
#
# Usage:
#   bash scripts/migrate_to_native_pg.sh
#   bash scripts/migrate_to_native_pg.sh --dry-run
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
skip() { echo -e "  ${YELLOW}-${NC}  $1 (already done)"; }
fail() { echo -e "  ${RED}✗${NC}  $1"; }
head() { echo -e "\n${BOLD}  $1${NC}"; }
info() { echo -e "     $1"; }

SUMMARY=()

track() {
  SUMMARY+=("$1")
}

# Load .env for current config
[ -f "$PROJECT_DIR/.env" ] && source "$PROJECT_DIR/.env"

DB_USER="${POSTGRES_USER:-agentmem}"
DB_NAME="${POSTGRES_DB:-agent_memory}"
DOCKER_CONTAINER="${DOCKER_CONTAINER:-agent-memory-db}"
NATIVE_PORT=5432

echo ""
echo -e "${BOLD}  ╔══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}  ║  Migrate to native Homebrew PostgreSQL       ║${NC}"
echo -e "${BOLD}  ╚══════════════════════════════════════════════╝${NC}"
echo ""
$DRY_RUN && echo -e "  ${YELLOW}DRY RUN — no changes will be made${NC}\n"

# ── Step 1: Install Homebrew Postgres ────────────────────────

head "Step 1: Homebrew PostgreSQL 16"

if brew list postgresql@16 &>/dev/null; then
  skip "postgresql@16 already installed"
  track "postgresql@16: skip (installed)"
else
  if $DRY_RUN; then
    info "Would run: brew install postgresql@16"
    track "postgresql@16: would install"
  else
    info "Installing postgresql@16..."
    brew install postgresql@16
    ok "postgresql@16 installed"
    track "postgresql@16: installed"
  fi
fi

# Ensure pg tools are on PATH
PG_BIN="$(brew --prefix postgresql@16)/bin"
export PATH="$PG_BIN:$PATH"

# pgvector extension
if brew list pgvector &>/dev/null; then
  skip "pgvector already installed"
  track "pgvector: skip (installed)"
else
  if $DRY_RUN; then
    info "Would run: brew install pgvector"
    track "pgvector: would install"
  else
    info "Installing pgvector..."
    brew install pgvector
    ok "pgvector installed"
    track "pgvector: installed"
  fi
fi

# ── Step 2: Start Postgres service ───────────────────────────

head "Step 2: PostgreSQL service"

if pg_isready -p $NATIVE_PORT -q 2>/dev/null; then
  skip "PostgreSQL already running on port $NATIVE_PORT"
  track "service: skip (running)"
else
  if $DRY_RUN; then
    info "Would run: brew services start postgresql@16"
    track "service: would start"
  else
    info "Starting postgresql@16 service..."
    brew services start postgresql@16
    # Wait for ready
    for i in $(seq 1 15); do
      if pg_isready -p $NATIVE_PORT -q 2>/dev/null; then
        break
      fi
      sleep 1
    done
    if pg_isready -p $NATIVE_PORT -q 2>/dev/null; then
      ok "PostgreSQL running on port $NATIVE_PORT"
      track "service: started"
    else
      fail "PostgreSQL did not start within 15s"
      exit 1
    fi
  fi
fi

# ── Step 3: Create role ──────────────────────────────────────

head "Step 3: Database role '$DB_USER'"

ROLE_EXISTS=$(psql -p $NATIVE_PORT -U "$(whoami)" -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" postgres 2>/dev/null || echo "")

if [[ "$ROLE_EXISTS" == "1" ]]; then
  skip "Role '$DB_USER' already exists"
  track "role: skip (exists)"
else
  if $DRY_RUN; then
    info "Would create role '$DB_USER' with CREATEDB"
    track "role: would create"
  else
    psql -p $NATIVE_PORT -U "$(whoami)" -c "CREATE ROLE $DB_USER WITH LOGIN CREATEDB;" postgres
    ok "Created role '$DB_USER'"
    track "role: created"
  fi
fi

# ── Step 4: Create database ──────────────────────────────────

head "Step 4: Database '$DB_NAME'"

DB_EXISTS=$(psql -p $NATIVE_PORT -U "$(whoami)" -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" postgres 2>/dev/null || echo "")

if [[ "$DB_EXISTS" == "1" ]]; then
  skip "Database '$DB_NAME' already exists"
  track "database: skip (exists)"
else
  if $DRY_RUN; then
    info "Would create database '$DB_NAME' owned by '$DB_USER'"
    track "database: would create"
  else
    psql -p $NATIVE_PORT -U "$(whoami)" -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" postgres
    ok "Created database '$DB_NAME'"
    track "database: created"
  fi
fi

# ── Step 5: Enable pgvector extension ────────────────────────

head "Step 5: pgvector extension"

EXT_EXISTS=$(psql -p $NATIVE_PORT -U "$(whoami)" -tAc "SELECT 1 FROM pg_extension WHERE extname='vector'" "$DB_NAME" 2>/dev/null || echo "")

if [[ "$EXT_EXISTS" == "1" ]]; then
  skip "vector extension already enabled"
  track "pgvector ext: skip (enabled)"
else
  if $DRY_RUN; then
    info "Would run: CREATE EXTENSION IF NOT EXISTS vector"
    track "pgvector ext: would enable"
  else
    psql -p $NATIVE_PORT -U "$(whoami)" -c "CREATE EXTENSION IF NOT EXISTS vector;" "$DB_NAME"
    ok "Enabled vector extension"
    track "pgvector ext: enabled"
  fi
fi

# ── Step 6: Import data from Docker ─────────────────────────

head "Step 6: Data import from Docker"

# Check if native DB already has data
NATIVE_COUNT=$(psql -p $NATIVE_PORT -U "$DB_USER" -tAc "SELECT count(*) FROM mem_observations" "$DB_NAME" 2>/dev/null || echo "0")
NATIVE_COUNT=$(echo "$NATIVE_COUNT" | tr -d ' ')

if [[ "$NATIVE_COUNT" -gt "0" ]]; then
  skip "Native DB already has $NATIVE_COUNT observations"
  track "import: skip ($NATIVE_COUNT rows)"
else
  # Check if Docker container is available
  DOCKER_RUNNING=false
  if command -v docker &>/dev/null && docker ps --filter "name=$DOCKER_CONTAINER" --format '{{.Names}}' 2>/dev/null | grep -q "$DOCKER_CONTAINER"; then
    DOCKER_RUNNING=true
  fi

  if $DOCKER_RUNNING; then
    DOCKER_COUNT=$(docker exec "$DOCKER_CONTAINER" psql -U "$DB_USER" -tAc "SELECT count(*) FROM mem_observations" "$DB_NAME" 2>/dev/null || echo "0")
    DOCKER_COUNT=$(echo "$DOCKER_COUNT" | tr -d ' ')

    if [[ "$DOCKER_COUNT" -gt "0" ]]; then
      if $DRY_RUN; then
        info "Would import $DOCKER_COUNT observations from Docker container"
        track "import: would import $DOCKER_COUNT rows"
      else
        info "Importing $DOCKER_COUNT observations from Docker..."
        docker exec "$DOCKER_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --no-owner --no-acl \
          | psql -p $NATIVE_PORT -U "$DB_USER" -d "$DB_NAME" -q
        NEW_COUNT=$(psql -p $NATIVE_PORT -U "$DB_USER" -tAc "SELECT count(*) FROM mem_observations" "$DB_NAME" 2>/dev/null || echo "0")
        ok "Imported data (now $NEW_COUNT observations)"
        track "import: imported from Docker ($NEW_COUNT rows)"
      fi
    else
      skip "Docker container has no data to import"
      track "import: skip (Docker empty)"
    fi
  else
    skip "Docker container not running — skipping import"
    info "Start Docker container first if you need to import data"
    track "import: skip (no Docker)"
  fi
fi

# ── Step 7: Run schema migrations ────────────────────────────

head "Step 7: Schema migrations"

if $DRY_RUN; then
  info "Would run: python scripts/run_migrations.py --dsn postgresql://$DB_USER@localhost:$NATIVE_PORT/$DB_NAME"
  track "migrations: would run"
else
  DSN="postgresql://$DB_USER@localhost:$NATIVE_PORT/$DB_NAME"
  info "Running migrations against native postgres..."
  "$PYTHON" "$PROJECT_DIR/scripts/run_migrations.py" --dsn "$DSN"
  ok "Migrations complete"
  track "migrations: applied"
fi

# ── Step 8: Update .env ──────────────────────────────────────

head "Step 8: Environment configuration (.env)"

# Build the new DATABASE_URL
NEW_DB_URL="postgresql://$DB_USER@localhost:$NATIVE_PORT/$DB_NAME"

if $DRY_RUN; then
  info "Would update .env:"
  info "  DATABASE_URL=$NEW_DB_URL"
  track ".env: would update"
else
  # Use the env-write hook if available, otherwise edit directly
  ENV_WRITE="$HOME/.claude/hooks/env-write.js"
  if [[ -f "$ENV_WRITE" ]]; then
    node "$ENV_WRITE" "$PROJECT_DIR/.env" "DATABASE_URL" "$NEW_DB_URL"
    ok "Updated DATABASE_URL via env-write hook"
  else
    # Direct edit — update or add DATABASE_URL
    if grep -q "^DATABASE_URL=" "$PROJECT_DIR/.env" 2>/dev/null; then
      sed -i '' "s|^DATABASE_URL=.*|DATABASE_URL=$NEW_DB_URL|" "$PROJECT_DIR/.env"
    else
      echo "DATABASE_URL=$NEW_DB_URL" >> "$PROJECT_DIR/.env"
    fi
    ok "Updated DATABASE_URL in .env"
  fi
  track ".env: updated"
fi

# ── Summary ──────────────────────────────────────────────────

echo ""
echo -e "${BOLD}  ── Summary ──${NC}"
echo ""
for item in "${SUMMARY[@]}"; do
  echo "     $item"
done
echo ""

if $DRY_RUN; then
  echo -e "  ${YELLOW}Dry run complete. Re-run without --dry-run to apply.${NC}"
else
  echo -e "  ${GREEN}Migration complete!${NC}"
  echo ""
  info "Native PostgreSQL is running on port $NATIVE_PORT"
  info "Auto-starts on boot via: brew services start postgresql@16"
  info ""
  info "You can now stop Docker Desktop — postgres no longer needs it."
  info "To verify: node install.js --status"
fi
echo ""
