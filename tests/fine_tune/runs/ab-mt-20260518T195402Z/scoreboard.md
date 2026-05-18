# Multi-turn adaptation scoreboard
Generated: 2026-05-18T19:56:29.127158+00:00
Scenarios: 10, harness: llama-server :9099

| Model | N | NoTurn1Call | EmptyTurn2 | Regression | NewCall | TextAns | Adaptation |
|-------|--:|------------:|-----------:|-----------:|--------:|--------:|-----------:|
| v1 | 10 | 0 | 0 | 1 | 8 | 1 | 90% |
| v3 | 10 | 0 | 0 | 3 | 7 | 0 | 70% |
| v4 | 10 | 0 | 0 | 0 | 0 | 10 | 100% |

## Ship gate
- adaptation_rate ≥ 60% AND regression_same_call ≤ floor(N/5)
