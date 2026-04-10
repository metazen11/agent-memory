#!/usr/bin/env python3
"""Train a LoRA adapter with mlx-tune (or print the command).

This script is intentionally explicit for first-time local runs on 16GB Macs.
It defaults to dry-run so you can verify settings before execution.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run mlx-tune LoRA training")
    parser.add_argument("--model-path", default="models/mlx-community--gemma-3-4b-it-4bit")
    parser.add_argument("--train-file", default="data/processed/fine_tune/train.chat.jsonl")
    parser.add_argument("--valid-file", default="data/processed/fine_tune/valid.chat.jsonl")
    parser.add_argument("--output-dir", default="fine-tune/outputs/gemma-lora")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--micro-batch-size", type=int, default=1)
    parser.add_argument("--iters", type=int, default=250)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--max-seq-length", type=int, default=1024)
    parser.add_argument("--lora-rank", type=int, default=8)
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--run", action="store_true", help="Actually execute the command")
    parser.add_argument("--log-file", default="fine-tune/outputs/train.log")
    return parser.parse_args()


def build_command(args: argparse.Namespace) -> list[str]:
    # Uses mlx-tune CLI layout. If your local installation uses a slightly different
    # command surface, run with --run once and adjust based on CLI help.
    return [
        "mlx-tune",
        "train",
        "--model",
        args.model_path,
        "--train-file",
        args.train_file,
        "--val-file",
        args.valid_file,
        "--output-dir",
        args.output_dir,
        "--batch-size",
        str(args.batch_size),
        "--micro-batch-size",
        str(args.micro_batch_size),
        "--iters",
        str(args.iters),
        "--learning-rate",
        str(args.learning_rate),
        "--max-seq-length",
        str(args.max_seq_length),
        "--lora-rank",
        str(args.lora_rank),
        "--lora-alpha",
        str(args.lora_alpha),
    ]


def main() -> None:
    args = parse_args()
    cmd = build_command(args)

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)

    print("command:")
    print(" ".join(shlex.quote(part) for part in cmd))

    config_path = Path(args.output_dir) / "run_config.json"
    config_path.write_text(json.dumps(vars(args), indent=2), encoding="utf-8")

    if not args.run:
        print("dry-run only. add --run to execute training.")
        return

    with Path(args.log_file).open("w", encoding="utf-8") as log_f:
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
        rc = proc.wait()

    if rc != 0:
        raise SystemExit(f"training failed with exit code {rc}. see {args.log_file}")

    print(f"training completed. logs: {args.log_file}")


if __name__ == "__main__":
    main()
