-- 015-quarantine-super-projects.sql
--
-- Quarantine mem_projects rows whose full_path is a strict prefix of any
-- real project's cwd ("super-projects"). Without this, project_path_filter()
-- — which does bidirectional prefix matching — uses these rows to absorb
-- every cwd under their path, leaking cross-project lessons and observations
-- into the per-project injection surfaces.
--
-- Example: a row at full_path='/Users/mz' (created when a hook fired with
-- cwd=$HOME outside any project) means EVERY cwd under /Users/mz/* matches
-- the row's path filter. The session-start preamble and user-prompt-submit
-- injections then pull lessons/observations attached to this row into every
-- project, e.g. seeing [mz] lessons in /Users/mz/_CODING/agentMemory work.
--
-- Background: see docs/sessions/2026-05-19-memory-infra.md for the full
-- post-mortem. The runtime fix (per-endpoint filter changes + basename
-- fallback) shipped in commit 3ef92f1. This migration is the data fix that
-- makes a fresh DB / restored backup get the same cleanup automatically.
--
-- What this migration does, in one transaction:
--
--   1. Identify super-project rows by signature (top-level/sentinel paths
--      that could absorb other projects).
--   2. For each lesson attached to a super-project, reassign to project_id
--      = NULL ("truly global"). Rationale: lessons end up attached to a
--      super-project because its prefix swallowed the lesson author's cwd,
--      not because the rule is project-specific. Promoting to global is
--      the safest interpretation — the rule still fires, just everywhere.
--   3. Rename the super-project rows and remap their full_path to a
--      sentinel under /.agent-memory-archive/* so the prefix filter can
--      no longer hit them on any real cwd. History (observations, sessions,
--      tool_calls) stays attached and remains searchable when explicitly
--      queried.
--
-- Idempotent: re-running on an already-quarantined DB is a no-op. The
-- signature matchers exclude paths that already start with
-- /.agent-memory-archive/ or /Users/<x>/.agent-memory-archive/.

BEGIN;

-- ── Step 0: snapshot pre-migration state into a backup table ──
-- Makes the down-migration possible by recording the original
-- (id, name, full_path, canonical_root_path, git_remote) for each row
-- we're about to rewrite.

CREATE TABLE IF NOT EXISTS migration_015_project_backup (
    id                  bigint PRIMARY KEY,
    name                text,
    full_path           text,
    canonical_root_path text,
    git_remote          text,
    backed_up_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS migration_015_lesson_backup (
    lesson_id           bigint PRIMARY KEY,
    original_project_id bigint,
    backed_up_at        timestamptz NOT NULL DEFAULT now()
);

-- ── Step 1: identify super-projects ──
-- Match by signature, not hardcoded IDs (different DBs/restores will
-- assign different IDs). A row is a super-project if its full_path is
-- one of:
--   *  exactly '/'
--   *  exactly '.'    (pre-migration-013 sentinel)
--   *  exactly '/unknown'  (sentinel used when ensure_project gets no path)
--   *  '/Users/<one-segment>'  (home-dir row — created when a hook ran
--                               with cwd=$HOME outside any project)
--   *  '/Users/<one-segment>/Dropbox/_CODING'  (the Dropbox parent —
--                               source of the v4 path-bias and a
--                               2-level prefix that absorbs every
--                               Dropbox-checkout project)
-- Already-quarantined rows (full_path starts with .agent-memory-archive)
-- are skipped so re-running this migration is a no-op.

CREATE TEMP TABLE _super_projects ON COMMIT DROP AS
SELECT id, name, full_path, canonical_root_path, git_remote
FROM mem_projects
WHERE
  -- not already quarantined
  full_path NOT LIKE '/.agent-memory-archive/%'
  AND full_path NOT LIKE '/Users/%/.agent-memory-archive/%'
  AND (
       full_path = '/'
    OR full_path = '.'
    OR full_path = '/unknown'
    -- /Users/<single-segment> — home dir
    OR full_path ~ '^/Users/[^/]+$'
    -- /Users/<single-segment>/Dropbox/_CODING
    OR full_path ~ '^/Users/[^/]+/Dropbox/_CODING$'
  );

-- ── Step 2: snapshot the projects we're about to rewrite ──

INSERT INTO migration_015_project_backup
    (id, name, full_path, canonical_root_path, git_remote)
SELECT id, name, full_path, canonical_root_path, git_remote
FROM _super_projects
ON CONFLICT (id) DO NOTHING;  -- preserve earliest backup row

-- ── Step 3: snapshot + reassign lessons attached to super-projects ──

INSERT INTO migration_015_lesson_backup (lesson_id, original_project_id)
SELECT l.id, l.project_id
FROM mem_lessons l
WHERE l.project_id IN (SELECT id FROM _super_projects)
ON CONFLICT (lesson_id) DO NOTHING;

UPDATE mem_lessons
SET project_id = NULL
WHERE project_id IN (SELECT id FROM _super_projects);

-- ── Step 4: rename + remap the super-project rows themselves ──
-- Each row gets a sentinel path that no real cwd will ever match against
-- the bidirectional prefix filter. Pattern:
--   path='/' → /.agent-memory-archive/root-id-<N>
--   path='.' → /.agent-memory-archive/dot-id-<N>
--   path='/unknown' → /.agent-memory-archive/unknown-id-<N>
--   path='/Users/<u>' → /Users/<u>/.agent-memory-archive/personal-id-<N>
--   path='/Users/<u>/Dropbox/_CODING' → /Users/<u>/.agent-memory-archive/dropbox-coding-id-<N>
-- The id suffix keeps name/path uniqueness across multiple super-project
-- rows of the same kind (rare but possible).

UPDATE mem_projects p
SET
    name = CASE
        WHEN p.full_path = '/'        THEN 'root-archived'
        WHEN p.full_path = '.'        THEN 'dot-archived'
        WHEN p.full_path = '/unknown' THEN 'unknown-archived'
        WHEN p.full_path ~ '^/Users/[^/]+$' THEN
            split_part(p.full_path, '/', 3) || '-personal-archived'
        WHEN p.full_path ~ '^/Users/[^/]+/Dropbox/_CODING$' THEN
            split_part(p.full_path, '/', 3) || '-dropbox-coding-archived'
        ELSE p.name
    END,
    full_path = CASE
        WHEN p.full_path = '/'        THEN '/.agent-memory-archive/root-id-' || p.id::text
        WHEN p.full_path = '.'        THEN '/.agent-memory-archive/dot-id-' || p.id::text
        WHEN p.full_path = '/unknown' THEN '/.agent-memory-archive/unknown-id-' || p.id::text
        WHEN p.full_path ~ '^/Users/[^/]+$' THEN
            p.full_path || '/.agent-memory-archive/personal-id-' || p.id::text
        WHEN p.full_path ~ '^/Users/[^/]+/Dropbox/_CODING$' THEN
            '/Users/' || split_part(p.full_path, '/', 3) ||
            '/.agent-memory-archive/dropbox-coding-id-' || p.id::text
        ELSE p.full_path
    END,
    canonical_root_path = NULL,
    git_remote = NULL
WHERE p.id IN (SELECT id FROM _super_projects);

-- ── Step 5: report what changed ──

DO $$
DECLARE
    n_projects integer;
    n_lessons integer;
BEGIN
    SELECT count(*) INTO n_projects FROM _super_projects;
    SELECT count(*) INTO n_lessons FROM migration_015_lesson_backup;
    RAISE NOTICE '015: quarantined % super-project row(s); reassigned % lesson(s) to project_id=NULL',
                 n_projects, n_lessons;
END $$;

COMMIT;
