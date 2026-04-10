# Skill: Dataset Curation

Goal: Build clean instruction-response examples from raw coding logs and tool traces.

Checklist:
1. Keep originals in `data/raw/` only.
2. Remove secrets/tokens from responses before training.
3. Dedupe exact instruction-response pairs.
4. Exclude empty/very short turns.
5. Split train/val and capture stats in `data/processed/fine_tune/stats.json`.
