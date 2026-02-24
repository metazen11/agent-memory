---
name: search_lessons
description: "Search existing lessons (proactive rules). Use to check if a lesson already exists before creating one."
argument-hint: "<query> [--project VALUE] [--limit N]"
user-invocable: true
---

# search lessons

Search existing lessons (proactive rules). Use to check if a lesson already exists before creating one.

## Parameters

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Search query |
| `project` | string | No | Filter by project name |
| `limit` | integer | No | Max results (default 10) |

## Execution

When invoked with `/search_lessons`, call the `search_lessons` MCP tool (server: agent-memory) with query="$ARGUMENTS".

```
search_lessons(query="$ARGUMENTS")
```
