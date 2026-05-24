-- 014-normalize-dropbox-paths.sql
--
-- ROOT-CAUSE fix for v4 path-bias hallucination. The model trained on
-- mem_tool_calls echoes "/Users/mz/Dropbox/_CODING/X" when the user
-- types "/Users/mz/_CODING/X" because 78% of the training data has a
-- Dropbox cwd.
--
-- Per CLAUDE.md: source of truth is ~/_CODING/, Dropbox is a stale
-- backup mirror. Every /Dropbox/_CODING/<project> is the same content
-- as /_CODING/<project>, just at a different path.
--
-- This migration does (in one transaction):
--
--   1. Project consolidation. For each dropbox-rooted mem_projects row,
--      if a local-rooted twin exists (same suffix after the prefix swap),
--      redirect all FK references (mem_tool_calls.project_id,
--      mem_sessions.project_id, mem_projects.parent_project_id) to the
--      local row, then delete the dropbox row.
--
--   2. In-place path rewrites for the remaining dropbox rows with no
--      local twin (deep paths like sweep_results/* that only exist in
--      the historical dropbox snapshot).
--
--   3. Path rewrites across all other columns that carry path-shaped text:
--        mem_tool_calls.cwd
--        mem_tool_calls.tool_input            (jsonb)
--        mem_tool_calls.tool_response_preview
--        mem_user_prompts.prompt_text
--        mem_observations.title
--        mem_observations.narrative
--        mem_observations.facts               (jsonb)
--        backfill_log.jsonl_path
--
-- All UPDATEs back up the old value into migration_014_path_backup
-- (column-row-pair grain) so 014-normalize-dropbox-paths.down.sql can
-- reverse the change.
--
-- Counts at migration time (2026-05-18):
--   mem_projects: 119 dropbox-rooted rows
--     - 15 collide with a local twin (need FK redirect + delete)
--     - 104 just need in-place rewrite (no local twin exists)
--   mem_tool_calls.project_id refs to dropbox projects: 67,642
--   mem_sessions.project_id   refs to dropbox projects: 258
--   mem_tool_calls.cwd LIKE dropbox: 67,755
--   mem_tool_calls.tool_input contains dropbox: 37,799
--   mem_tool_calls.tool_response_preview: 25,215
--   mem_user_prompts.prompt_text: 256
--   mem_observations title+narrative+facts: 504

BEGIN;

-- ---- 0. Backup table ----
CREATE TABLE IF NOT EXISTS migration_014_path_backup (
    table_name  text NOT NULL,
    row_id      bigint NOT NULL,
    column_name text NOT NULL,
    old_value   text NOT NULL,
    backed_up_at timestamptz NOT NULL DEFAULT now()
);

-- ---- 1. Project consolidation: redirect FKs, then delete dropbox twins ----
-- Build the (dropbox_id -> local_id) mapping for projects that collide.
CREATE TEMP TABLE _proj_remap ON COMMIT DROP AS
SELECT
    d.id AS dropbox_id,
    l.id AS local_id,
    d.full_path AS dropbox_path,
    l.full_path AS local_path
FROM mem_projects d
JOIN mem_projects l
  ON l.full_path = replace(d.full_path, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE d.full_path LIKE '/Users/mz/Dropbox/_CODING/%'
  AND d.id != l.id;

-- Backup the FK redirects (we can restore by remapping the other way)
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_tool_calls', tc.id, 'project_id', tc.project_id::text
FROM mem_tool_calls tc
JOIN _proj_remap r ON r.dropbox_id = tc.project_id;

UPDATE mem_tool_calls tc
SET project_id = r.local_id
FROM _proj_remap r
WHERE tc.project_id = r.dropbox_id;

INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_sessions', s.id, 'project_id', s.project_id::text
FROM mem_sessions s
JOIN _proj_remap r ON r.dropbox_id = s.project_id;

UPDATE mem_sessions s
SET project_id = r.local_id
FROM _proj_remap r
WHERE s.project_id = r.dropbox_id;

-- mem_user_prompts FK
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_user_prompts', u.id, 'project_id', u.project_id::text
FROM mem_user_prompts u
JOIN _proj_remap r ON r.dropbox_id = u.project_id;

UPDATE mem_user_prompts u
SET project_id = r.local_id
FROM _proj_remap r
WHERE u.project_id = r.dropbox_id;

-- mem_observations FK
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_observations', o.id, 'project_id', o.project_id::text
FROM mem_observations o
JOIN _proj_remap r ON r.dropbox_id = o.project_id;

UPDATE mem_observations o
SET project_id = r.local_id
FROM _proj_remap r
WHERE o.project_id = r.dropbox_id;

-- mem_lessons FK
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_lessons', l.id, 'project_id', l.project_id::text
FROM mem_lessons l
JOIN _proj_remap r ON r.dropbox_id = l.project_id;

UPDATE mem_lessons l
SET project_id = r.local_id
FROM _proj_remap r
WHERE l.project_id = r.dropbox_id;

-- mem_projects.parent_project_id may point at a dropbox row too
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_projects', p.id, 'parent_project_id', p.parent_project_id::text
FROM mem_projects p
JOIN _proj_remap r ON r.dropbox_id = p.parent_project_id;

UPDATE mem_projects p
SET parent_project_id = r.local_id
FROM _proj_remap r
WHERE p.parent_project_id = r.dropbox_id;

-- Delete the collided dropbox project rows.
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_projects', dropbox_id, '_DELETED_full_path', dropbox_path
FROM _proj_remap;

DELETE FROM mem_projects p
USING _proj_remap r
WHERE p.id = r.dropbox_id;

-- ---- 2. In-place rewrite for the remaining dropbox-rooted projects ----
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_projects', id, 'full_path', full_path
FROM mem_projects
WHERE full_path LIKE '/Users/mz/Dropbox/_CODING/%';

UPDATE mem_projects
SET full_path = replace(full_path, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE full_path LIKE '/Users/mz/Dropbox/_CODING/%';

INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_projects', id, 'canonical_root_path', canonical_root_path
FROM mem_projects
WHERE canonical_root_path LIKE '/Users/mz/Dropbox/_CODING/%';

UPDATE mem_projects
SET canonical_root_path = replace(canonical_root_path, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE canonical_root_path LIKE '/Users/mz/Dropbox/_CODING/%';

-- ---- 3. Path rewrites in all other tables ----

-- mem_tool_calls.cwd
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_tool_calls', id, 'cwd', cwd
FROM mem_tool_calls
WHERE cwd LIKE '%/Users/mz/Dropbox/_CODING/%';

UPDATE mem_tool_calls
SET cwd = replace(cwd, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE cwd LIKE '%/Users/mz/Dropbox/_CODING/%';

-- mem_tool_calls.tool_input (jsonb)
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_tool_calls', id, 'tool_input', tool_input::text
FROM mem_tool_calls
WHERE tool_input::text LIKE '%/Users/mz/Dropbox/_CODING/%';

UPDATE mem_tool_calls
SET tool_input = replace(tool_input::text, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')::jsonb
WHERE tool_input::text LIKE '%/Users/mz/Dropbox/_CODING/%';

-- mem_tool_calls.tool_response_preview
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_tool_calls', id, 'tool_response_preview', tool_response_preview
FROM mem_tool_calls
WHERE tool_response_preview LIKE '%/Users/mz/Dropbox/_CODING/%';

UPDATE mem_tool_calls
SET tool_response_preview = replace(tool_response_preview, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE tool_response_preview LIKE '%/Users/mz/Dropbox/_CODING/%';

-- mem_user_prompts.prompt_text
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_user_prompts', id, 'prompt_text', prompt_text
FROM mem_user_prompts
WHERE prompt_text LIKE '%/Users/mz/Dropbox/_CODING/%';

UPDATE mem_user_prompts
SET prompt_text = replace(prompt_text, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE prompt_text LIKE '%/Users/mz/Dropbox/_CODING/%';

-- mem_observations: title, narrative, facts
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_observations', id, 'title', title
FROM mem_observations
WHERE title LIKE '%/Users/mz/Dropbox/_CODING/%';

UPDATE mem_observations
SET title = replace(title, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE title LIKE '%/Users/mz/Dropbox/_CODING/%';

INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_observations', id, 'narrative', narrative
FROM mem_observations
WHERE narrative LIKE '%/Users/mz/Dropbox/_CODING/%';

UPDATE mem_observations
SET narrative = replace(narrative, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE narrative LIKE '%/Users/mz/Dropbox/_CODING/%';

INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'mem_observations', id, 'facts', facts::text
FROM mem_observations
WHERE facts::text LIKE '%/Users/mz/Dropbox/_CODING/%';

UPDATE mem_observations
SET facts = replace(facts::text, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')::jsonb
WHERE facts::text LIKE '%/Users/mz/Dropbox/_CODING/%';

-- backfill_log.jsonl_path
INSERT INTO migration_014_path_backup (table_name, row_id, column_name, old_value)
SELECT 'backfill_log', id, 'jsonl_path', jsonl_path
FROM backfill_log
WHERE jsonl_path LIKE '%/Users/mz/Dropbox/_CODING/%';

UPDATE backfill_log
SET jsonl_path = replace(jsonl_path, '/Users/mz/Dropbox/_CODING/', '/Users/mz/_CODING/')
WHERE jsonl_path LIKE '%/Users/mz/Dropbox/_CODING/%';

-- Summary
DO $mig$
DECLARE n_bak bigint;
BEGIN
    SELECT count(*) INTO n_bak FROM migration_014_path_backup;
    RAISE NOTICE 'migration_014: backed up % column-rows for rollback', n_bak;
END
$mig$;

COMMIT;

-- Post-commit verification (run separately, advisory):
--   SELECT count(*) FROM mem_projects WHERE full_path LIKE '%/Dropbox/_CODING/%';
--   SELECT count(*) FROM mem_tool_calls WHERE cwd LIKE '%/Dropbox/_CODING/%';
--   SELECT count(*) FROM mem_tool_calls WHERE tool_input::text LIKE '%/Dropbox/_CODING/%';
--   SELECT count(*) FROM mem_observations WHERE narrative LIKE '%/Dropbox/_CODING/%';
