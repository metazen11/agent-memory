#!/usr/bin/env python3
"""Continuous RL-style data loop for agentMemory.

Loop stages:
1) Build fresh scored episodes from tool history.
2) Slice into high-reward (chosen) and lower-reward (rejected) sets.
3) Emit preference pairs for DPO/ORPO style training.
4) Emit summary metrics for iteration tracking.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _build_pairs(episodes: list[dict], high_reward: float, low_reward: float) -> tuple[list[dict], dict]:
    chosen = [e for e in episodes if e.get("reward", -999) >= high_reward]
    rejected = [e for e in episodes if e.get("reward", 999) <= low_reward]
    mode = "threshold"

    # Fallback for small/flat batches: take top/bottom quartile by reward.
    if not chosen or not rejected:
        ranked = sorted(episodes, key=lambda x: x.get("reward", 0.0))
        if ranked:
            q = max(1, len(ranked) // 4)
            rejected = ranked[:q]
            chosen = list(reversed(ranked[-q:]))
            mode = "quartile_fallback"

    pairs: list[dict] = []
    n = min(len(chosen), len(rejected))
    for idx in range(n):
        c = chosen[idx]
        r = rejected[idx]
        prompt = c.get("prompt_text") or r.get("prompt_text") or ""
        pairs.append(
            {
                "dataset_type": "preference",
                "prompt_text": prompt,
                "chosen": c,
                "rejected": r,
            }
        )
    return pairs, {"pair_mode": mode, "chosen_pool": len(chosen), "rejected_pool": len(rejected)}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create preference pairs from scored RL episodes")
    p.add_argument("--episodes", required=True, help="Path to scored episodes JSONL")
    p.add_argument("--high-reward", type=float, default=2.0)
    p.add_argument("--low-reward", type=float, default=1.0)
    p.add_argument("--output-dir", default="data/processed/rl")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    episodes_path = Path(args.episodes)
    episodes = _read_jsonl(episodes_path)

    pairs, pair_meta = _build_pairs(episodes, args.high_reward, args.low_reward)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir)
    pairs_file = out_dir / f"preference_pairs_{ts}.jsonl"
    summary_file = out_dir / f"preference_pairs_summary_{ts}.json"

    _write_jsonl(pairs_file, pairs)

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "episodes_input": str(episodes_path),
        "episodes_count": len(episodes),
        "pairs_count": len(pairs),
        "high_reward": args.high_reward,
        "low_reward": args.low_reward,
        "pairs_file": str(pairs_file),
        **pair_meta,
    }
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
