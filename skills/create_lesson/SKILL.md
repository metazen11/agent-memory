---
name: create_lesson
description: "Create a lesson — a proactive rule that fires BEFORE risky operations. Unlike observations (passive), lessons are instructions injected at session start and triggered by PreToolUse hooks. Use after learning from a mistake."
argument-hint: "<rule> [--title VALUE] [--severity VALUE] [--project VALUE] [--trigger_tool VALUE] [--trigger_pattern VALUE]"
user-invocable: true
---

# create lesson

Create a lesson — a proactive rule that fires BEFORE risky operations. Unlike observations (passive), lessons are instructions injected at session start and triggered by PreToolUse hooks. Use after learning from a mistake.

## Parameters

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `rule` | string | Yes | The instruction/rule (e.g. 'ALWAYS diff dev vs prod config before deploying') |
| `title` | string | No | Short title for the lesson |
| `severity` | string | No | How important (default: warning) |
| `project` | string | **Yes** | Project/folder name — ALWAYS pass the current project name. Omit ONLY for truly global lessons. |
| `trigger_tool` | string | No | Tool to match: Bash, Edit, Write, NotebookEdit (omit for any) |
| `trigger_pattern` | string | No | Regex to match against tool input (e.g. 'amplify.*update-app') |

## Execution

When invoked with `/create_lesson`, call the `create_lesson` MCP tool (server: agent-memory) with rule="$ARGUMENTS".
IMPORTANT: Always include the `project` parameter set to the current project folder name (from the session-start context).

```
create_lesson(rule="$ARGUMENTS", project="<current_project>")
```
