# Session — 2026-05-19 — agent-memory infra sprint

Two-day sprint (2026-05-18 → 2026-05-19) on the agent-memory *runtime
contract* — the surfaces the model sees on session start and on every
prompt. Independent of the in-progress v5 fine-tune work (`handoff.md`
remains the source of truth for that).

## What shipped

All commits landed on `origin/dev`. None on `main` yet — the dev→main
integration PR is the next human-review gate (intentionally not opened
by the agent).

| commit | summary |
|---|---|
| `3ef92f1` | `fix(memory): scope lessons + observations strictly by project` |
| `a445c71` | `feat(memory): one-call recall() — search + hydrate in a single tool` |
| `442287f` | `feat(memory): abilities_memory() MCP tool + 97% smaller session-start` |
| `4de5d18` | `docs(integration): self-integration guide + /api/integration_guide endpoint` |
| anvil `5cc48f80` | `feat(middleware): per-turn CRITICAL lesson injection — parity with claude` |

## The bugs we found

### 1. Cross-project lesson leak (`3ef92f1`)

Symptom: opening any project in claude-code printed up to 6 CRITICAL
lessons from *other* projects (`[psde]`, `[fire-map.wfca.com]`,
`[anvil]`, `[mz]`) in the session-start preamble and on every prompt.

Two independent bugs, same surface:

- `/api/lessons` with `project=None` (the "globals" fetch the hooks
  used) was returning *every* active lesson regardless of scope. The
  client (`session-start.js`) trusted the response; `user-prompt-submit.js`
  had a client-side filter that masked the bug for that surface but not
  for session-start.
- The `mem_projects` table had a row keyed on `/Users/mz`
  (full_path=`/Users/mz`, project_id=159). `project_path_filter()`'s
  bidirectional-prefix SQL meant ANY cwd under `/Users/mz/*` matched
  this "super-project," so it absorbed every project's hits and
  polluted "Recent Activity" with home-directory noise.

Fix:
- `/api/lessons`: `project=None` → strictly `project_id IS NULL`.
  `project=<path>` → unscoped OR path-prefix OR basename-fallback (so
  moved Dropbox→local checkouts still resolve to the same row).
- `/api/observations` got the same basename-fallback for parity.
- Data: quarantined the 5 super-project rows (`mz`, `/`, `.`,
  `unknown`, `_CODING-Dropbox`) by renaming them and remapping their
  `full_path` to `/.agent-memory-archive/...` so the prefix filter
  can't hit them anymore. History stays attached and searchable.
  The 3 lessons that had been attached to the `mz` super-project
  were reassigned to `project_id=NULL` (truly global) since they
  were agent-behavior rules, not project facts.

Verified live: both session-start `systemMessage` and
user-prompt-submit `additionalContext` now show only `[global]` lessons
and project-scoped Recent Activity. No cross-project leak.

### 2. Session-start preamble was 15KB → file-stashed by Claude Code

Claude Code's tool-result handler wraps any large hook output in
`<persisted-output>...</persisted-output>` with only the first ~2KB
inline as a preview; the rest goes to a sidecar file the model never
opens. Our session-start `systemMessage` was 15546 bytes:

| section | bytes |
|---|---|
| MCP_HINT | 1413 |
| Memory Visibility Rules | 900 |
| Active Lessons (10 critical) | 5496 |
| Project Knowledge (10 observations) | 7364 |
| Recent Activity | 373 |

So the model saw the MCP hint + visibility rules + maybe 1-2 lessons,
then truncation. **Most of what we thought we were injecting never
reached the model.**

Worth noting: lessons *also* inject every prompt via
`user-prompt-submit.js`, which is well under the cap. So lessons were
firing every turn — the session-start lesson block was redundant duplication
that the cap was eating anyway.

Fix: deprecate session-start as a content surface.
- Session-start `systemMessage` shrunk to a 434-byte stub: cwd,
  project-scoping reminder, "call `abilities_memory()` for the manual."
- Dead code removed (`MCP_HINT`, `MEMORY_VISIBILITY_RULES`,
  `projectCtx`, `fetchLessons`, `fetchObservations`,
  `searchProjectContext`).
- Operator manual moved to a new MCP tool `abilities_memory()` that
  renders **live** at call time:
  - tool inventory iterated from `list_tools()` so the manual never
    drifts when tools are added/renamed
  - active CRITICAL lesson + observation counts queried from the DB
    (scoped to `project=<cwd>` if passed)
  - static prose for project scoping, visibility, lesson auto-inject
    stays inline

97% reduction (15546 → 434 bytes) on session-start. Lessons keep firing
every turn via `user-prompt-submit.js`, which is the load-bearing
surface anyway.

### 3. The 3-tool memory dance was unnecessary friction (`a445c71`)

`search()` → `timeline()` → `get_observations([IDs])` was a token-saving
heuristic the model had to remember to choreograph. For the common case
("what do I know about X?") it was pure overhead. Added `recall()`:

- MCP: `recall(query, project, k=5)` — composes `_search` (hybrid
  vector+FTS+keyword RRF with recency boost) with `_get_observations`,
  returns top-k full hydrated rows in one call. `k` clamped [1, 20].
- HTTP: `POST /api/recall` — thin wrapper over
  `/api/observations/search` with k=5 default + hybrid mode.

Backwards compat: `search`, `timeline`, `get_observations`,
`/api/observations/search` unchanged. Existing callers (anvil, codex,
training exporter) keep working.

`memory_search_guide` tool and session-start hint updated to advertise
`recall()` as preferred, 3-step dance as "advanced for triaging large
result sets."

## Architecture decisions

### Why an integration guide instead of an SDK (`4de5d18`)

User asked about extracting a shared abstraction so other systems
could install agent-memory. We didn't — each host's hook surface is
genuinely different (claude's stdin/stdout JSON hooks vs. anvil's
in-process middleware stack vs. codex's pre-tool trigger). Locking in
an SDK shape we haven't validated against a fourth integration would
be premature.

Shipped instead: `docs/INTEGRATION.md` + `GET /api/integration_guide`.

Structure: recipes first (5 minimum-viable wires with Python + Node
stubs, ~50 lines each), then a lifecycle hook map pointing at the
three live wirings, then the full contract (auth, payload schemas,
project scoping rules, failure modes, idempotency). Audience is both
human implementers and LLMs doing self-integration.

If/when a fourth wire reveals a real abstraction, we'll extract it
informed by reality.

### Why anvil's middleware mirrors claude's hook contract exactly

`anvil/middleware/agent_memory_hints.py` was extended (not replaced)
to fetch `/api/lessons?severity=critical` every turn and inject the
**same** `<agent-memory>...</agent-memory>` envelope claude does. The
model's expectation about that envelope shape is now stable across
agents — if anvil and claude rendered lessons differently, the model
would have to learn two formats.

Anvil already had `MemoryMiddleware` for PreToolUse pattern-matched
lessons via `/api/lessons/match`, but no equivalent of the per-turn
CRITICAL lessons block. That gap is closed.

## Surfaces touched

```
agent-memory:
  app/routes/lessons.py        (scope filter)
  app/routes/observations.py   (basename fallback + /api/recall)
  app/routes/health.py         (/api/integration_guide)
  mcp_server.py                (recall + abilities_memory)
  hooks/session-start.js       (15.5KB → 434B preamble)
  hooks/user-prompt-submit.js  (drop redundant globals fetch)
  docs/INTEGRATION.md          (new)
  tests/test_api_observations.py (recall tests)
  tests/test_api_health.py     (integration-guide test)

anvil:
  anvil/middleware/agent_memory_hints.py  (per-turn lessons inject)
  tests/test_agent_memory_lessons_injection.py  (new)

Postgres (live, no migration file yet):
  mem_projects: 5 super-project rows quarantined
  mem_lessons:  3 mz-attached lessons reassigned to NULL project
```

## Open items / next steps

1. **No migration file for the super-project quarantine.** The cleanup
   was done directly in the running DB. If another checkout restores
   from backup or sets up fresh Postgres, the `/Users/mz`-style
   super-projects will recreate themselves on first contact. Should
   write `migrations/015_quarantine_super_projects.sql` and add to the
   migration runner.
2. **`dev → main` PR.** All today's work is on `dev`. The integration
   PR is the only PR the agent is allowed to open, and it requires a
   human review gate. Recommend before next ramp-up; the trunk has
   diverged ~8 commits since last integration.
3. **Pre-existing test failure on `dev`:
   `tests/test_api_tool_calls.py::test_tool_calls_lookup`** returns 404.
   Confirmed not introduced by today's work (failed on bare `origin/dev`
   HEAD pre-commit). Unrelated. Worth investigating in its own task.
4. **Codex integration.** Anvil + claude both now pull lessons via
   the same envelope. Codex has the PreToolUse pattern-match wire
   (`integrations/codex/pre-tool-trigger.js`) but no per-turn
   CRITICAL-lessons block. Same shape transfer as the anvil work
   if/when prioritized.

## Verification record

- 31/31 agent-memory tests pass on `dev`
  (`test_api_observations.py`, `test_api_lessons.py`, `test_api_health.py`).
- 76/76 relevant anvil tests pass on `dev`
  (`test_middleware.py`, `test_middleware_integration.py`,
  `test_memory_hint_visibility.py`, `test_agent_memory_lessons_injection.py`).
- Live integration probe: claude and anvil both fetch the same 10
  CRITICAL lessons from `localhost:3377/api/lessons` for the
  agent-memory cwd and format them identically.
- `curl http://localhost:3377/api/integration_guide` returns 17.5KB of
  markdown verbatim.
- session-start preamble dropped from 15546 bytes to 434 bytes
  (97% reduction, well under Claude Code's ~2KB `<persisted-output>`
  preview cap).
