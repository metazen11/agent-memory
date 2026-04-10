#!/usr/bin/env python3
"""Generate a ready-to-run MLX training script from a natural-language request.

Example:
  ./.venv/bin/python fine-tune/generate_training_script.py \
    --request "write a Python script using mlx-tune to fine-tune Gemma 4 E4B on a 16GB Mac" \
    --output fine-tune/generated/train_gemma_16gb.py
"""

from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate MLX fine-tune script template")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", default="fine-tune/generated/train_template.py")
    return parser.parse_args()


def build_script(request: str) -> str:
    # We keep this deterministic for reproducibility while preserving the user request
    # in the generated artifact header.
    return f'''#!/usr/bin/env python3
"""Auto-generated from request:
{request}
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MODEL_PATH = "models/mlx-community--gemma-3-4b-it-4bit"
TRAIN_FILE = "data/processed/fine_tune/train.chat.jsonl"
VALID_FILE = "data/processed/fine_tune/valid.chat.jsonl"
OUTPUT_DIR = "fine-tune/outputs/gemma-lora-generated"
LOG_FILE = "fine-tune/outputs/gemma-lora-generated/train.log"

cmd = [
    "mlx-tune",
    "train",
    "--model", MODEL_PATH,
    "--train-file", TRAIN_FILE,
    "--val-file", VALID_FILE,
    "--output-dir", OUTPUT_DIR,
    "--batch-size", "1",
    "--micro-batch-size", "1",
    "--iters", "250",
    "--learning-rate", "2e-4",
    "--max-seq-length", "1024",
    "--lora-rank", "8",
    "--lora-alpha", "16",
]

Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

with open(LOG_FILE, "w", encoding="utf-8") as log_file:
    rc = subprocess.call(cmd, stdout=log_file, stderr=subprocess.STDOUT)

if rc != 0:
    raise SystemExit(f"training failed with exit code {{rc}}; see {{LOG_FILE}}")

print(f"training finished; logs at {{LOG_FILE}}")
'''


def main() -> None:
    args = parse_args()
    script_text = build_script(args.request)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(script_text, encoding="utf-8")
    print(f"generated script -> {out_path}")


if __name__ == "__main__":
    main()
