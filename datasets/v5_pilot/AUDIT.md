# v5 pilot dataset audit

- config: `/Users/mz/_CODING/agentMemory/configs/v5_pilot.yaml`
- target_clean_rows: 5000
- source_window: 15000

## Funnel

| stage | count |
|---|---|
| db rows pulled | 15000 |
| dropped: excluded project | 274 |
| dropped: unknown project | 0 |
| dropped: cwd excluded prefix (/tmp etc) | 0 |
| dropped: cwd outside workspace | 923 |
| dropped: empty after strip | 0 |
| dropped: continuation prompt | 840 |
| dropped: bad tool_input | 0 |
| dropped: too long (>4k chars) | 2199 |
| dropped: duplicate | 60 |
| **kept** | **5000** |

## Per-project kept

| project | rows |
|---|---|
| fire-map.wfca.com | 1793 |
| psde_mz_test | 1744 |
| anvil | 672 |
| agentMemory | 470 |
| mz-personal-archived | 321 |

## Per-subfolder kept (top 30)

| project/subfolder | rows |
|---|---|
| fire-map.wfca.com/wfca-app | 1495 |
| fire-map.wfca.com/aws-infrastructure | 108 |
| fire-map.wfca.com/wfca-etl | 45 |
| agentMemory/.claude | 27 |
| psde_mz_test/dbt | 7 |

## Per-project dropped (excluded)

| project | rows |
|---|---|
| anvil | 859 |
| my-repo | 187 |
| mz-personal-archived | 64 |
| my-project | 46 |
| DailyDispatch.local | 20 |
| lib | 19 |
| b | 1 |
| a | 1 |

## Sample rendered rows (first 3)

### Sample 1 — psde_mz_test (id=60650)

```json
{
  "messages": [
    {
      "role": "system",
      "content": "YOU ARE ANVIL - A Coding orchestrator agent - Use agents via the anvil tool to delegate your coding work and conserve context.\n\nWhen You Write a file, give the user the path. Always display and link files you create or edit.\n\n### CODE TESTING ABSOLUTE LAW\nAfter Coding / Tool Calls\n[ ] YOU MUST TEST all code after writing or modifying it\n[ ] YOU MUST verify the code runs without errors\n[ ] YOU MUST ensure no existing functionality is broken\n[ ] YOU MUST confirm linting/type checks pass (if applicable)\n[ ] YOU MUST review the output and behavior after execution\n[ ] You MUST Commit your changes to the respective account and repository\n\n### File Awareness & Context\n[ ] Always read and review all *.md files in the root working directory before starting\n[ ] Always read a file before editing it (it may have changed)\n[ ] Verify exact file paths before performing any operation\n[ ] NEVER hard code paths or values — use canonical settings variables\n[ ] Do not assume directory structure — confirm it\n\n### Clarification & Assumptions\n[ ] If requirements are unclear, ask before proceeding\n[ ] Do not make silent assumptions about functionality or intent\n[ ] Confirm edge cases when behavior is ambiguous\n\n### Code Standards & Approach\n[ ] Prefer established libraries and frameworks over building custom solutions\n[ ] Use canonical naming conventions\n[ ] Follow existing project patterns and architecture\n\n### Editing & Change Safety\n[ ] Make minimal, targeted changes — avoid unnecessary refactors\n[ ] Preserve existing functionality unless explicitly modifying it\n[ ] Check for dependencies and side effects before editing\n\n### Debugging Mindset\n[ ] Read error messages carefully\n[ ] Trace root cause (not just symptoms)\n[ ] Fix the underlying issue, not just the output\n\n### Communication & Updates\n[ ] Provide updates after each significant action\n[ ] Clearly state what was done and what changed\n[ ] Note any issues encountered\n[ ] Keep communication concise but clear\n\n### Execution Discipline\n[ ] Think before acting — do not rush into code changes\n[ ] Break problems into steps before implementation\n[ ] Validate each step before moving forward\n\n### One-Line Operating Principle\nRead first → Think second → Verify paths → Use proven tools → Make minimal changes → TEST EVERYTHING → Communicate clearly\n\n[Project]\nproject_name: psde_mz_test\nproject_root: <TRUSTED_ROOT>/psde_mz_test\ncwd: <TRUSTED_ROOT>/psde_mz_test"
    },
    {
      "role": "user",
      "content": "ok can you keep going?"
    },
    {
      "role": "assistant",
      "tool_calls": [
        {
          "type": "function",
          "function": {
            "name": "Bash",
            "arguments": {
              "command": "git status -s | wc -l && echo \"---\" && ls plans/*completion-audit*.json 2>&1 | head -5",
              "description": "Check if auditor agent has written verdicts yet"
            }
          }
        }
      ]
    },
    {
      "role": "tool",
      "name": "Bash",
      "content": "       0---plans/487-completion-audit.jsonplans/488-completion-audit.json"
    }
  ],
  "tools": [],
  "project_tag": "psde_mz_test",
  "multi_turn": false,
  "source_ids": [
    60650
  ]
}
```

### Sample 2 — psde_mz_test (id=60649)

```json
{
  "messages": [
    {
      "role": "system",
      "content": "YOU ARE ANVIL - A Coding orchestrator agent - Use agents via the anvil tool to delegate your coding work and conserve context.\n\nWhen You Write a file, give the user the path. Always display and link files you create or edit.\n\n### CODE TESTING ABSOLUTE LAW\nAfter Coding / Tool Calls\n[ ] YOU MUST TEST all code after writing or modifying it\n[ ] YOU MUST verify the code runs without errors\n[ ] YOU MUST ensure no existing functionality is broken\n[ ] YOU MUST confirm linting/type checks pass (if applicable)\n[ ] YOU MUST review the output and behavior after execution\n[ ] You MUST Commit your changes to the respective account and repository\n\n### File Awareness & Context\n[ ] Always read and review all *.md files in the root working directory before starting\n[ ] Always read a file before editing it (it may have changed)\n[ ] Verify exact file paths before performing any operation\n[ ] NEVER hard code paths or values — use canonical settings variables\n[ ] Do not assume directory structure — confirm it\n\n### Clarification & Assumptions\n[ ] If requirements are unclear, ask before proceeding\n[ ] Do not make silent assumptions about functionality or intent\n[ ] Confirm edge cases when behavior is ambiguous\n\n### Code Standards & Approach\n[ ] Prefer established libraries and frameworks over building custom solutions\n[ ] Use canonical naming conventions\n[ ] Follow existing project patterns and architecture\n\n### Editing & Change Safety\n[ ] Make minimal, targeted changes — avoid unnecessary refactors\n[ ] Preserve existing functionality unless explicitly modifying it\n[ ] Check for dependencies and side effects before editing\n\n### Debugging Mindset\n[ ] Read error messages carefully\n[ ] Trace root cause (not just symptoms)\n[ ] Fix the underlying issue, not just the output\n\n### Communication & Updates\n[ ] Provide updates after each significant action\n[ ] Clearly state what was done and what changed\n[ ] Note any issues encountered\n[ ] Keep communication concise but clear\n\n### Execution Discipline\n[ ] Think before acting — do not rush into code changes\n[ ] Break problems into steps before implementation\n[ ] Validate each step before moving forward\n\n### One-Line Operating Principle\nRead first → Think second → Verify paths → Use proven tools → Make minimal changes → TEST EVERYTHING → Communicate clearly\n\n[Project]\nproject_name: psde_mz_test\nproject_root: <TRUSTED_ROOT>/psde_mz_test\ncwd: <TRUSTED_ROOT>/psde_mz_test"
    },
    {
      "role": "user",
      "content": "there needs to be a visual interface to all of this which should be in the goal otherwise it is useless - it needs to be in the unification layer / django app"
    },
    {
      "role": "assistant",
      "tool_calls": [
        {
          "type": "function",
          "function": {
            "name": "TaskUpdate",
            "arguments": {
              "status": "completed",
              "taskId": "32"
            }
          }
        }
      ]
    },
    {
      "role": "tool",
      "name": "TaskUpdate",
      "content": "Updated task #32 status"
    }
  ],
  "tools": [],
  "project_tag": "psde_mz_test",
  "multi_turn": false,
  "source_ids": [
    60649
  ]
}
```

### Sample 3 — psde_mz_test (id=60648)

```json
{
  "messages": [
    {
      "role": "system",
      "content": "YOU ARE ANVIL - A Coding orchestrator agent - Use agents via the anvil tool to delegate your coding work and conserve context.\n\nWhen You Write a file, give the user the path. Always display and link files you create or edit.\n\n### CODE TESTING ABSOLUTE LAW\nAfter Coding / Tool Calls\n[ ] YOU MUST TEST all code after writing or modifying it\n[ ] YOU MUST verify the code runs without errors\n[ ] YOU MUST ensure no existing functionality is broken\n[ ] YOU MUST confirm linting/type checks pass (if applicable)\n[ ] YOU MUST review the output and behavior after execution\n[ ] You MUST Commit your changes to the respective account and repository\n\n### File Awareness & Context\n[ ] Always read and review all *.md files in the root working directory before starting\n[ ] Always read a file before editing it (it may have changed)\n[ ] Verify exact file paths before performing any operation\n[ ] NEVER hard code paths or values — use canonical settings variables\n[ ] Do not assume directory structure — confirm it\n\n### Clarification & Assumptions\n[ ] If requirements are unclear, ask before proceeding\n[ ] Do not make silent assumptions about functionality or intent\n[ ] Confirm edge cases when behavior is ambiguous\n\n### Code Standards & Approach\n[ ] Prefer established libraries and frameworks over building custom solutions\n[ ] Use canonical naming conventions\n[ ] Follow existing project patterns and architecture\n\n### Editing & Change Safety\n[ ] Make minimal, targeted changes — avoid unnecessary refactors\n[ ] Preserve existing functionality unless explicitly modifying it\n[ ] Check for dependencies and side effects before editing\n\n### Debugging Mindset\n[ ] Read error messages carefully\n[ ] Trace root cause (not just symptoms)\n[ ] Fix the underlying issue, not just the output\n\n### Communication & Updates\n[ ] Provide updates after each significant action\n[ ] Clearly state what was done and what changed\n[ ] Note any issues encountered\n[ ] Keep communication concise but clear\n\n### Execution Discipline\n[ ] Think before acting — do not rush into code changes\n[ ] Break problems into steps before implementation\n[ ] Validate each step before moving forward\n\n### One-Line Operating Principle\nRead first → Think second → Verify paths → Use proven tools → Make minimal changes → TEST EVERYTHING → Communicate clearly\n\n[Project]\nproject_name: psde_mz_test\nproject_root: <TRUSTED_ROOT>/psde_mz_test\ncwd: <TRUSTED_ROOT>/psde_mz_test"
    },
    {
      "role": "user",
      "content": "there needs to be a visual interface to all of this which should be in the goal otherwise it is useless - it needs to be in the unification layer / django app"
    },
    {
      "role": "assistant",
      "tool_calls": [
        {
          "type": "function",
          "function": {
            "name": "TaskUpdate",
            "arguments": {
              "status": "completed",
              "taskId": "31"
            }
          }
        }
      ]
    },
    {
      "role": "tool",
      "name": "TaskUpdate",
      "content": "Updated task #31 status"
    }
  ],
  "tools": [],
  "project_tag": "psde_mz_test",
  "multi_turn": false,
  "source_ids": [
    60648
  ]
}
```
