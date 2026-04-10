#!/usr/bin/env python3
"""Merge PEFT LoRA adapter into base model weights (HF format)."""

from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Merge HF LoRA adapter")
    p.add_argument("--base-model", required=True)
    p.add_argument("--lora-adapter", required=True)
    p.add_argument("--output-dir", required=True)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model = AutoModelForCausalLM.from_pretrained(args.base_model)
    model = PeftModel.from_pretrained(model, args.lora_adapter)
    merged = model.merge_and_unload()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    merged.save_pretrained(out)
    tok = AutoTokenizer.from_pretrained(args.base_model, use_fast=True)
    tok.save_pretrained(out)

    print(f"merged model saved -> {out}")


if __name__ == "__main__":
    main()
