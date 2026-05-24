-- 016-lesson-trigger-broad-match-guard.down.sql
--
-- Reverse migration 016 by dropping the CHECK constraint. The runtime
-- guard in app/routes/lessons.py still filters broad-match input
-- triggers at match time, so existing data behavior is unchanged; the
-- only effect is that the DB will once again accept broad-match
-- input-triggered lessons via INSERT/UPDATE. Pair this with reverting
-- the runtime change if you want to fully restore pre-016 behavior.

BEGIN;

ALTER TABLE mem_lessons DROP CONSTRAINT IF EXISTS chk_input_trigger_has_filter;

COMMIT;
