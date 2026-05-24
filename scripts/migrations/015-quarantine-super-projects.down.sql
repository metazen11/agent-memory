-- 015-quarantine-super-projects.down.sql
--
-- Reverse 015 by restoring rows from the backup tables. Restores the
-- pre-migration name/full_path/canonical_root_path/git_remote on
-- mem_projects, and re-attaches lessons that had been moved to NULL.
--
-- WARNING: running this re-introduces the prefix-leak bug if you don't
-- also revert the runtime fix (commit 3ef92f1). The migration is the
-- belt; the runtime filter is the suspenders. The two should travel
-- together.

BEGIN;

-- ── Step 1: restore project rows from migration_015_project_backup ──

UPDATE mem_projects p
SET
    name                = b.name,
    full_path           = b.full_path,
    canonical_root_path = b.canonical_root_path,
    git_remote          = b.git_remote
FROM migration_015_project_backup b
WHERE p.id = b.id;

-- ── Step 2: re-attach lessons that were moved to NULL ──

UPDATE mem_lessons l
SET project_id = b.original_project_id
FROM migration_015_lesson_backup b
WHERE l.id = b.lesson_id;

-- ── Step 3: drop the backup tables ──
-- Re-running the up migration after a down will re-snapshot from the
-- now-restored state. If you want to keep the backups for forensics,
-- comment out these two DROP TABLE statements.

DROP TABLE IF EXISTS migration_015_lesson_backup;
DROP TABLE IF EXISTS migration_015_project_backup;

COMMIT;
