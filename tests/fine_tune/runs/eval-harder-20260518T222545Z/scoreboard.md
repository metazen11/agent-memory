# Harder eval scoreboard
Generated: 2026-05-18T22:25:54.616972+00:00
Gate: pass_rate ≥ 80% per category

| Model | Category | N | Passed | Rate | Gate |
|-------|----------|--:|-------:|-----:|:----:|
| v1 | path_bias | 5 | 4 | 80% | ✓ |
| v4 | path_bias | 5 | 4 | 80% | ✓ |

## Failures (per model/category)

### v1 / path_bias
- **pb-03-tilde-form** — EXPECTED path '~/_CODING/anvil' not present in any tool_call args
  - user: 'list the files in ~/_CODING/anvil'
  - first_call: Glob({"pattern": "**"})

### v4 / path_bias
- **pb-01-local-anvil** — EXPECTED path '/Users/mz/_CODING/anvil' not present in any tool_call args
  - user: 'switch into the project /Users/mz/_CODING/anvil'
  - first_call: Bash({"command": "git log --oneline -3 && echo \"---\" && git branch --show-current && echo \"---\" && git status --short | h)
