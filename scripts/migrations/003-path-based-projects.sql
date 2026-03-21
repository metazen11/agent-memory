-- 003-path-based-projects.sql
-- Switch from basename-only project names to full_path as canonical identifier.
-- Enables bidirectional prefix matching (parent finds child projects, child finds parent).

BEGIN;

-- Step 0: Normalize empty strings to NULL
UPDATE mem_projects SET full_path = NULL WHERE full_path = '';

-- Step 1: Backfill full_path from mem_observation_queue.cwd where possible.
-- Pick the most recent cwd for each project, but only if that cwd isn't
-- already claimed by another project's full_path.
UPDATE mem_projects p
SET full_path = sub.best_path
FROM (
    SELECT DISTINCT ON (s.project_id)
        s.project_id,
        q.cwd AS best_path
    FROM mem_observation_queue q
    JOIN mem_sessions s ON s.id = q.session_id
    WHERE q.cwd IS NOT NULL AND q.cwd != ''
    ORDER BY s.project_id, q.id DESC
) sub
WHERE p.id = sub.project_id
  AND p.full_path IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM mem_projects p2
      WHERE p2.full_path = sub.best_path AND p2.id != p.id
  );

-- Step 2: Backfill remaining NULLs with name (basename-only fallback).
-- These are legacy projects with no cwd data — they get name as full_path.
UPDATE mem_projects
SET full_path = name
WHERE full_path IS NULL;

-- Step 3: Make full_path NOT NULL
ALTER TABLE mem_projects ALTER COLUMN full_path SET NOT NULL;

-- Step 4: Deduplicate any remaining collisions on full_path.
-- Keep the project with the most observations; delete others by reassigning
-- their sessions/observations to the winner, then removing the duplicates.
DO $$
DECLARE
    dup RECORD;
    winner_id INT;
BEGIN
    FOR dup IN
        SELECT full_path FROM mem_projects
        GROUP BY full_path HAVING COUNT(*) > 1
    LOOP
        -- Pick the project with most observations as winner
        SELECT p.id INTO winner_id
        FROM mem_projects p
        LEFT JOIN mem_observations o ON o.project_id = p.id
        WHERE p.full_path = dup.full_path
        GROUP BY p.id
        ORDER BY COUNT(o.id) DESC, p.id ASC
        LIMIT 1;

        -- Reassign observations from losers to winner
        UPDATE mem_observations SET project_id = winner_id
        WHERE project_id IN (
            SELECT id FROM mem_projects
            WHERE full_path = dup.full_path AND id != winner_id
        );

        -- Reassign sessions from losers to winner
        UPDATE mem_sessions SET project_id = winner_id
        WHERE project_id IN (
            SELECT id FROM mem_projects
            WHERE full_path = dup.full_path AND id != winner_id
        );

        -- Reassign lessons from losers to winner
        UPDATE mem_lessons SET project_id = winner_id
        WHERE project_id IN (
            SELECT id FROM mem_projects
            WHERE full_path = dup.full_path AND id != winner_id
        );

        -- Delete loser projects
        DELETE FROM mem_projects
        WHERE full_path = dup.full_path AND id != winner_id;
    END LOOP;
END $$;

-- Step 5: Drop the old UNIQUE constraint on name (if it exists)
ALTER TABLE mem_projects DROP CONSTRAINT IF EXISTS mem_projects_name_key;

-- Step 6: Add UNIQUE on full_path
ALTER TABLE mem_projects ADD CONSTRAINT mem_projects_full_path_key UNIQUE (full_path);

-- Step 7: Add text_pattern_ops index for efficient LIKE 'prefix%' queries
CREATE INDEX IF NOT EXISTS idx_mem_projects_full_path_pattern
ON mem_projects (full_path text_pattern_ops);

COMMIT;
