#!/usr/bin/env python3
"""Download an HF base model into the canonical location.

Reusable across every model in scripts/fine_tune/lib.py:MODELS.

Usage:
    python scripts/fine_tune/download_base.py qwen2.5-3b-instruct
    python scripts/fine_tune/download_base.py qwen2.5-7b-instruct
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from huggingface_hub import HfApi, snapshot_download  # noqa: E402

from lib import get_model  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slug", help="Model slug from MODELS in lib.py")
    p.add_argument("--force", action="store_true", help="Re-download even if REVISION.txt exists")
    args = p.parse_args()

    spec = get_model(args.slug)
    if spec.revision_file.exists() and not args.force:
        rev = spec.revision_file.read_text().strip()
        print(f"already downloaded: {spec.base_dir} (revision {rev[:12]})")
        print("pass --force to re-download")
        return 0

    spec.base_dir.mkdir(parents=True, exist_ok=True)
    print(f"downloading {spec.hf_repo} -> {spec.base_dir}")
    snapshot_download(
        repo_id=spec.hf_repo,
        local_dir=str(spec.base_dir),
    )
    info = HfApi().model_info(spec.hf_repo)
    spec.revision_file.write_text(info.sha + "\n")
    print(f"downloaded ok. revision: {info.sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
