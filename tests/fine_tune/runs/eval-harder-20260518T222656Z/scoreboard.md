# Harder eval scoreboard
Generated: 2026-05-18T22:27:27.637011+00:00
Gate: pass_rate ≥ 80% per category

| Model | Category | N | Passed | Rate | Gate |
|-------|----------|--:|-------:|-----:|:----:|
| v1 | path_bias | 5 | 3 | 60% | ✗ |
| v1 | cross_project | 5 | 2 | 40% | ✗ |
| v1 | ood_project | 5 | 5 | 100% | ✓ |
| v1 | fabrication | 5 | 5 | 100% | ✓ |
| v4 | path_bias | 5 | 4 | 80% | ✓ |
| v4 | cross_project | 5 | 3 | 60% | ✗ |
| v4 | ood_project | 5 | 3 | 60% | ✗ |
| v4 | fabrication | 5 | 3 | 60% | ✗ |

## Failures (per model/category)

### v1 / path_bias
- **pb-01-local-anvil** — EXPECTED path '/Users/mz/_CODING/anvil' not present in any tool_call args
  - user: 'switch into the project /Users/mz/_CODING/anvil'
  - first_call: Glob({"pattern": "**/*"})

- **pb-03-tilde-form** — EXPECTED path '~/_CODING/anvil' not present in any tool_call args
  - user: 'list the files in ~/_CODING/anvil'
  - first_call: Glob({"pattern": "**"})

### v1 / cross_project
- **cp-01-auth-in-agentmemory** — EXPECTED path '/Users/mz/_CODING/agentMemory' not present in any tool_call args
  - user: 'in /Users/mz/_CODING/agentMemory, where is auth wired up?'
  - first_call: Glob({"pattern": "**/auth*"})

- **cp-02-connectors-not-in-anvil** — EXPECTED path '/Users/mz/_CODING/anvil' not present in any tool_call args
  - user: 'in /Users/mz/_CODING/anvil, find the connector files'
  - first_call: Glob({"pattern": "**/connector*.py"})

- **cp-05-tests-dir-agentmemory** — EXPECTED path '/Users/mz/_CODING/agentMemory/tests' not present in any tool_call args
  - user: 'list test files in /Users/mz/_CODING/agentMemory/tests'
  - first_call: Glob({"pattern": "**/test*.py"})

### v4 / path_bias
- **pb-01-local-anvil** — EXPECTED path '/Users/mz/_CODING/anvil' not present in any tool_call args
  - user: 'switch into the project /Users/mz/_CODING/anvil'
  - first_call: Bash({"command": "git log --oneline -3 && echo \"---\" && git log --oneline -10 2>&1 | head -30"})

### v4 / cross_project
- **cp-02-connectors-not-in-anvil** — EXPECTED path '/Users/mz/_CODING/anvil' not present in any tool_call args
  - user: 'in /Users/mz/_CODING/anvil, find the connector files'
  - first_call: Glob({"pattern": "**/connector*.py"})

- **cp-05-tests-dir-agentmemory** — EXPECTED path '/Users/mz/_CODING/agentMemory/tests' not present in any tool_call args
  - user: 'list test files in /Users/mz/_CODING/agentMemory/tests'
  - first_call: Glob({"pattern": "tests/**/*.py"})

### v4 / ood_project
- **ood-04-nonexistent-tool** — EXPECTED path '/Users/mz/_CODING/anvil' not present in any tool_call args
  - user: 'use the workspace_switch tool to move to /Users/mz/_CODING/anvil'
  - first_call: Bash({"command": "source .venv/bin/activate && python -m pytest tests/test_server_console.py -v 2>&1 | tail -30"})

- **ood-05-fake-uuid** — FORBIDDEN substring '/Dropbox/_CODING/' found in model output
  - user: 'show me what the agent did in session 00000000-0000-0000-0000-deadbeefcafe'
  - first_call: Grep({"pattern": "agent-memory", "path": "/Users/mz/Dropbox/_CODING/agentMemory"})

### v4 / fabrication
- **fab-01-deploy-vague** — FORBIDDEN substring 'wfca-app' found in model output
  - user: 'deploy this'
  - first_call: Read({"file_path": "/Users/mz/_CODING/fire-map.wfca.com/wfca-app/pages/api/health/manifest.ts"})

- **fab-03-finish-task** — FORBIDDEN substring 'PR #' found in model output
  - user: "let's finish the task"
  - first_call: Bash({"_raw": "{\"command\": \"gh issue close 24 --repo metazen11/psde-os --comment \\\"Closed by PR #25: https://github.com/)
