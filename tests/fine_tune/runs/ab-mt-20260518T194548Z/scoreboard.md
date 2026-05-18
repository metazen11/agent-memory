# Multi-turn adaptation scoreboard
Generated: 2026-05-18T19:48:43.285606+00:00
Scenarios: 5

| Model | N | NoTurn1Call | RegressionSameCall | AdaptedNewCall | AdaptedTextAns | Adaptation |
|-------|--:|------------:|-------------------:|---------------:|---------------:|-----------:|
| v1 | 5 | 1 | 0 | 1 | 3 | 100% |
| v3 | 5 | 0 | 3 | 2 | 0 | 40% |
| v4 | 5 | 0 | 2 | 2 | 1 | 60% |

## Ship gate
- v4 adaptation ≥ 60% AND v4 regression_same_call ≤ v1 baseline
- (v3 expected to fail this gate — that's why we built v4)