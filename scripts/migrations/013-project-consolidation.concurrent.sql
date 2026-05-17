-- 013-project-consolidation.concurrent.sql
-- Runs OUTSIDE any transaction. CREATE INDEX CONCURRENTLY and the parent_fk
-- VALIDATE CONSTRAINT both need this. All statements are idempotent
-- (IF NOT EXISTS / repeatable VALIDATE).

CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_projects_canonical_root_path_idx
    ON mem_projects (canonical_root_path);

CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_projects_git_remote_idx
    ON mem_projects (git_remote);

CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_projects_source_kind_idx
    ON mem_projects (source_kind);

CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_projects_parent_project_id_idx
    ON mem_projects (parent_project_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS mem_tool_calls_git_branch_idx
    ON mem_tool_calls (git_branch);

-- VALIDATE the self-FK now that the table is stable. Idempotent.
ALTER TABLE mem_projects VALIDATE CONSTRAINT mem_projects_parent_project_fk;
