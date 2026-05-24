# Multi-turn adaptation scoreboard
Generated: 2026-05-18T21:02:00.518186+00:00
Scenarios: 10, harness: llama-server :9099

| Model | N | NoTurn1Call | EmptyTurn2 | Regression | NewCall | TextAns | Adaptation |
|-------|--:|------------:|-----------:|-----------:|--------:|--------:|-----------:|
| v1 | 10 | 0 | 0 | 2 | 7 | 1 | 80% |
| v4-ckpt4750 | 10 | 0 | 0 | 0 | 0 | 10 | 100% |
| v4-ckpt5000 | 10 | 0 | 0 | 0 | 0 | 10 | 100% |
| v4 | 10 | 0 | 0 | 0 | 0 | 10 | 100% |

## Ship gate
- adaptation_rate ≥ 60% AND regression_same_call ≤ floor(N/5)
