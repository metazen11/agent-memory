# agent-memory — integration guide

This is a self-integration guide for adding agent-memory to a coding agent
(claude-style hooks, anvil-style middleware, codex, your own). It's written
to be readable by both a human implementer and an LLM that's been asked to
self-integrate. Recipes first, contract below, reference implementations at
the end.

If you're an LLM reading this: skip to the recipes. They have copy-pasteable
examples. The contract section is for when you hit a gotcha and need
authoritative payload shapes.

---

## Recipes — minimum viable wires

These four wires give you ~90% of the value. Implement them in this order;
each is independent.

### Recipe 1 — lessons inject on every turn (load-bearing)

The CRITICAL surface. Lessons are short rules learned from past mistakes
("never push to main without rebasing"). The host fetches them every turn
and prepends them to the user's most recent message in an `<agent-memory>`
envelope. The LLM reads the rules and obeys.

**Wire:** intercept each user turn, before the LLM call. Fetch:

```
GET http://localhost:3377/api/lessons
    ?active=true
    &severity=critical
    &limit=10
    &project=<absolute cwd of the project>
```

Headers: `X-Agent-Name: <your-agent-name>` (allowlisted in
`trusted_agents`; see contract for details).

The server's filter does the right thing automatically:
`project=<cwd>` matches lessons attached to that project (path-prefix or
basename), PLUS truly-global lessons (`project_id IS NULL`).
`project=` (empty/missing) returns ONLY truly-global. Pass the cwd
verbatim.

Wrap and prepend:

```
<agent-memory>
## Active Lessons

Learned from past mistakes. Follow them.

  1. CRITICAL [global]: <rule body>
  2. CRITICAL [<project>]: <rule body>
  ...
</agent-memory>
```

Then the original user message after a blank line. Idempotency:
if the message already contains `<agent-memory>...</agent-memory>`,
do not re-wrap.

**Python (stdlib + httpx, ~40 lines):**

```python
import httpx

BASE = "http://localhost:3377"
TIMEOUT = 3.0
AGENT_NAME = "your-agent"  # must be in trusted_agents allowlist


def fetch_critical_lessons(cwd: str) -> list[dict]:
    try:
        r = httpx.get(
            f"{BASE}/api/lessons",
            params={
                "active": "true",
                "severity": "critical",
                "limit": 10,
                "project": cwd,
            },
            headers={"X-Agent-Name": AGENT_NAME},
            timeout=TIMEOUT,
        )
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []  # fall through — never block the turn


def format_envelope(lessons: list[dict]) -> str:
    if not lessons:
        return ""
    lines = []
    for i, l in enumerate(lessons, 1):
        sev = l.get("severity", "info").upper()
        scope = l.get("project_name") or "global"
        lines.append(f"  {i}. {sev} [{scope}]: {l['rule']}")
    body = "## Active Lessons\n\nLearned from past mistakes. Follow them.\n\n" + "\n".join(lines)
    return f"<agent-memory>\n{body}\n</agent-memory>"


def inject(messages: list[dict], cwd: str) -> list[dict]:
    env = format_envelope(fetch_critical_lessons(cwd))
    if not env:
        return messages
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            if "<agent-memory>" in content:
                return messages  # idempotent
            msg["content"] = f"{env}\n\n{content}"
        return messages
    return messages
```

**Node (stdlib http, ~50 lines):**

```javascript
const http = require('http');

const BASE = 'http://localhost:3377';
const TIMEOUT_MS = 3000;
const AGENT_NAME = 'your-agent';

function fetchCriticalLessons(cwd) {
  return new Promise((resolve) => {
    const params = new URLSearchParams({
      active: 'true', severity: 'critical', limit: '10', project: cwd,
    });
    const url = new URL(`${BASE}/api/lessons?${params}`);
    const req = http.get({
      hostname: url.hostname, port: url.port,
      path: `${url.pathname}${url.search}`,
      headers: { 'X-Agent-Name': AGENT_NAME },
      timeout: TIMEOUT_MS,
    }, (res) => {
      let body = '';
      res.on('data', (c) => { body += c; });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch { resolve([]); }
      });
    });
    req.on('error', () => resolve([]));
    req.on('timeout', () => { req.destroy(); resolve([]); });
  });
}

function formatEnvelope(lessons) {
  if (!lessons || !lessons.length) return '';
  const lines = lessons.map((l, i) => {
    const sev = (l.severity || 'info').toUpperCase();
    const scope = l.project_name || 'global';
    return `  ${i + 1}. ${sev} [${scope}]: ${l.rule}`;
  });
  return `<agent-memory>\n## Active Lessons\n\nLearned from past mistakes. Follow them.\n\n${lines.join('\n')}\n</agent-memory>`;
}

async function injectLessons(messages, cwd) {
  const env = formatEnvelope(await fetchCriticalLessons(cwd));
  if (!env) return messages;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role !== 'user') continue;
    const c = messages[i].content;
    if (typeof c === 'string') {
      if (c.includes('<agent-memory>')) return messages;
      messages[i].content = `${env}\n\n${c}`;
    }
    return messages;
  }
  return messages;
}

module.exports = { injectLessons, fetchCriticalLessons, formatEnvelope };
```

**Failure mode contract:** any error (server down, timeout, bad JSON,
non-200) MUST fall through and let the turn proceed. Lessons are
advisory; blocking on agent-memory unavailability is wrong.

### Recipe 2 — one-call recall (replace bespoke search)

When the agent wants to know "have I seen X before?", call this instead
of implementing your own search. Returns top-k hydrated observations in
one round-trip.

```
POST http://localhost:3377/api/recall
Content-Type: application/json
X-Agent-Name: <your-agent-name>

{
  "query": "auth bug refresh token",
  "project": "/Users/you/path/to/project",
  "k": 5
}
```

Response (`SearchResult`):

```json
{
  "observations": [
    {
      "id": 30882,
      "title": "Fixed JWT refresh token race condition",
      "type": "bugfix",
      "narrative": "...",
      "facts": [...],
      "files_modified": [...],
      "project_name": "myapp",
      "created_at": "2026-02-15T..."
    }
  ],
  "mode": "hybrid",
  "total": 5,
  "query": "auth bug refresh token"
}
```

`k` is clamped to [1, 20]. The server runs vector + FTS + keyword RRF
with recency boost; you don't need to tune.

### Recipe 3 — record tool calls so the memory is worth searching

Every tool call your agent makes should be POSTed to `/api/queue`. The
server writes a `mem_tool_calls` row immediately and queues an LLM
extractor that turns the call into a `mem_observations` row asynchronously.

```
POST http://localhost:3377/api/queue
X-Agent-Name: <your-agent-name>
Content-Type: application/json

{
  "session_id": "sess-<unique-per-conversation>",
  "tool_name": "Bash",
  "tool_input": {"command": "git status"},
  "tool_response_preview": "On branch dev\n...",
  "tool_success": true,
  "cwd": "/Users/you/project",
  "last_user_message": "what's my git status",
  "source_system": "your-agent-name",
  "source_mode": "cli",
  "source_agent": "dev"
}
```

Fire-and-forget — don't await, don't block the tool result. Failures
log and drop; nothing user-visible breaks.

`tool_response_preview` should be the first ~2KB of the response.
`source_system`/`source_mode`/`source_agent` are used to slice training
data later; pick sensible labels and stick with them.

### Recipe 4 — per-tool-call pattern-matched lessons (advanced)

Recipe 1 fires every CRITICAL lesson on every turn. Some lessons should
fire only when a specific tool is about to be called with specific input
(e.g., "block `rm -rf /` patterns"). For those, use `/api/lessons/match`.

```
GET http://localhost:3377/api/lessons/match
    ?tool_name=Bash
    &tool_input_preview=<first 500 chars of the tool input>
    &project=<cwd>
    &trigger_on=input
```

Response is up to 5 matching `LessonMatch` rows. Inject them in front of
the tool call so the LLM sees them before the tool runs. This is the
"PreToolUse" pattern — see `hooks/pre-tool-use.js` in agent-memory for
the wire format. Optional. Recipe 1 alone is sufficient for most agents.

### Recipe 5 — self-discovery via `abilities_memory()`

If you're an MCP host, register agent-memory as an MCP server and the
`abilities_memory(project=<cwd>)` tool will return the live operator
manual (this guide's most-current contents + live tool inventory + DB
counts). Useful for agents that want to bootstrap without baking the
contract into their codebase.

If you're not an MCP host, just `curl` this guide directly:
`http://localhost:3377/docs/INTEGRATION.md` (when served) or read the
markdown file in the agent-memory repo.

---

## Lifecycle hook map — where to splice in your agent

| Agent / host | Splice point | What it does |
|---|---|---|
| claude-code | `hooks/user-prompt-submit.js` returning `hookSpecificOutput.additionalContext` | injects lessons envelope on every prompt |
| claude-code | `hooks/session-start.js` returning `systemMessage` | one-time, 434-byte preamble pointing at `abilities_memory()` |
| claude-code | `hooks/post-tool-use.js` POSTing `/api/queue` | records tool calls |
| anvil | `anvil/middleware/agent_memory_hints.py::AgentMemoryHintsMiddleware.before()` | injects lessons + static system hint each turn |
| anvil | `anvil/middleware/memory_consolidate.py` POSTing `/api/queue` | records tool calls |
| codex | `integrations/codex/pre-tool-trigger.js` calling `/api/lessons/match` | recipe 4 (pattern-matched) |
| your host | the equivalent of "right before the LLM sees the user's input" | recipe 1 |
| your host | the equivalent of "after a tool call completes" | recipe 3 |

**Picking your splice point:** find the lowest layer in your agent that
runs *before every LLM call* and has access to the message list + cwd.
That's where lessons inject. For tool-call recording, find the layer
that runs *after every tool call* with access to the tool name, input,
and output preview.

---

## Reference implementations (worked examples)

These are the live wirings — read them to see how the recipes play out
in a real codebase. They're more elaborate than the minimal stubs above
because they handle their host's lifecycle quirks (multipart content,
multi-agent dedup, debug-tracing toggles), but the core path is the
same: fetch, format, inject.

- **claude-code lessons hook** (Node, ~300 lines including auth header,
  path normalization, prompt logging):
  `hooks/user-prompt-submit.js` in the agent-memory repo.
- **anvil lessons middleware** (Python, ~330 lines including the static
  system-prompt hint and PreToolUse lesson-match plumbing):
  `anvil/middleware/agent_memory_hints.py` in the anvil repo.
- **codex pre-tool trigger** (Node):
  `integrations/codex/pre-tool-trigger.js` in the agent-memory repo.

If you implement a new wire and it lands somewhere shareable, add a
row to the hook-map table above and link it here.

---

## Contract (full reference)

### Base URL

`http://localhost:3377` (FastAPI, port configurable via `.env`).
Default ships with the agent-memory installer; check `health` first:

```
GET /api/health
→ {"status": "ok", "db": {...}, "embeddings": {...}}
```

If `status` ≠ `"ok"` or `"degraded"`, the host should not call the
write endpoints. Reads are still safe.

### Authentication

Two paths:

1. **Trusted-agent allowlist (default).** Send
   `X-Agent-Name: <name>`. If `<name>` is in `app/config.py::trusted_agents`
   (`anvil,claude,codex,gemini,python-httpx`), the request is allowed.
   For new integrations, add your agent's name to that list or set
   `trusted_agents = "*"` to trust all localhost.
2. **Bearer token.** Generate with
   `python -m app.cli create-token --agent <name>`. Send
   `Authorization: Bearer <token>`. Use this when the server is exposed
   beyond localhost (rare; the default deployment is localhost-only).

The two paths are alternates — pick one per request.

### Endpoints used by the recipes

| Method | Path | Body / params | Returns |
|---|---|---|---|
| GET | `/api/lessons` | `active`, `severity`, `limit`, `project` | `list[LessonOut]` |
| GET | `/api/lessons/match` | `tool_name`, `tool_input_preview`, `project`, `trigger_on`, etc. | `list[LessonMatch]` |
| POST | `/api/recall` | `{query, project, k, cross_project?, type?}` | `SearchResult` |
| POST | `/api/observations/search` | `{query, project, limit, mode, type?}` | `SearchResult` (raw search; `recall` is the wrapper) |
| POST | `/api/queue` | `QueueItem` (see schemas) | `{"status": "queued"}` |
| POST | `/api/observations` | `ObservationCreate` | `ObservationOut` |
| POST | `/api/lessons` | `LessonCreate` | `LessonOut` |
| GET | `/api/health` | — | health snapshot |

### Payload schemas (live source: `app/models.py`)

`LessonOut` (what `/api/lessons` and `/api/lessons/match` return):

```
id: int
project_id: int | null
project_name: str | null    # null = truly global
title: str
rule: str                    # the rule body the LLM reads
severity: "critical" | "warning" | "info"
trigger_tool: str | null
trigger_pattern: str | null
trigger_on: "input" | "output" | "phase" | "file_scope"
trigger_count: int
last_triggered_at: timestamp | null
active: bool
created_at: timestamp
```

`QueueItem` (what you POST to `/api/queue`):

```
session_id: str              # REQUIRED. Unique per conversation.
tool_name: str | null
tool_input: dict | null
tool_response_preview: str | null   # first ~2KB
tool_success: bool | null
tool_error: str | null
cwd: str | null              # the project's cwd
last_user_message: str | null
source_system: str | null    # e.g. "anvil", "claude-code"
source_mode: str | null      # e.g. "cli", "tui"
source_agent: str | null     # e.g. "dev", "qa"
hook_event_name: str | null
```

`SearchResult` (what `/api/recall` and `/api/observations/search` return):

```
observations: list[ObservationOut]   # full bodies
query: str
mode: "hybrid" | "vector" | "fts"
total: int
```

`ObservationOut` (one observation row):

```
id: int
session_id: int
project_id: int
project_name: str
title: str
subtitle: str | null
type: "discovery" | "bugfix" | "feature" | "refactor" | "decision" | "change" | "pattern" | "gotcha"
narrative: str | null
facts: list[str]
concepts: list[str]
files_read: list[str]
files_modified: list[str]
tool_name: str | null
source_system: str | null
source_mode: str | null
source_agent: str | null
created_at: timestamp
```

### Project scoping rules

Pass `project` as the **absolute cwd** of the project. The server
applies bidirectional prefix matching against `mem_projects.full_path`,
PLUS a basename fallback (so `~/Dropbox/_CODING/X → ~/_CODING/X`
checkout migrations don't break scope). The path you pass in is the
path you get scoped to.

`project=<cwd>` → that project's rows OR truly-global (`project_id IS NULL`).
`project=` missing → truly-global only.

This matters mostly for `/api/lessons` — the previous behavior was to
return all lessons when project was absent, which leaked cross-project
rules into the injection.

### Failure modes (host MUST handle)

| Condition | Recommended host behavior |
|---|---|
| 3s+ timeout fetching lessons | Drop the envelope, let the turn proceed |
| Server returns non-200 | Drop the envelope, log at debug |
| Server returns malformed JSON | Drop the envelope, log at debug |
| `/api/queue` POST fails | Drop the write, don't retry inline (the server has its own queue) |
| Health check fails on startup | Print a notice; degrade gracefully |
| Auth header rejected (401) | Stop calling endpoints, surface to user once, don't spam |

**Never block the LLM turn on agent-memory unavailability.** Lessons are
advisory; the agent runs without them.

### Idempotency

- Lessons envelope: the host SHOULD check the user message for an
  existing `<agent-memory>` marker before prepending. This prevents
  double-injection when multiple middlewares run, or when a turn
  re-enters the middleware stack mid-flight.
- `/api/queue`: the server dedupes on `(session_id, tool_call_id)` so
  retries are safe, but you should still avoid retrying inline — the
  server is the queue.

### Versioning + schema drift

The API is not yet versioned. Schema changes happen behind feature
work; check `git log app/models.py` if a payload starts looking unfamiliar.
The recipes above target the contract as of agent-memory `dev` HEAD
2026-05-19.

---

## What this guide deliberately omits

- **Direct DB access.** Don't query Postgres directly from your host —
  schema is unstable. The HTTP API is the contract.
- **Training data export** — has its own helper (`training_export_guide()`
  MCP tool / `GET /api/tool-calls/export/help`). Out of scope for runtime
  integration.
- **Lesson creation from your host.** Hosts MAY POST to `/api/lessons`,
  but lessons are usually authored by humans or by the LLM itself
  (via `create_lesson` MCP tool / `POST /api/lessons`). If you do create
  lessons from the host, scope them to the project and don't make them
  CRITICAL unless they should fire every turn.

---

## TL;DR if you only do one thing

Implement Recipe 1. Lessons injecting on every turn is the entire
load-bearing value proposition. Everything else can come later.
