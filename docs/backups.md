# Daily Postgres Backups

Operator reference for the daily `pg_dump` schedule of the `agent_memory`
database.

## Overview

- **What:** full `pg_dump` of the `agent_memory` database (no owner, no ACL),
  gzipped.
- **When:** daily at 03:14 local time (launchd `StartCalendarInterval`).
- **Where:** `data/backups/daily_YYYYMMDD_HHMMSS.sql.gz` relative to the repo
  root.
- **Retention:** last 3 daily backups are kept; older `daily_*.sql.gz` files
  are pruned by the backup script (`ls -t | tail -n +4 | xargs rm -f`).
- **Method:** native `pg_dump` when local Postgres is reachable, otherwise
  `docker exec` against the `agent-memory-db` container.

Backups named with other prefixes (e.g. `pre_v2_backfill_*.sql.gz`) are
manual snapshots and are not rotated.

## Setup

On macOS the schedule is installed automatically. `hooks/ensure-services.js`
calls `ensureBackupSchedule()` at session start and reinstalls the launchd
job whenever the template `scripts/com.metazen.agent-memory-backup.plist` is
newer than the installed copy at
`~/Library/LaunchAgents/com.metazen.agent-memory-backup.plist`. Failures are
debug-logged and never block session start.

Manual install:

```bash
bash scripts/install_backup_schedule.sh
```

The installer renders the template with absolute `__PROJECT_DIR__` and
`__HOME__` paths, copies it to `~/Library/LaunchAgents/`, and bootstraps the
job with `launchctl bootstrap gui/$UID`.

## Verification

```bash
# One-shot status check: shows resolved paths, install state, recent backups.
bash scripts/install_backup_schedule.sh --check

# Confirm the launchd job is loaded.
launchctl list | grep com.metazen.agent-memory-backup

# Inspect recent backups.
ls -lh data/backups/

# Inspect logs (stdout and stderr are separate files).
tail -n 50 ~/Library/Logs/agent-memory-backup.log
tail -n 50 ~/Library/Logs/agent-memory-backup.err.log
```

`launchctl list | grep com.metazen.agent-memory-backup` prints a line like
`-  0  com.metazen.agent-memory-backup`. The middle column is the last exit
code (0 means the previous run succeeded). A `-` in the PID column is
expected — the job is not running, only scheduled.

## Manual one-off backup

To run a backup right now without waiting for the schedule:

```bash
bash scripts/backup.sh
```

This produces a `daily_*.sql.gz` file and participates in the same 3-file
rotation as the scheduled runs.

## Restore

```bash
# Drop active connections, then restore from a gzipped dump.
psql -U mz -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
   WHERE datname='agent_memory' AND pid <> pg_backend_pid();"

gunzip -c data/backups/daily_YYYYMMDD_HHMMSS.sql.gz \
  | psql -U mz -d agent_memory
```

Caveats:

- The dump is produced with `--no-owner --no-acl`. Restore as a role that
  already owns or can create the schema objects.
- Restoring into an existing database overlays rows on top of current data;
  if you need a clean slate, drop and recreate `agent_memory` first, or
  regenerate the dump with `pg_dump --clean --if-exists` before restoring.
- Open connections (the FastAPI server, MCP clients) will block the restore.
  Stop services first (`node install.js --stop`) or terminate connections as
  shown above.

## Disabling

```bash
bash scripts/install_backup_schedule.sh --uninstall
```

This bootouts the launchd job and removes the installed plist. The backup
script itself stays in the repo and can still be invoked manually.

Note that `ensure-services.js` will re-install the schedule on the next
session start. To disable persistently, also remove or comment the
`ensureBackupSchedule()` call in `hooks/ensure-services.js`.

## Non-macOS

The installer falls back to a `crontab` entry on non-Darwin platforms — a
single line at `0 3 * * *` invoking `scripts/backup.sh`. Logs go wherever
`cron` is configured to send them (typically the user mailbox or syslog).
The `--check` and `--uninstall` flags work the same way.
