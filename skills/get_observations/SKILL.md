---
name: get_observations
description: "Step 3: Fetch full details for filtered IDs. Params: ids (array of observation IDs, required), orderBy, limit, project"
argument-hint: "<ids>"
user-invocable: true
---

# get observations

Step 3: Fetch full details for filtered IDs. Params: ids (array of observation IDs, required), orderBy, limit, project

## Parameters

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `ids` | array | Yes | Array of observation IDs to fetch (required) |

## Execution

When invoked with `/get_observations`, call the `get_observations` MCP tool (server: agent-memory) with ids="$ARGUMENTS".

```
get_observations(ids="$ARGUMENTS")
```
