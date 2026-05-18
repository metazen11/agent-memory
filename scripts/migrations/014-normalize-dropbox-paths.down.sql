-- 014-normalize-dropbox-paths.down.sql
--
-- Restore /Users/mz/Dropbox/_CODING/ paths from migration_014_path_backup.
-- This reverses the up-migration row-for-row using the backup table.
--
-- The up-migration's normalization is idempotent (running it twice is a
-- no-op), so this down also restores the original state if the up was
-- run multiple times — only the FIRST backup row per (table, row_id,
-- column) is the true pre-migration value; later backups would be from
-- already-normalized rows. We dedupe on (table_name, row_id, column_name)
-- by taking the EARLIEST backed_up_at.

BEGIN;

-- Restore mem_tool_calls.cwd
UPDATE mem_tool_calls AS t
SET cwd = b.old_value
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_tool_calls' AND column_name = 'cwd'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

-- Restore mem_tool_calls.tool_input (jsonb)
UPDATE mem_tool_calls AS t
SET tool_input = b.old_value::jsonb
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_tool_calls' AND column_name = 'tool_input'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

-- Restore mem_tool_calls.tool_response_preview
UPDATE mem_tool_calls AS t
SET tool_response_preview = b.old_value
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_tool_calls' AND column_name = 'tool_response_preview'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

-- Restore mem_projects.canonical_root_path
UPDATE mem_projects AS t
SET canonical_root_path = b.old_value
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_projects' AND column_name = 'canonical_root_path'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

-- Restore mem_projects.full_path
UPDATE mem_projects AS t
SET full_path = b.old_value
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_projects' AND column_name = 'full_path'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

-- Restore mem_user_prompts.prompt_text
UPDATE mem_user_prompts AS t
SET prompt_text = b.old_value
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_user_prompts' AND column_name = 'prompt_text'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

-- Restore mem_observations title + narrative + facts
UPDATE mem_observations AS t
SET title = b.old_value
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_observations' AND column_name = 'title'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

UPDATE mem_observations AS t
SET narrative = b.old_value
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_observations' AND column_name = 'narrative'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

UPDATE mem_observations AS t
SET facts = b.old_value::jsonb
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'mem_observations' AND column_name = 'facts'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

-- Restore FK project_id columns (mem_tool_calls/sessions/user_prompts/observations/lessons/projects)
-- These were redirected to local_id during consolidation; the backup
-- contains the original dropbox_id as text.

UPDATE mem_tool_calls t SET project_id = b.old_value::bigint
FROM (SELECT DISTINCT ON (row_id) row_id, old_value FROM migration_014_path_backup
      WHERE table_name = 'mem_tool_calls' AND column_name = 'project_id'
      ORDER BY row_id, backed_up_at ASC) b
WHERE t.id = b.row_id;

UPDATE mem_sessions t SET project_id = b.old_value::bigint
FROM (SELECT DISTINCT ON (row_id) row_id, old_value FROM migration_014_path_backup
      WHERE table_name = 'mem_sessions' AND column_name = 'project_id'
      ORDER BY row_id, backed_up_at ASC) b
WHERE t.id = b.row_id;

UPDATE mem_user_prompts t SET project_id = b.old_value::bigint
FROM (SELECT DISTINCT ON (row_id) row_id, old_value FROM migration_014_path_backup
      WHERE table_name = 'mem_user_prompts' AND column_name = 'project_id'
      ORDER BY row_id, backed_up_at ASC) b
WHERE t.id = b.row_id;

UPDATE mem_observations t SET project_id = b.old_value::bigint
FROM (SELECT DISTINCT ON (row_id) row_id, old_value FROM migration_014_path_backup
      WHERE table_name = 'mem_observations' AND column_name = 'project_id'
      ORDER BY row_id, backed_up_at ASC) b
WHERE t.id = b.row_id;

UPDATE mem_lessons t SET project_id = b.old_value::bigint
FROM (SELECT DISTINCT ON (row_id) row_id, old_value FROM migration_014_path_backup
      WHERE table_name = 'mem_lessons' AND column_name = 'project_id'
      ORDER BY row_id, backed_up_at ASC) b
WHERE t.id = b.row_id;

UPDATE mem_projects t SET parent_project_id = b.old_value::bigint
FROM (SELECT DISTINCT ON (row_id) row_id, old_value FROM migration_014_path_backup
      WHERE table_name = 'mem_projects' AND column_name = 'parent_project_id'
      ORDER BY row_id, backed_up_at ASC) b
WHERE t.id = b.row_id;

-- NOTE: deleted dropbox project rows are NOT auto-restored by this down
-- script. To recover them, restore from the daily_*.sql.gz backup taken
-- before the migration (see data/backups/).

-- Restore backfill_log.jsonl_path
UPDATE backfill_log AS t
SET jsonl_path = b.old_value
FROM (
    SELECT DISTINCT ON (row_id) row_id, old_value
    FROM migration_014_path_backup
    WHERE table_name = 'backfill_log' AND column_name = 'jsonl_path'
    ORDER BY row_id, backed_up_at ASC
) b
WHERE t.id = b.row_id;

-- Once verified the down has worked, drop the backup table manually:
--   DROP TABLE migration_014_path_backup;
-- (Not dropped automatically — keep until rollback is no longer needed.)

COMMIT;
