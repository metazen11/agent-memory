# TODO — codex parity for per-turn CRITICAL lesson injection

Status: **specced, not implemented.** Adopter picks up; the contract below
is enough to wire it end-to-end without rediscovery.

## Background — what claude and anvil already do

After commit `442287f` (agent-memory) and anvil `5cc48f80`, both agents
fetch active CRITICAL lessons **every user turn** and prepend an
`<agent-memory>...</agent-memory>` envelope to the most-recent user
message. The lesson body lands in front of the LLM's eyes on every
prompt, even if the DB updated mid-session.

| Agent | Splice point | Mechanism |
|---|---|---|
| claude-code | `hooks/user-prompt-submit.js` | returns `hookSpecificOutput.additionalContext` |
| anvil | `anvil/middleware/agent_memory_hints.py::before()` | mutates `messages` list in place |
| codex | **NOT IMPLEMENTED** | session-start only — lessons go stale within the session |

## What codex has today

`integrations/codex/hooks.json` exposes four hooks:

- `session_start` (`session-start.js`) — fetches lessons once at session
  boot, formats them, prints to stdout for inclusion in the system prompt.
- `pre_tool_trigger` (`pre-tool-trigger.js`) — calls `/api/lessons/match`
  with tool_name + input preview; injects matched lessons before a tool
  runs. Pattern-matched, not "every turn."
- `post_tool_hook` (`post-tool-hook.js`) — records the tool call to
  `/api/queue` (parity with claude's post-tool-use hook).
- `session_end` — cleanup.

The gap: nothing fires between turns. A lesson created during a session
is invisible until the next session start, and even then it's a one-shot
prompt-prefix rather than a per-turn re-pull.

## Why this matters

CRITICAL lessons are load-bearing — "never push to main without rebasing,"
"always call `done` after a text response," etc. They are written to
*fire every turn*. The per-turn injection is the only surface that
keeps lessons fresh and makes "I just created a lesson" immediately
effective.

In codex today, this loop is broken: a lesson authored during a session
won't fire until the user restarts. That gap also breaks the integration
tests we'd like to write (lesson-created → next-turn-respects-it).

## The codex constraint that makes this hard

> Codex CLI currently supports MCP but not native lifecycle hooks like
> Claude Code.
> — `integrations/codex/hooks.json` notes

Codex has no `UserPromptSubmit` analog. The existing four hooks are
wrapper-driven, not lifecycle-driven by the CLI itself. So a per-turn
hook in the claude sense doesn't exist as a splice point we can
register against.

## Three plausible implementations

### Option A — host watcher polls + injects via a sidecar file

`scripts/codex-agent-memory.sh` already runs a host watcher (per the
hooks.json notes). Extend it to:

1. Tail the codex conversation transcript file (codex writes JSONL).
2. On each new user turn (detect by reading the latest JSONL line),
   call `/api/lessons?active=true&severity=critical&project=<cwd>`.
3. Write the formatted envelope to a sentinel file (e.g.,
   `~/.agent-memory/codex/<session-id>/lessons.envelope.md`).
4. Configure codex's system prompt or pre-input wrapper to read that
   sentinel each turn (codex MCP server can expose it as a resource).

**Pros:** stays out of the codex CLI internals. Reuses the existing
host-watch mechanism.

**Cons:** depends on transcript-tailing being reliable; adds a polling
loop and a sidecar file. The MCP-resource route is the cleanest but
needs the codex side to register the resource.

### Option B — MCP resource served by agent-memory's MCP server

Add a new MCP resource (NOT a tool) to `mcp_server.py`:

```
URI: agent-memory://lessons/critical?project=<cwd>
```

Codex's MCP client refetches resources on each turn — the resource
provider sees the request and returns fresh lesson content. This is
the protocol-native way to do "fresh data per turn" in MCP.

**Implementation outline:**

```python
# mcp_server.py
from mcp.types import Resource

@server.list_resources()
async def list_resources():
    return [
        Resource(
            uri="agent-memory://lessons/critical",
            name="Active CRITICAL lessons (auto-refresh)",
            description=(
                "Current active CRITICAL lessons for the caller's project. "
                "Codex (or any MCP client) should fetch this every turn and "
                "prepend to the user message. Returns the same "
                "<agent-memory>...</agent-memory> envelope claude and anvil emit."
            ),
            mimeType="text/markdown",
        ),
    ]

@server.read_resource()
async def read_resource(uri: str):
    # Parse project from the URI's ?project= query (MCP allows query params)
    project = _extract_project_query(uri)
    pool = await get_pool()
    lessons = await _fetch_critical_lessons_via_pool(pool, project)
    envelope = _format_lessons_envelope(lessons)  # mirrors claude/anvil
    return envelope or ""
```

Then on the codex side, configure codex to inject the contents of
`agent-memory://lessons/critical?project=$PWD` as a prefix on every
user message. Codex's MCP client handles the per-turn refresh.

**Pros:** protocol-native. No new hook. Other MCP-aware hosts (gemini,
future agents) get the same wire for free.

**Cons:** depends on codex actually supporting per-turn resource
fetching. Verify before implementing — MCP spec allows resources to be
re-fetched but client behavior varies.

### Option C — codex CLI wrapper script

Wrap codex's CLI invocation with a shell script that intercepts each
user-submitted prompt, fetches lessons, prepends the envelope, and
forwards to codex. Same pattern claude's hook uses, but at the OS
level instead of the codex internal level.

**Pros:** zero changes to codex itself.

**Cons:** brittle (depends on codex's input handling staying
stable), can't capture mid-conversation user inputs in TUI mode.

## Recommendation

Try **Option B first** — it's protocol-native and reusable. If codex
doesn't actually refetch resources per turn, fall back to **Option A**
(host watcher + sentinel file). **Option C** is the last resort.

Whichever lands, the rendered envelope MUST match what claude and anvil
emit so the model's expectations stay stable across agents:

```
<agent-memory>
## Active Lessons

Learned from past mistakes. Follow them.

  1. CRITICAL [global]: <rule>
  2. CRITICAL [<project>]: <rule>
  ...
</agent-memory>
```

The reference implementation lives in
`anvil/middleware/agent_memory_hints.py::_format_lessons_envelope`.

## Test plan

When this lands, add:

1. **Unit test:** the renderer produces the same envelope shape as
   claude/anvil. Snapshot test against the reference output.
2. **Integration test:** create a lesson with `create_lesson()`, start
   a codex session, send a user prompt, assert the envelope appears
   in the system context for that turn.
3. **Soak test:** create lesson mid-session, send a second prompt,
   confirm lesson appears (this is the "fresh data per turn" test
   that's broken today).

## Estimated effort

- **Option B:** ~2-3h. Most of the work is verifying codex's MCP
  resource-refresh behavior, then ~50 lines on the server side.
- **Option A:** ~4-6h. More moving parts (transcript tailing,
  sentinel files, retry on host disconnect).
- **Option C:** ~1h to wire, then high ongoing maintenance burden.

## References

- claude impl: `hooks/user-prompt-submit.js` (agent-memory repo)
- anvil impl: `anvil/middleware/agent_memory_hints.py` (anvil repo)
- envelope contract: `docs/INTEGRATION.md` recipe 1
- codex hook docs: `integrations/codex/hooks.json` (notes section)
- agent-memory infra sprint that closed the claude+anvil gap:
  `docs/sessions/2026-05-19-memory-infra.md`
