#!/usr/bin/env python3
"""Generate a markdown run report from a training run directory.

Reads HF Trainer's `trainer_state.json` and the run_meta.json sidecar, emits
docs/training_runs/M-FT-1-<UTC>.md with loss curves (ASCII sparkline + table),
eval metrics, wall-clock, GGUF SHAs (if present), and the canonical artifact
paths.

Usage:
    python scripts/fine_tune/generate_run_report.py \\
        --run-dir models/lora/qwen2.5-3b-instruct-toolcalls-lora/latest

    # Or pick the latest automatically:
    python scripts/fine_tune/generate_run_report.py --slug qwen2.5-3b-instruct
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).absolute().parent))

from lib import REPO_ROOT, get_model, sha256_file, utc_stamp  # noqa: E402


def sparkline(values: list[float]) -> str:
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    # NaN-safe: NaN renders as '?' and is excluded from min/max
    finite = [v for v in values if isinstance(v, (int, float)) and v == v]  # NaN != NaN
    if not finite:
        return "?" * len(values)
    lo, hi = min(finite), max(finite)
    if hi - lo < 1e-9:
        return blocks[0] * len(values)
    def cell(v):
        if not (v == v):
            return "?"
        return blocks[min(7, int((v - lo) / (hi - lo) * 7))]
    return "".join(cell(v) for v in values)


def load_trainer_state(run_dir: Path) -> dict:
    # HF Trainer may write trainer_state.json directly in run_dir OR inside
    # the last checkpoint-N/ subdir
    candidates = [run_dir / "trainer_state.json"]
    candidates.extend(sorted(run_dir.glob("checkpoint-*/trainer_state.json")))
    for p in reversed(candidates):
        if p.exists():
            with p.open() as f:
                return json.load(f)
    raise FileNotFoundError(f"no trainer_state.json under {run_dir}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-dir", help="Path to a runs/<UTC>/ dir")
    g.add_argument("--slug", help="Model slug; uses lora_dir/latest")
    p.add_argument("--gguf-q4km", help="Optional path to Q4_K_M GGUF for SHA capture")
    p.add_argument("--gguf-f16", help="Optional path to f16 GGUF for SHA capture")
    p.add_argument("--out", help="Output markdown path (default: docs/training_runs/M-FT-1-<UTC>.md)")
    args = p.parse_args()

    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        spec = get_model(args.slug)
        run_dir = spec.lora_dir / "latest"

    if not run_dir.exists():
        sys.exit(f"FAIL: {run_dir} does not exist")

    meta_p = run_dir / "run_meta.json"
    run_meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    trainer_state = load_trainer_state(run_dir)

    history = trainer_state.get("log_history", [])
    train_logs = [h for h in history if "loss" in h and "eval_loss" not in h]
    eval_logs = [h for h in history if "eval_loss" in h]

    train_losses = [h["loss"] for h in train_logs]
    eval_losses = [h["eval_loss"] for h in eval_logs]
    last_train_loss = train_losses[-1] if train_losses else None
    last_eval_loss = eval_losses[-1] if eval_losses else None
    train_runtime = trainer_state.get("train_runtime")
    epoch_final = trainer_state.get("epoch")
    global_step = trainer_state.get("global_step")

    gguf_lines = []
    for label, path in (("Q4_K_M", args.gguf_q4km), ("f16", args.gguf_f16)):
        if path and Path(path).exists():
            gguf_lines.append(f"- **{label}**: `{path}` — sha256 `{sha256_file(Path(path))[:16]}…`")

    out_path = Path(args.out) if args.out else (
        REPO_ROOT / "docs" / "training_runs" / f"M-FT-1-{utc_stamp()}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    md = []
    md.append(f"# Training run report — {run_dir.name}")
    md.append("")
    md.append(f"_Generated: {utc_stamp()} UTC_")
    md.append("")
    md.append("## Run metadata")
    md.append("")
    if run_meta:
        md.append("| key | value |")
        md.append("|---|---|")
        for k in sorted(run_meta):
            md.append(f"| {k} | `{run_meta[k]}` |")
        md.append("")
    md.append("## Outcome")
    md.append("")
    md.append(f"- **Global steps:** {global_step}")
    md.append(f"- **Final epoch:** {epoch_final}")
    if train_runtime:
        md.append(f"- **Train runtime:** {train_runtime:.1f}s ({train_runtime/60:.1f} min)")
    if last_train_loss is not None:
        md.append(f"- **Final train loss:** {last_train_loss:.4f}")
    if last_eval_loss is not None:
        md.append(f"- **Final eval loss:** {last_eval_loss:.4f}")
    md.append("")
    if train_losses:
        md.append("## Train loss curve")
        md.append("")
        md.append("```")
        md.append(f"{sparkline(train_losses)}   {len(train_losses)} samples, "
                  f"{train_losses[0]:.3f} → {train_losses[-1]:.3f}")
        md.append("```")
        md.append("")
        md.append("| step | epoch | loss | lr | grad_norm |")
        md.append("|---:|---:|---:|---:|---:|")
        for h in train_logs:
            md.append(f"| {h.get('step', '?')} | {h.get('epoch', '?'):.3f} | "
                      f"{h.get('loss', '?'):.4f} | {h.get('learning_rate', 0):.2e} | "
                      f"{h.get('grad_norm', 0):.3f} |")
        md.append("")
    if eval_losses:
        md.append("## Eval loss curve")
        md.append("")
        md.append("```")
        md.append(f"{sparkline(eval_losses)}   {len(eval_losses)} eval points, "
                  f"{eval_losses[0]:.3f} → {eval_losses[-1]:.3f}")
        md.append("```")
        md.append("")
        md.append("| step | epoch | eval_loss | eval_runtime |")
        md.append("|---:|---:|---:|---:|")
        for h in eval_logs:
            md.append(f"| {h.get('step', '?')} | {h.get('epoch', '?'):.3f} | "
                      f"{h.get('eval_loss', '?'):.4f} | {h.get('eval_runtime', 0):.1f}s |")
        md.append("")
    if gguf_lines:
        md.append("## GGUF artifacts")
        md.append("")
        md.extend(gguf_lines)
        md.append("")
    md.append("## Adapter")
    md.append("")
    md.append(f"- `{run_dir.relative_to(REPO_ROOT) if run_dir.is_relative_to(REPO_ROOT) else run_dir}`")
    md.append("")

    out_path.write_text("\n".join(md))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
