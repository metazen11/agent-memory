# Lessons-as-Enforcement: Design Sketch

**Status:** Draft. Captures the design from the 2026-05-16 Claude session
that converted the `git init in $HOME` lesson into a PreToolUse hook.
**No code in this doc — implementation is a future sprint.**

## Problem

Today, "lessons" live in the `mem_lessons` table and are pushed into
agent prompts via UserPromptSubmit `additionalContext`. This has three
recurring failure modes:

1. **Push grows unboundedly.** Every new lesson adds bytes to every
   prompt of every agent in every project. With cross-agent lessons
   (e.g., a fire-map.wfca.com rule appearing in psde_mz_test), the
   signal-to-noise ratio collapses.

2. **A lesson is a wish, not a guarantee.** "NEVER run git init in
   $HOME" depends on the model reading the rule, remembering it, and
   not making a mistake. A hook denying the Bash call enforces it
   mechanically — no model attention required.

3. **No lifecycle.** Lessons accumulate. There's no signal "this lesson
   was fixed in code." A lesson that's now enforced by a hook keeps
   getting injected, wasting prompt budget.

## Insight

> Every active lesson should have a paired enforcement artifact (hook,
> runtime check, or GitHub issue). The lesson is the rationale; the
> artifact is the enforcement. Lessons without enforcement are work
> items; lessons with enforcement should stop pushing into prompts.

## Proposed schema change

Add to `mem_lessons`:

| column | type | purpose |
|---|---|---|
| `enforcement_kind` | enum | `hook`, `runtime`, `gh_issue`, `none` |
| `enforcement_ref` | text | hook file path, GH issue URL, etc. |
| `enforcement_agent` | text | `claude-code`, `codex`, `anvil`, or `*` |
| `enforcement_commit` | text | SHA where the artifact was added |
| `enforcement_added_at` | timestamp | when the artifact landed |

Migration is additive (all nullable). Existing lessons get
`enforcement_kind = NULL`, indicating "still a wish."

## Inject-side change

The UserPromptSubmit hook filters lessons it pushes:

```
inject lesson L if:
  L.active = true
  AND L.severity = 'critical'
  AND (L.enforcement_kind IS NULL  -- not yet enforced
       OR L.enforcement_agent != current_agent)  -- enforced elsewhere
```

A lesson enforced by a Claude hook stops appearing in Claude prompts.
It still appears in Codex prompts until Codex has an equivalent
enforcement artifact. Cross-agent rules want all three slots filled
before they disappear from inject everywhere.

## /promote-lesson skill (Shape A from session discussion)

A user-invoked slash command. Manual promotion, low magic.

```
/promote-lesson <lesson_id> [--kind hook|runtime|gh_issue] [--agent claude|codex|anvil|*]
```

Behavior depends on `--kind`:

- `--kind gh_issue` (default for project-scoped lessons): opens a GH
  issue in the repo identified by the lesson's project path. Issue
  body = lesson rule + creation context. Labels: `lesson`,
  `severity:critical`. Returns issue URL.
- `--kind hook`: prompts the operator to choose hook type
  (PreToolUse / PostToolUse / Stop) and tool matcher, then scaffolds
  `~/.claude/hooks/<lesson-slug>.js` from a template, opens it for
  editing. Does NOT auto-wire into settings.json — operator does
  that.
- `--kind runtime`: prints a stub for the agent's runtime
  (Anvil/Codex) and the file to add it to. Operator implements.

In all cases, on success, the skill calls `PATCH /api/lessons/<id>`
setting `enforcement_kind`, `enforcement_ref`, `enforcement_agent`,
`enforcement_commit`. Lesson stays `active=true` — it's still the
rationale of record — but the inject filter (above) starts skipping
it for the appropriate agent.

## Cross-agent fairness

Three agents (Claude, Codex, Anvil) consume the same lessons table.
A lesson enforced by a Claude hook is still a wish for Codex and
Anvil until they have their own enforcement. The schema handles this:
`enforcement_agent` distinguishes per-agent vs all-agent enforcement,
and the inject filter respects it.

The "ideal end state" for a critical lesson is `enforcement_agent='*'`
with separate hook/runtime artifacts in each agent's adapter
directory. The lesson then never appears in any inject; it's enforced
everywhere, mechanically.

## Worked example: lesson 52 (git init in $HOME)

- 2026-05-10 — lesson created. `enforcement_kind = NULL`. Pushed into
  every Claude prompt.
- 2026-05-16 — Claude session built
  `~/.claude/hooks/git-init-guard.js` (PreToolUse Bash matcher,
  denies with the lesson text as reason). Lesson deactivated in DB
  (`active = false`). Stopped appearing in inject.
- **Today (with this design):** instead of deactivating, the operator
  would run `/promote-lesson 52 --kind hook --agent claude-code` to
  record the enforcement link without losing the lesson as an
  auditable record. Codex and Anvil still get the inject until they
  ship equivalent guards.

## Worked example: the `[mz]` done-tool lesson

- Anvil-specific rule about agent loop behavior. Never belonged in
  Claude inject.
- Right home: runtime check inside `integrations/anvil/` agent loop.
- Promote: `/promote-lesson <id> --kind runtime --agent anvil`,
  scope the lesson to `project_name='anvil'` so cross-agent inject
  excludes it.

## Out of scope for this sketch

- Auto-detection of "this lesson already has an enforcement artifact"
  by scanning the repo for matching hook files. Could be added later
  as a sanity-check job.
- Bidirectional GH issue sync (closing the issue → deactivating the
  lesson, etc.). Shape B from session discussion. Wait until
  Shape A has run for a few weeks before adding machinery.
- A UI for browsing active lessons + their enforcement status. Just
  use `curl /api/lessons?active=true | jq` for now.

## Next steps when this gets prioritized

1. Migration for the five new columns. Backfill nothing.
2. Update `LessonUpdate` Pydantic model in `app/routes/lessons.py`.
3. Update UserPromptSubmit hook inject filter to skip
   `enforcement_kind IS NOT NULL AND enforcement_agent IN ('*', this_agent)`.
4. Scaffold `/promote-lesson` skill. Start with `--kind gh_issue`
   path — it's the simplest and unblocks the bulk of project-scoped
   lessons.
5. Backfill: walk the existing active CRITICAL lessons, decide for
   each whether it's hook-enforceable / runtime-enforceable /
   GH-issue, and promote.
