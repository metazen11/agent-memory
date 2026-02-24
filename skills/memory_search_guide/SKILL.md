---
name: memory_search_guide
description: "3-LAYER WORKFLOW (ALWAYS FOLLOW):\n1. search(query) → Get index with IDs (~50-100 tokens/result)\n2. timeline(anchor=ID) → Get context around interesting results\n3. get_observations([IDs]) → Fetch full details ONLY for filtered IDs\nNEVER fetch full details without filtering first. 10x token savings."
argument-hint: ""
user-invocable: true
---

# memory search guide

3-LAYER WORKFLOW (ALWAYS FOLLOW):\n1. search(query) → Get index with IDs (~50-100 tokens/result)\n2. timeline(anchor=ID) → Get context around interesting results\n3. get_observations([IDs]) → Fetch full details ONLY for filtered IDs\nNEVER fetch full details without filtering first. 10x token savings.

## Execution

When invoked with `/memory_search_guide`, call the `memory_search_guide` MCP tool (server: agent-memory).

```
memory_search_guide()
```
