---
name: mem-search
description: "Search agent-memory's persistent cross-session memory database. Use when user asks \"did we already solve this?\", \"how did we do X last time?\", or needs work from previous sessions."
argument-hint: "[search query]"
user-invocable: true
---

# Memory Search

Search past work across all coding sessions using the agent-memory MCP tools.

## When to Use

Use when users ask about **previous sessions** (not the current conversation):

- "Did we already fix this?"
- "How did we solve X last time?"
- "What happened last week?"
- "What decisions did we make about the auth system?"

## 3-Layer Workflow (ALWAYS Follow)

**NEVER fetch full details without filtering first. 10x token savings.**

### Step 1: Search — Get Index with IDs

Use the `search` MCP tool (server: agent-memory):

```
search(query="$ARGUMENTS", limit=20)
```

Returns: table with IDs, titles, types, scores (~50-100 tokens/result)

**Optional filters:**

| Param | Example | Description |
|-------|---------|-------------|
| `project` | `"myapp"` | Filter by project name |
| `type` | `"bugfix"` | Filter: discovery, bugfix, feature, refactor, decision, change, pattern, gotcha |
| `dateStart` | `"2026-02-01"` | ISO date range start |
| `dateEnd` | `"2026-02-12"` | ISO date range end |
| `limit` | `20` | Max results (default 20, max 50) |

### Step 2: Timeline — Get Context Around Interesting Results

Use the `timeline` MCP tool:

```
timeline(anchor=<ID>, depth_before=3, depth_after=3)
```

Or find anchor automatically:

```
timeline(query="authentication", depth_before=3, depth_after=3)
```

Shows what happened before and after a specific observation in the same session.

### Step 3: Fetch — Get Full Details ONLY for Filtered IDs

Review titles from Step 1. Pick relevant IDs. Discard the rest.

```
get_observations(ids=[11131, 10942])
```

Returns: complete observations with title, narrative, facts, concepts, files (~500-1000 tokens each)

**ALWAYS batch multiple IDs in one call.**

## Saving Memories

Use `save_memory` to store important findings for future sessions:

```
save_memory(text="Important discovery about the auth system", title="Auth Architecture", project="myapp")
```

## Examples

**Find recent bug fixes:**
```
search(query="bug", type="bugfix", limit=10)
```

**Find decisions made last week:**
```
search(query="architecture decision", type="decision", dateStart="2026-02-05")
```

**Understand context around a discovery:**
```
timeline(anchor=11131, depth_before=5, depth_after=5)
```

**Search for the user's query:**
```
search(query="$ARGUMENTS", limit=20)
```

## Execution

When invoked with `/mem-search`, immediately run:

```
search(query="$ARGUMENTS", limit=20)
```

Present results as a table. If the user wants details on specific results, proceed to steps 2 and 3.
