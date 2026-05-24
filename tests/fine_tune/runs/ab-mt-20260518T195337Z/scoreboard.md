# Multi-turn adaptation scoreboard
Generated: 2026-05-18T19:53:52.588209+00:00
Scenarios: 1, harness: llama-server :9099

| Model | N | NoTurn1Call | EmptyTurn2 | Regression | NewCall | TextAns | Adaptation |
|-------|--:|------------:|-----------:|-----------:|--------:|--------:|-----------:|
| v4 | 1 | 0 | 0 | 0 | 0 | 1 | 100% |

## Ship gate
- adaptation_rate ≥ 60% AND regression_same_call ≤ floor(N/5)
