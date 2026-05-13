#!/usr/bin/env python3
"""Verify a downloaded base model loads and generates.

Reusable across every model in MODELS. Run after download_base.py.

Usage:
    python scripts/fine_tune/smoke_test_base.py qwen2.5-3b-instruct
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import get_model  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slug")
    p.add_argument("--max-new-tokens", type=int, default=8)
    p.add_argument("--prompt", default="Reply with the single word READY and nothing else.")
    args = p.parse_args()

    spec = get_model(args.slug)
    if not spec.revision_file.exists():
        print(f"FAIL: {spec.base_dir} not downloaded. Run download_base.py first.", file=sys.stderr)
        return 1

    print(f"loading {spec.slug} from {spec.base_dir}...")
    import torch  # noqa: PLC0415
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: PLC0415

    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(str(spec.base_dir), local_files_only=True, trust_remote_code=False)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "mps" else torch.float32
    model = AutoModelForCausalLM.from_pretrained(
        str(spec.base_dir),
        local_files_only=True,
        trust_remote_code=False,
        torch_dtype=dtype,
    ).to(device)
    model.eval()
    load_s = time.time() - t0

    messages = [{"role": "user", "content": args.prompt}]
    text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tok(text, return_tensors="pt").to(device)

    t0 = time.time()
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=args.max_new_tokens,
            do_sample=False,
            pad_token_id=tok.eos_token_id,
        )
    gen_s = time.time() - t0
    generated = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)

    print(f"device:    {device}")
    print(f"dtype:     {dtype}")
    print(f"load_s:    {load_s:.2f}")
    print(f"gen_s:     {gen_s:.2f}")
    print(f"generated: {generated!r}")

    if not generated.strip():
        print("FAIL: generation was empty", file=sys.stderr)
        return 2
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
