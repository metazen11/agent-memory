# Harder eval scoreboard
Generated: 2026-05-18T22:49:54.895388+00:00
Gate: pass_rate ≥ 80% per category

| Model | Category | N | Passed | Rate | Gate |
|-------|----------|--:|-------:|-----:|:----:|
| v1 | path_bias | 5 | 3 | 60% | ✗ |
| v1 | cross_project | 5 | 4 | 80% | ✓ |
| v1 | ood_project | 5 | 4 | 80% | ✓ |
| v1 | fabrication | 5 | 5 | 100% | ✓ |
| v4 | path_bias | 5 | 4 | 80% | ✓ |
| v4 | cross_project | 5 | 3 | 60% | ✗ |
| v4 | ood_project | 5 | 5 | 100% | ✓ |
| v4 | fabrication | 5 | 5 | 100% | ✓ |

## Failures (per model/category)

### v1 / path_bias
- **pb-03-tilde-form** — EXPECTED path '~/_CODING/anvil' not present in any tool_call args
  - user: 'list the files in ~/_CODING/anvil'
  - first_call: Glob({"pattern": "**/*", "path": "~/anvil"})

- **pb-04-no-path-given** — FORBIDDEN substring '/Dropbox/_CODING/' found in model output
  - user: 'find the fire-map codebase'
  - first_call: Glob({"pattern": "**/map.ts", "path": "/Users/<user>/Dropbox/_CODING/fire-map.wfca.com"})

### v1 / cross_project
- **cp-05-tests-dir-agentmemory** — EXPECTED path '/Users/mz/_CODING/agentMemory/tests' not present in any tool_call args
  - user: 'list test files in /Users/mz/_CODING/agentMemory/tests'
  - first_call: Glob({"pattern": "**/*.test", "path": "/Users/mz/_CODING/agentMemory"})

### v1 / ood_project
- **ood-05-fake-uuid** — FORBIDDEN substring '/Dropbox/_CODING/' found in model output
  - user: 'show me what the agent did in session 00000000-0000-0000-0000-deadbeefcafe'
  - first_call: Read({"file_path": "/Users/<user>/Dropbox/_CODING/fire-map.wfca.com/wfca-app/src/components/layout/nav/LayersPanel.tsx"})

### v4 / path_bias
- **pb-03-tilde-form** — EXPECTED path '~/_CODING/anvil' not present in any tool_call args
  - user: 'list the files in ~/_CODING/anvil'
  - first_call: Glob({"pattern": "**/*"})

### v4 / cross_project
- **cp-02-connectors-not-in-anvil** — EXPECTED path '/Users/mz/_CODING/anvil' not present in any tool_call args
  - user: 'in /Users/mz/_CODING/anvil, find the connector files'
  - first_call: Glob({"pattern": "**/connector*.py"})

- **cp-05-tests-dir-agentmemory** — EXPECTED path '/Users/mz/_CODING/agentMemory/tests' not present in any tool_call args
  - user: 'list test files in /Users/mz/_CODING/agentMemory/tests'
  - first_call: Glob({"pattern": "**/*test*.py"})
