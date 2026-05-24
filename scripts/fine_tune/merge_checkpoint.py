#!/usr/bin/env python3
"""End-to-end checkpoint -> Q6_K GGUF wrapper.

Takes an intermediate (or final) LoRA checkpoint dir from a training run
and produces a llama-server-ready Q6_K GGUF by chaining:
  1. fine-tune/gguf/merge_lora_hf.py   (LoRA -> merged HF model)
  2. fine-tune/gguf/convert_to_gguf.py (HF -> f16 GGUF -> Q6_K GGUF)

Then cleans up the merged HF dir + f16 GGUF (each ~7-8GB) so we keep
only the ~3GB Q6_K artifact. Idempotent: skips if the Q6_K output
already exists.

Wrap the *invocation* in `caffeinate -di` from the shell; this script
does not self-wrap so that the caller controls power-management policy.

Usage:
  caffeinate -di .venv-finetune/bin/python scripts/fine_tune/merge_checkpoint.py \
      --checkpoint models/lora/qwen3-4b-toolcalls-lora/runs/20260518T024108Z-v4-full/checkpoint-5000 \
      --base-model models/base/qwen3-4b \
      --out-gguf  models/gguf/qwen3-4b-toolcalls-v4-ckpt5000-q6k.gguf
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LLAMA_CPP = REPO_ROOT / "models" / "llama.cpp"
MERGE_SCRIPT = REPO_ROOT / "fine-tune" / "gguf" / "merge_lora_hf.py"
CONVERT_SCRIPT = REPO_ROOT / "fine-tune" / "gguf" / "convert_to_gguf.py"


def _run(cmd: list[str], label: str) -> None:
    print(f"\n[{label}] $ {' '.join(cmd)}", flush=True)
    t0 = time.time()
    res = subprocess.run(cmd)
    dur = time.time() - t0
    if res.returncode != 0:
        print(f"[{label}] FAILED after {dur:.1f}s (exit={res.returncode})",
              file=sys.stderr)
        sys.exit(res.returncode)
    print(f"[{label}] done in {dur:.1f}s", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--checkpoint", required=True,
                   help="LoRA adapter dir (must contain adapter_model.safetensors).")
    p.add_argument("--base-model", required=True,
                   help="Base HF model dir (e.g. models/base/qwen3-4b).")
    p.add_argument("--out-gguf", required=True,
                   help="Final Q6_K GGUF output path.")
    p.add_argument("--quant", default="Q6_K",
                   help="Quantization (default Q6_K).")
    p.add_argument("--work-dir", default=None,
                   help="Working dir for intermediates (default: alongside --out-gguf).")
    p.add_argument("--llama-cpp-dir", default=str(DEFAULT_LLAMA_CPP),
                   help=f"llama.cpp checkout (default {DEFAULT_LLAMA_CPP}).")
    p.add_argument("--keep-intermediates", action="store_true",
                   help="Don't delete the merged HF dir + f16 GGUF on success.")
    p.add_argument("--python", default=sys.executable,
                   help="Python interpreter for sub-scripts (default: current).")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    ckpt = Path(args.checkpoint).resolve()
    base = Path(args.base_model).resolve()
    out_gguf = Path(args.out_gguf).resolve()
    llama_cpp = Path(args.llama_cpp_dir).resolve()

    # --- Pre-flight ---
    if not (ckpt / "adapter_model.safetensors").exists():
        print(f"FAIL: adapter_model.safetensors not in {ckpt}", file=sys.stderr)
        return 2
    if not base.exists():
        print(f"FAIL: base model dir missing: {base}", file=sys.stderr)
        return 2
    if not MERGE_SCRIPT.exists() or not CONVERT_SCRIPT.exists():
        print(f"FAIL: helper scripts missing under {MERGE_SCRIPT.parent}", file=sys.stderr)
        return 2

    # Idempotent
    if out_gguf.exists():
        sz = out_gguf.stat().st_size
        print(f"SKIP: {out_gguf} already exists ({sz/1e9:.2f}GB).")
        return 0

    out_gguf.parent.mkdir(parents=True, exist_ok=True)

    # Intermediates: a per-checkpoint scratch dir
    work_root = Path(args.work_dir).resolve() if args.work_dir else out_gguf.parent
    stem = out_gguf.stem  # e.g. qwen3-4b-toolcalls-v4-ckpt5000-q6k
    merged_dir = work_root / f"_merged_{stem}"
    f16_gguf = work_root / f"_f16_{stem}.gguf"

    # Leftovers from a previous run? Wipe them so we start clean.
    if merged_dir.exists():
        print(f"  cleaning stale merged dir: {merged_dir}")
        shutil.rmtree(merged_dir)
    if f16_gguf.exists():
        print(f"  removing stale f16 gguf: {f16_gguf}")
        f16_gguf.unlink()

    print(f"checkpoint : {ckpt}")
    print(f"base       : {base}")
    print(f"merged dir : {merged_dir}")
    print(f"f16 gguf   : {f16_gguf}")
    print(f"out (Q6_K) : {out_gguf}")

    # --- Step 1: merge LoRA into base ---
    _run(
        [
            args.python, str(MERGE_SCRIPT),
            "--base-model", str(base),
            "--lora-adapter", str(ckpt),
            "--output-dir", str(merged_dir),
        ],
        "merge",
    )

    # --- Step 2: convert HF -> f16 GGUF -> Q6_K GGUF ---
    _run(
        [
            args.python, str(CONVERT_SCRIPT),
            "--llama-cpp-dir", str(llama_cpp),
            "--hf-model-dir", str(merged_dir),
            "--out-f16", str(f16_gguf),
            "--quant", args.quant,
            "--out-quant", str(out_gguf),
            "--run",
        ],
        "convert+quantize",
    )

    if not out_gguf.exists():
        print(f"FAIL: convert step did not produce {out_gguf}", file=sys.stderr)
        return 3

    # --- Step 3: cleanup ---
    if not args.keep_intermediates:
        if merged_dir.exists():
            print(f"  cleaning merged dir: {merged_dir}")
            shutil.rmtree(merged_dir)
        if f16_gguf.exists():
            print(f"  removing f16 gguf: {f16_gguf}")
            f16_gguf.unlink()

    sz = out_gguf.stat().st_size
    print(f"\nOK -> {out_gguf} ({sz/1e9:.2f}GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
