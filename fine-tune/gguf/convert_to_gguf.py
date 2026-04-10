#!/usr/bin/env python3
"""Convert merged HF model to GGUF and optionally quantize for llama.cpp / LM Studio.

Requirements:
- local llama.cpp checkout
- convert_hf_to_gguf.py and llama-quantize built
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convert HF model to GGUF")
    p.add_argument("--llama-cpp-dir", default="models/llama.cpp")
    p.add_argument("--hf-model-dir", required=True)
    p.add_argument("--out-f16", required=True, help="Output GGUF f16 file")
    p.add_argument("--quant", default="Q4_K_M")
    p.add_argument("--out-quant", default=None, help="Output quantized GGUF file")
    p.add_argument("--run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    llama_dir = Path(args.llama_cpp_dir)
    convert_py = llama_dir / "convert_hf_to_gguf.py"
    quant_bin = llama_dir / "build/bin/llama-quantize"

    out_f16 = Path(args.out_f16)
    out_f16.parent.mkdir(parents=True, exist_ok=True)

    cmd_convert = [
        sys.executable,
        str(convert_py),
        str(args.hf_model_dir),
        "--outfile",
        str(out_f16),
        "--outtype",
        "f16",
    ]

    print("convert command:")
    print(" ".join(shlex.quote(x) for x in cmd_convert))

    cmd_quant = None
    if args.out_quant:
        out_quant = Path(args.out_quant)
        out_quant.parent.mkdir(parents=True, exist_ok=True)
        cmd_quant = [str(quant_bin), str(out_f16), str(out_quant), args.quant]
        print("quantize command:")
        print(" ".join(shlex.quote(x) for x in cmd_quant))

    if not args.run:
        print("dry-run only. add --run to execute.")
        return

    subprocess.run(cmd_convert, check=True)
    if cmd_quant:
        subprocess.run(cmd_quant, check=True)
    print("done")


if __name__ == "__main__":
    main()
