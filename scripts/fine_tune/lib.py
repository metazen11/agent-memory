"""Shared helpers for the fine-tune pipeline.

Reusable across base downloads, training, GGUF conversion, validation, and
LM Studio integration tests. Keep this module pure stdlib + huggingface_hub
so it imports cheaply from preflight scripts.

Canonical layout assumed (paths are relative to the repo root):

    models/base/<slug>/                 HF base model snapshot
    models/lora/<slug>-toolcalls-lora/  LoRA adapter output
    models/merged/<slug>-toolcalls-merged/   merged HF model
    models/gguf/<slug>-toolcalls-{f16,q4km}.gguf
    data/processed/qwen25_tools/v1/     restructured dataset
    docs/training_runs/                 per-run reports
    logs/m-ft-1/                        phase logs

`MODELS` registers every model the pipeline knows about. Add a new entry to
fine-tune another model — every script downstream picks it up automatically.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ModelSpec:
    slug: str               # short name used in all output paths
    hf_repo: str            # huggingface repo id
    family: str             # "qwen25", "qwen3", "gemma3", ...
    chat_template_style: str  # "qwen25-tools", "hermes", "llama3", ...

    @property
    def base_dir(self) -> Path:
        return REPO_ROOT / "models" / "base" / self.slug

    @property
    def lora_dir(self) -> Path:
        return REPO_ROOT / "models" / "lora" / f"{self.slug}-toolcalls-lora"

    @property
    def merged_dir(self) -> Path:
        return REPO_ROOT / "models" / "merged" / f"{self.slug}-toolcalls-merged"

    @property
    def gguf_f16(self) -> Path:
        return REPO_ROOT / "models" / "gguf" / f"{self.slug}-toolcalls-f16.gguf"

    @property
    def gguf_q4km(self) -> Path:
        return REPO_ROOT / "models" / "gguf" / f"{self.slug}-toolcalls-q4km.gguf"

    @property
    def revision_file(self) -> Path:
        return self.base_dir / "REVISION.txt"


MODELS: dict[str, ModelSpec] = {
    "qwen2.5-3b-instruct": ModelSpec(
        slug="qwen2.5-3b-instruct",
        hf_repo="Qwen/Qwen2.5-3B-Instruct",
        family="qwen25",
        chat_template_style="qwen25-tools",
    ),
    "qwen2.5-7b-instruct": ModelSpec(
        slug="qwen2.5-7b-instruct",
        hf_repo="Qwen/Qwen2.5-7B-Instruct",
        family="qwen25",
        chat_template_style="qwen25-tools",
    ),
    "qwen3-4b": ModelSpec(
        slug="qwen3-4b",
        hf_repo="Qwen/Qwen3-4B",
        family="qwen3",
        chat_template_style="qwen3-tools",
    ),
    "qwen3-8b": ModelSpec(
        slug="qwen3-8b",
        hf_repo="Qwen/Qwen3-8B",
        family="qwen3",
        chat_template_style="qwen3-tools",
    ),
}


def get_model(slug: str) -> ModelSpec:
    if slug not in MODELS:
        raise KeyError(
            f"Unknown model slug '{slug}'. Add it to MODELS in scripts/fine_tune/lib.py. "
            f"Known: {sorted(MODELS)}"
        )
    return MODELS[slug]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path, buf_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(buf_size):
            h.update(chunk)
    return h.hexdigest()


def write_sha256_sidecar(path: Path) -> Path:
    digest = sha256_file(path)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n")
    return sidecar


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def log_path(phase: str, name: str) -> Path:
    log_dir = REPO_ROOT / "logs" / phase
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{name}_{utc_stamp()}.log"


def dropbox_running() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", "Dropbox.app/Contents/MacOS/Dropbox"], text=True)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def write_json(path: Path, data, sort_keys: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=sort_keys)
        f.write("\n")
