-- 013-project-consolidation.sql
-- Distinguish project leaf cwds from canonical git roots; track branch + sha
-- per tool call. See docs/fine_tune/V2_DATA_PIPELINE_PLAN.md follow-up notes
-- and issue #36 for design rationale.
--
-- All new columns are nullable so ALTER doesn't rewrite the existing 688
-- mem_projects rows or 55k mem_tool_calls rows. Index builds in the
-- companion .concurrent.sql to avoid blocking writers.

BEGIN;

-- mem_projects: separate the cwd that originated the row from the canonical
-- git root the cwd belongs to. Sub-folders, worktrees, and cross-checkouts
-- of the same repo will share canonical_root_path post-consolidation.
ALTER TABLE mem_projects ADD COLUMN IF NOT EXISTS canonical_root_path text NULL;

-- source_kind: 'git' (under a git repo), 'non-git' (real cwd, no repo),
-- 'ephemeral' (pytest tmp / scratch — excluded from training-data export).
ALTER TABLE mem_projects ADD COLUMN IF NOT EXISTS source_kind         text NULL
    DEFAULT 'git';

-- parent_project_id: self-FK lets a leaf project point at its canonical
-- git-root row. Reads can resolve sub-folder calls to the parent by
-- following this link. NULL on canonical rows themselves.
ALTER TABLE mem_projects ADD COLUMN IF NOT EXISTS parent_project_id   bigint NULL;

-- Self-FK, NOT VALID to skip the existing-row scan; VALIDATE happens in
-- the concurrent companion.
DO $mig$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE t.relname = 'mem_projects'
          AND c.conname = 'mem_projects_parent_project_fk'
    ) THEN
        ALTER TABLE mem_projects
            ADD CONSTRAINT mem_projects_parent_project_fk
            FOREIGN KEY (parent_project_id)
            REFERENCES mem_projects(id) ON DELETE SET NULL NOT VALID;
    END IF;
END
$mig$;

-- mem_tool_calls: branch and sha at the moment of the call. These live on
-- the call (not the session) because branches change mid-session.
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS git_branch text NULL;
ALTER TABLE mem_tool_calls ADD COLUMN IF NOT EXISTS git_sha    text NULL;

COMMIT;
