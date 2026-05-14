-- 013-project-consolidation.down.sql
-- Reverses migration 013. Drops indexes (CONCURRENTLY) outside the
-- transaction; FK and columns drop inside.
--
-- Apply manually:
--   psql -U mz -d agent_memory -f scripts/migrations/013-project-consolidation.down.sql
--
-- Not auto-applied. Rollback note: this DOES NOT reverse the consolidation
-- script's re-mapping of mem_tool_calls.project_id — that requires a separate
-- snapshot taken before the consolidation run.

DROP INDEX IF EXISTS mem_projects_canonical_root_path_idx;
DROP INDEX IF EXISTS mem_projects_git_remote_idx;
DROP INDEX IF EXISTS mem_projects_source_kind_idx;
DROP INDEX IF EXISTS mem_projects_parent_project_id_idx;
DROP INDEX IF EXISTS mem_tool_calls_git_branch_idx;

BEGIN;

ALTER TABLE mem_projects
    DROP CONSTRAINT IF EXISTS mem_projects_parent_project_fk;

ALTER TABLE mem_tool_calls DROP COLUMN IF EXISTS git_sha;
ALTER TABLE mem_tool_calls DROP COLUMN IF EXISTS git_branch;

ALTER TABLE mem_projects   DROP COLUMN IF EXISTS parent_project_id;
ALTER TABLE mem_projects   DROP COLUMN IF EXISTS source_kind;
ALTER TABLE mem_projects   DROP COLUMN IF EXISTS canonical_root_path;

DELETE FROM mem_schema_migrations WHERE filename IN (
    '013-project-consolidation.sql',
    '013-project-consolidation.concurrent.sql'
);

COMMIT;
