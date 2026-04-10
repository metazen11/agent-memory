#!/usr/bin/env python3
"""Download HF/MLX models into ./models for local fine-tuning/inference."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from huggingface_hub import snapshot_download


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download model snapshots into ./models")
    parser.add_argument("--model", action="append", required=True, help="HF repo id, e.g. mlx-community/gemma-3-4b-it-4bit")
    parser.add_argument("--models-dir", default="models")
    parser.add_argument("--hf-token", default=None, help="Optional token for gated/private models")
    parser.add_argument("--hf-token-env", default="HF_TOKEN")
    parser.add_argument("--revision", default=None)
    parser.add_argument("--allow-pattern", action="append", default=None)
    parser.add_argument("--ignore-pattern", action="append", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = (
        args.hf_token
        or os.getenv("HUGGING_FACE_API")
        or os.getenv(args.hf_token_env)
    )
    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    for model_id in args.model:
        target = models_dir / model_id.replace("/", "--")
        target.mkdir(parents=True, exist_ok=True)
        print(f"downloading {model_id} -> {target}")
        path = snapshot_download(
            repo_id=model_id,
            local_dir=str(target),
            revision=args.revision,
            token=token,
            allow_patterns=args.allow_pattern,
            ignore_patterns=args.ignore_pattern,
        )
        print(f"done: {path}")


if __name__ == "__main__":
    main()
