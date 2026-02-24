---
name: timeline
description: "Step 2: Get context around results. Params: anchor (observation ID) OR query (finds anchor automatically), depth_before, depth_after, project"
argument-hint: "[--anchor N] [--query VALUE] [--depth_before N] [--depth_after N] [--project VALUE]"
user-invocable: true
---

# timeline

Step 2: Get context around results. Params: anchor (observation ID) OR query (finds anchor automatically), depth_before, depth_after, project

## Parameters

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `anchor` | integer | No | Observation ID to center on |
| `query` | string | No | Find anchor automatically by searching for this query |
| `depth_before` | integer | No | Observations before (default 3) |
| `depth_after` | integer | No | Observations after (default 3) |
| `project` | string | No | Filter by project name |

## Execution

When invoked with `/timeline`, call the `timeline` MCP tool (server: agent-memory).

```
timeline()
```
