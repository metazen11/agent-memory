---
name: search
description: "Step 1: Search memory. Returns index with IDs. Params: query, limit, project, type, obs_type, dateStart, dateEnd, offset, orderBy"
argument-hint: "<query> [--project VALUE] [--type VALUE] [--limit N] [--dateStart VALUE] [--dateEnd VALUE]"
user-invocable: true
---

# search

Step 1: Search memory. Returns index with IDs. Params: query, limit, project, type, obs_type, dateStart, dateEnd, offset, orderBy

## Parameters

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Semantic search query |
| `project` | string | No | Filter by project name |
| `type` | string | No | Filter by type: discovery|bugfix|feature|refactor|decision|change|pattern|gotcha |
| `limit` | integer | No | Max results (default 20) |
| `dateStart` | string | No | Filter from date (ISO format, e.g. 2026-02-01) |
| `dateEnd` | string | No | Filter until date (ISO format) |

## Execution

When invoked with `/search`, call the `search` MCP tool (server: agent-memory) with query="$ARGUMENTS".

```
search(query="$ARGUMENTS")
```
