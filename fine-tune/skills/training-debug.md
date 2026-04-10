# Skill: Training Debug

Goal: Diagnose local fine-tune failures quickly.

Checklist:
1. Inspect `fine-tune/outputs/train.log` first.
2. Classify issue: memory, dependency, auth, path, network.
3. Apply smallest config reduction for OOM: seq_len, batch, rank.
4. Re-run with one change at a time and capture diffs in log.
