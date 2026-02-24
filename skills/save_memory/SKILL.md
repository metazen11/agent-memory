---
name: save_memory
description: "Save a manual memory/observation for semantic search. Use this to remember important information."
argument-hint: "<text> [--title VALUE] [--project VALUE]"
user-invocable: true
---

# save memory

Save a manual memory/observation for semantic search. Use this to remember important information.

## Parameters

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | Yes | Content to remember (required) |
| `title` | string | No | Short title (auto-generated from text if omitted) |
| `project` | string | No | Project name (uses 'manual' if omitted) |

## Execution

When invoked with `/save_memory`, call the `save_memory` MCP tool (server: agent-memory) with text="$ARGUMENTS".

```
save_memory(text="$ARGUMENTS")
```
