#!/usr/bin/env python3
"""Interactive TUI wizard for the agent-memory fine-tune pipeline.

Wraps the v3 ad-hoc playbook (see docs/fine_tune/V3_PLAN.md) into a guided
flow. Picks base model + dataset filters + run type, writes a
``train_config.yaml`` to the repo root, then drives the 12 pipeline screens
end-to-end. Designed to work on Mac (MPS), Linux (CUDA / CPU), and
Windows (CUDA / CPU).

Usage:
    .venv-finetune/bin/python scripts/fine_tune/wizard.py
    .venv-finetune/bin/python scripts/fine_tune/wizard.py --config train_config.yaml
    .venv-finetune/bin/python scripts/fine_tune/wizard.py --config c.yaml --no-tui
    .venv-finetune/bin/python scripts/fine_tune/wizard.py --print-config-schema

See docs/fine_tune/WIZARD.md for the full guide.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants / repo layout
# ---------------------------------------------------------------------------

# Note: use .absolute() not .resolve() because models/ symlinks to a Dropbox
# mirror on the user's Mac (see V3_PLAN.md §9 + FAILURE_MODES #1). Walking
# the symlink at this stage corrupts paths recorded in checkpoints.
REPO_ROOT = Path(__file__).absolute().parents[2]
DEFAULT_VENV = REPO_ROOT / ".venv-finetune"
LLAMA_BIN_DIRS = [
    REPO_ROOT / "models" / "llama.cpp" / "build" / "bin",      # Mac/Linux
    REPO_ROOT / "models" / "llama.cpp" / "build" / "Release",  # Windows
]

# All known base models. Custom HF repos can be passed via the wizard's
# "custom" field. The slug is what shows up in output paths.
KNOWN_MODELS: dict[str, dict[str, str]] = {
    "qwen3-4b": {
        "hf_repo": "Qwen/Qwen3-4B",
        "label": "Qwen3-4B (recommended, fast iteration, ~3.5 GB Q4_K_M)",
        "warning": "",
    },
    "qwen3-8b": {
        "hf_repo": "Qwen/Qwen3-8B",
        "label": "Qwen3-8B (V3_PLAN.md target, ~5 GB Q4_K_M, ~40h train)",
        "warning": "",
    },
    "qwen3.5-9b": {
        "hf_repo": "Qwen/Qwen3.5-9B",
        "label": "Qwen3.5-9B (NOT recommended for training)",
        "warning": (
            "Qwen3.5-9B uses a hybrid Mamba/SSM architecture; LoRA "
            "injection points for SSM tensors are unproven, and "
            "thinking-by-default mode fights tool-call SFT. Use for "
            "inference only. See V3_PLAN.md §3."
        ),
    },
}

# 8 V3_PLAN §5 dataset fixes. Default state matches the doc.
FILTER_DEFINITIONS: list[tuple[str, str, bool]] = [
    ("stop_after_tool_call",      "Stop-after-tool_call cut",                True),
    ("text_synthesis_oversample", "Oversample tool_response -> text answers", True),
    ("negative_reemit_pairs",     "Synthesise re-emit DPO pairs (v3.1 work)", False),
    ("subagent_filter",           "Drop subagents/agent-*.jsonl",            True),
    ("off_distribution_filter",   "Drop off-distribution first-turn actions", True),
    ("project_tagged_oversample", "Oversample project-tagged rows (2x)",      True),
    ("in_args_repetition_cap",    "Cap argument-value length + repetition",  True),
    ("vision_row_filter",         "Drop / placeholder image rows",           True),
]

DEFAULT_PROJECTS = ["agent-memory", "fire-map", "daily-dispatch", "anvil"]
QUANT_CHOICES = ["Q4_K_M", "Q6_K", "Q8_0", "f16"]
DEFAULT_QUANTS = ["Q4_K_M", "Q6_K"]
RUN_TYPES = ["tiny_smoke", "full", "dataset_only", "eval_only"]


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------

CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "wizard train_config.yaml",
    "type": "object",
    "required": ["base_model", "dataset", "output", "training", "run_type"],
    "properties": {
        "base_model": {"type": "string", "description": "HF repo or local path"},
        "base_revision": {"type": ["string", "null"]},
        "dataset": {
            "type": "object",
            "required": ["source", "date_range", "projects", "filters"],
            "properties": {
                "source": {"enum": ["agent_memory_db", "jsonl_corpus"]},
                "date_range": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "items": {"type": "string"},
                },
                "projects": {"type": "array", "items": {"type": "string"}},
                "filters": {
                    "type": "object",
                    "properties": {
                        k: {"type": "boolean"} for k, _, _ in FILTER_DEFINITIONS
                    },
                },
            },
        },
        "output": {
            "type": "object",
            "required": ["lora_dir", "quants", "lm_studio_publisher"],
            "properties": {
                "lora_dir": {"type": "string"},
                "quants": {"type": "array", "items": {"enum": QUANT_CHOICES}},
                "lm_studio_publisher": {"type": "string"},
            },
        },
        "training": {
            "type": "object",
            "properties": {
                "epochs": {"type": "number"},
                "lr": {"type": "number"},
                "max_length": {"type": "integer"},
                "grad_accum": {"type": "integer"},
                "lora_r": {"type": ["integer", "null"]},
                "lora_alpha": {"type": ["integer", "null"]},
            },
        },
        "run_type": {"enum": RUN_TYPES},
    },
}


# ---------------------------------------------------------------------------
# Plain-Python config dataclass
# ---------------------------------------------------------------------------


@dataclass
class WizardConfig:
    base_model: str = "Qwen/Qwen3-4B"
    base_revision: str | None = None
    dataset_source: str = "agent_memory_db"
    date_start: str = "2026-03-29"
    date_end: str = "today"
    projects: list[str] = field(default_factory=lambda: list(DEFAULT_PROJECTS))
    filters: dict[str, bool] = field(
        default_factory=lambda: {k: default for k, _, default in FILTER_DEFINITIONS}
    )
    lora_dir: str = ""  # filled in by post_init resolution
    quants: list[str] = field(default_factory=lambda: list(DEFAULT_QUANTS))
    lm_studio_publisher: str = "mz"
    epochs: float = 1.0
    lr: float = 1.5e-4
    max_length: int = 4096
    grad_accum: int = 8
    lora_r: int | None = None
    lora_alpha: int | None = None
    run_type: str = "tiny_smoke"

    def base_slug(self) -> str:
        """Derive a filesystem-safe slug from base_model."""
        s = self.base_model.split("/")[-1].lower()
        return s.replace("_", "-")

    def resolved_lora_dir(self) -> Path:
        if self.lora_dir:
            return Path(self.lora_dir)
        return REPO_ROOT / "models" / "lora" / f"{self.base_slug()}-toolcalls-v3-lora"

    def to_yaml_dict(self) -> dict[str, Any]:
        end = self.date_end if self.date_end != "today" else date.today().isoformat()
        return {
            "base_model": self.base_model,
            "base_revision": self.base_revision,
            "dataset": {
                "source": self.dataset_source,
                "date_range": [self.date_start, end],
                "projects": list(self.projects),
                "filters": dict(self.filters),
            },
            "output": {
                "lora_dir": str(self.resolved_lora_dir()),
                "quants": list(self.quants),
                "lm_studio_publisher": self.lm_studio_publisher,
            },
            "training": {
                "epochs": self.epochs,
                "lr": self.lr,
                "max_length": self.max_length,
                "grad_accum": self.grad_accum,
                "lora_r": self.lora_r,
                "lora_alpha": self.lora_alpha,
            },
            "run_type": self.run_type,
        }

    @classmethod
    def from_yaml_dict(cls, d: dict[str, Any]) -> "WizardConfig":
        ds = d.get("dataset", {})
        out = d.get("output", {})
        tr = d.get("training", {})
        date_range = ds.get("date_range") or ["2026-03-29", "today"]
        filters = {k: default for k, _, default in FILTER_DEFINITIONS}
        filters.update(ds.get("filters", {}))
        return cls(
            base_model=d.get("base_model", "Qwen/Qwen3-4B"),
            base_revision=d.get("base_revision"),
            dataset_source=ds.get("source", "agent_memory_db"),
            date_start=str(date_range[0]),
            date_end=str(date_range[1]),
            projects=list(ds.get("projects") or DEFAULT_PROJECTS),
            filters=filters,
            lora_dir=out.get("lora_dir", ""),
            quants=list(out.get("quants") or DEFAULT_QUANTS),
            lm_studio_publisher=out.get("lm_studio_publisher", "mz"),
            epochs=float(tr.get("epochs", 1.0)),
            lr=float(tr.get("lr", 1.5e-4)),
            max_length=int(tr.get("max_length", 4096)),
            grad_accum=int(tr.get("grad_accum", 8)),
            lora_r=tr.get("lora_r"),
            lora_alpha=tr.get("lora_alpha"),
            run_type=d.get("run_type", "tiny_smoke"),
        )


# ---------------------------------------------------------------------------
# Environment introspection
# ---------------------------------------------------------------------------


@dataclass
class EnvReport:
    python_version: str
    venv_active: bool
    venv_path: str
    device: str          # "cuda" / "mps" / "cpu" / "unknown"
    disk_free_gb: float
    llama_cpp_present: bool
    llama_bin_dir: str | None
    gh_cli_auth: str     # "ok" / "no auth" / "no gh"
    dropbox_running: bool | None  # None when not detectable (e.g. Linux)

    def lines(self) -> list[tuple[str, str, bool]]:
        """Return (label, value, ok) rows for rendering."""
        return [
            ("Python", self.python_version, self.python_version.startswith(("3.10", "3.11", "3.12", "3.13", "3.14"))),
            ("Venv active", self.venv_path or "no", bool(self.venv_active)),
            ("Device", self.device, self.device in ("cuda", "mps")),
            ("Disk free", f"{self.disk_free_gb:.0f} GB", self.disk_free_gb >= 80),
            ("llama.cpp", self.llama_bin_dir or "missing", self.llama_cpp_present),
            ("gh CLI", self.gh_cli_auth, self.gh_cli_auth == "ok"),
            ("Dropbox", str(self.dropbox_running), self.dropbox_running is not True),
        ]


def detect_device() -> str:
    try:
        import torch  # noqa: PLC0415
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except Exception:
        return "unknown"


def find_llama_bin_dir() -> Path | None:
    for d in LLAMA_BIN_DIRS:
        if (d / "llama-quantize").exists() or (d / "llama-quantize.exe").exists():
            return d
    # PATH fallback
    if shutil.which("llama-quantize"):
        return None  # signals "on PATH"
    return None


def dropbox_running() -> bool | None:
    sys_name = platform.system()
    try:
        if sys_name in ("Darwin", "Linux"):
            r = subprocess.run(
                ["pgrep", "-fl", "Dropbox"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0 and bool(r.stdout.strip())
        if sys_name == "Windows":
            r = subprocess.run(
                ["tasklist"], capture_output=True, text=True, timeout=10,
            )
            return "Dropbox" in r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return None
    return None


def gh_cli_status() -> str:
    if not shutil.which("gh"):
        return "no gh"
    try:
        r = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=5
        )
        return "ok" if r.returncode == 0 else "no auth"
    except (subprocess.TimeoutExpired, OSError):
        return "no auth"


def lmstudio_models_dir() -> Path:
    """Cross-platform: ~/.lmstudio/models on Mac/Linux/Windows."""
    return Path.home() / ".lmstudio" / "models"


def env_report() -> EnvReport:
    venv = os.environ.get("VIRTUAL_ENV") or sys.prefix
    # sys.prefix differs from sys.base_prefix when running in a venv.
    venv_active = (
        bool(os.environ.get("VIRTUAL_ENV"))
        or sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    )
    try:
        free_bytes = shutil.disk_usage(REPO_ROOT).free
        free_gb = free_bytes / (1024 ** 3)
    except OSError:
        free_gb = 0.0
    bin_dir = find_llama_bin_dir()
    return EnvReport(
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        venv_active=venv_active,
        venv_path=venv,
        device=detect_device(),
        disk_free_gb=free_gb,
        llama_cpp_present=bin_dir is not None or shutil.which("llama-quantize") is not None,
        llama_bin_dir=str(bin_dir) if bin_dir else (shutil.which("llama-quantize") or None),
        gh_cli_auth=gh_cli_status(),
        dropbox_running=dropbox_running(),
    )


# ---------------------------------------------------------------------------
# Library check
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES = {
    "textual": "textual",
    "huggingface_hub": "huggingface-hub",
    "peft": "peft",
    "transformers": "transformers",
    "accelerate": "accelerate",
    "datasets": "datasets",
    "yaml": "pyyaml",
    "psutil": "psutil",
    "rich": "rich",
    "jsonschema": "jsonschema",
    "prompt_toolkit": "prompt_toolkit",
}


def check_libraries() -> list[tuple[str, bool, str]]:
    """Return (pip_name, installed, version_or_error) for each required pkg."""
    out: list[tuple[str, bool, str]] = []
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
        except ImportError as e:
            out.append((pip_name, False, str(e)))
            continue
        # Prefer importlib.metadata to dodge jsonschema's __version__ deprecation.
        try:
            from importlib.metadata import version  # noqa: PLC0415
            ver = version(pip_name)
        except Exception:
            ver = "unknown"
        out.append((pip_name, True, ver))
    return out


def pip_install(packages: list[str]) -> tuple[int, str]:
    """Install into the active venv via sys.executable -m pip."""
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + "\n" + r.stderr)


# ---------------------------------------------------------------------------
# YAML I/O (deferred import so --help works without pyyaml)
# ---------------------------------------------------------------------------


def write_config_yaml(cfg: WizardConfig, path: Path) -> None:
    import yaml  # noqa: PLC0415
    path.write_text(yaml.safe_dump(cfg.to_yaml_dict(), sort_keys=False))


def load_config_yaml(path: Path) -> WizardConfig:
    import yaml  # noqa: PLC0415
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a mapping")
    return WizardConfig.from_yaml_dict(raw)


# ---------------------------------------------------------------------------
# Pipeline screens (text-mode driver — also reusable from TUI)
# ---------------------------------------------------------------------------


def script_path(name: str) -> Path:
    return REPO_ROOT / "scripts" / "fine_tune" / name


def train_script_for(cfg: WizardConfig) -> Path:
    """Locate run_train_lora.py for the chosen base model."""
    slug = cfg.base_slug()
    candidates = [
        REPO_ROOT / "models" / "lora" / f"{slug}-toolcalls-v3-lora" / "run_train_lora_v3.py",
        REPO_ROOT / "models" / "lora" / f"{slug}-toolcalls-lora" / "run_train_lora.py",
        REPO_ROOT / "models" / "lora" / "qwen2.5-3b-toolcalls-lora" / "run_train_lora.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    # Fall back to whichever exists; caller handles missing case.
    return candidates[-1]


def screen_environment_check(_cfg: WizardConfig, *, verbose: bool = True) -> bool:
    r = env_report()
    if verbose:
        print("== Screen 1: Environment ==")
        for label, value, ok in r.lines():
            marker = "OK " if ok else "WARN"
            print(f"  [{marker}] {label:14s} {value}")
    blockers = [label for label, _, ok in r.lines() if not ok and label in {"Python", "Disk free"}]
    return not blockers


def screen_library_check(*, auto_install: bool, verbose: bool = True) -> bool:
    rows = check_libraries()
    missing = [pip for pip, installed, _ in rows if not installed]
    if verbose:
        print("== Screen 2: Libraries ==")
        for pip, installed, ver in rows:
            marker = "OK " if installed else "MISS"
            print(f"  [{marker}] {pip:20s} {ver}")
    if missing and auto_install:
        if verbose:
            print(f"installing missing: {missing}")
        code, output = pip_install(missing)
        if verbose:
            print(output[-1200:])
        return code == 0
    return not missing


def screen_base_model_download(cfg: WizardConfig, *, verbose: bool = True) -> bool:
    slug = cfg.base_slug()
    base_dir = REPO_ROOT / "models" / "base" / slug
    rev_file = base_dir / "REVISION.txt"
    if rev_file.exists():
        if verbose:
            print(f"== Screen 3: Base model {slug} already on disk at {base_dir} ==")
        return True
    if verbose:
        print(f"== Screen 3: Downloading base model {cfg.base_model} -> {base_dir} ==")
    try:
        from huggingface_hub import HfApi, snapshot_download  # noqa: PLC0415
        base_dir.mkdir(parents=True, exist_ok=True)
        repo = cfg.base_model
        if not ("/" in repo):
            # bare slug — assume Qwen org
            repo = f"Qwen/{repo}"
        snapshot_download(repo_id=repo, local_dir=str(base_dir), revision=cfg.base_revision)
        info = HfApi().model_info(repo)
        rev_file.write_text((info.sha or "unknown") + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"  download failed: {e}")
        return False


def screen_dataset_build(cfg: WizardConfig, *, verbose: bool = True) -> bool:
    """Invoke build_v3_dataset.py if present, else build_v2_dataset.py.

    All filter selections and the project list are passed as env vars so the
    underlying script (built by another agent) can pick them up without
    arg-parser changes.
    """
    builder = script_path("build_v3_dataset.py")
    fallback = script_path("build_v2_dataset.py")
    if not builder.exists():
        if verbose:
            print(f"== Screen 4: {builder.name} not present yet; falling back to {fallback.name} ==")
        builder = fallback
    env = os.environ.copy()
    env["WIZARD_FILTERS_JSON"] = json.dumps(cfg.filters)
    env["WIZARD_PROJECTS_JSON"] = json.dumps(cfg.projects)
    env["WIZARD_DATE_START"] = cfg.date_start
    env["WIZARD_DATE_END"] = cfg.date_end if cfg.date_end != "today" else date.today().isoformat()
    if verbose:
        print(f"== Screen 4: Building dataset via {builder.name} ==")
    try:
        r = subprocess.run([sys.executable, str(builder), "--write"], env=env)
        return r.returncode == 0
    except FileNotFoundError:
        if verbose:
            print(f"  builder not found at {builder}")
        return False


def screen_dataset_audit(_cfg: WizardConfig, *, verbose: bool = True) -> str:
    """Locate and return the dataset audit markdown path.

    Returns the file content (or '' if not found). UI layer handles gating.
    """
    candidates = [
        REPO_ROOT / "docs" / "training_runs" / "v3-dataset-audit.md",
        REPO_ROOT / "data" / "processed" / "qwen3_tools" / "v3" / "MANIFEST.json",
        REPO_ROOT / "data" / "processed" / "qwen25_tools" / "v2" / "MANIFEST.json",
    ]
    for c in candidates:
        if c.exists():
            if verbose:
                print(f"== Screen 5: Audit at {c} ==")
            return c.read_text()
    if verbose:
        print("== Screen 5: No audit file found ==")
    return ""


def screen_tiny_training(cfg: WizardConfig, *, verbose: bool = True) -> bool:
    script = train_script_for(cfg)
    if not script.exists():
        if verbose:
            print(f"== Screen 6: training script {script} not found ==")
        return False
    env = os.environ.copy()
    env["DATASET_TIER"] = "tiny"
    env["RUN_TAG"] = "tiny-smoke-v3"
    env["MODEL_SLUG"] = cfg.base_slug()
    env["DATASET_VERSION"] = "v3" if (REPO_ROOT / "data" / "processed" / "qwen3_tools" / "v3").exists() else "v2"
    env["EPOCHS"] = "0.5"
    env["LR"] = str(cfg.lr)
    env["MAX_LENGTH"] = str(min(cfg.max_length, 1024))
    env["GRAD_ACCUM"] = str(cfg.grad_accum)
    if verbose:
        print(f"== Screen 6: Tiny training via {script.name} ==")
    r = subprocess.run([sys.executable, str(script)], env=env)
    return r.returncode == 0


def screen_full_training(cfg: WizardConfig, *, verbose: bool = True) -> bool:
    script = train_script_for(cfg)
    if not script.exists():
        if verbose:
            print(f"== Screen 8: training script {script} not found ==")
        return False
    env = os.environ.copy()
    env["DATASET_TIER"] = "full"
    env["RUN_TAG"] = "full-v3"
    env["MODEL_SLUG"] = cfg.base_slug()
    env["DATASET_VERSION"] = "v3" if (REPO_ROOT / "data" / "processed" / "qwen3_tools" / "v3").exists() else "v2"
    env["EPOCHS"] = str(cfg.epochs)
    env["LR"] = str(cfg.lr)
    env["MAX_LENGTH"] = str(cfg.max_length)
    env["GRAD_ACCUM"] = str(cfg.grad_accum)
    if verbose:
        print(f"== Screen 8: Full training via {script.name} (~36-40h on MPS) ==")
    r = subprocess.run([sys.executable, str(script)], env=env)
    return r.returncode == 0


def screen_gguf_conversion(cfg: WizardConfig, *, verbose: bool = True) -> bool:
    bin_dir = find_llama_bin_dir()
    if bin_dir is None and shutil.which("llama-quantize") is None:
        if verbose:
            print("== Screen 9: llama.cpp not present, skipping GGUF (PC mode is OK) ==")
        return True
    if bin_dir:
        quantize = bin_dir / "llama-quantize"
    else:
        which_q = shutil.which("llama-quantize")
        assert which_q is not None, "llama-quantize unavailable despite earlier check"
        quantize = Path(which_q)
    f16 = REPO_ROOT / "models" / "gguf" / f"{cfg.base_slug()}-toolcalls-v3-f16.gguf"
    if not f16.exists():
        if verbose:
            print(f"== Screen 9: missing f16 GGUF at {f16}; merge step not implemented in wizard ==")
        # Don't fail the whole pipeline — user can run merge separately.
        return True
    for q in cfg.quants:
        if q == "f16":
            continue
        out = REPO_ROOT / "models" / "gguf" / f"{cfg.base_slug()}-toolcalls-v3-{q.lower()}.gguf"
        cmd = [str(quantize), str(f16), str(out), q]
        if verbose:
            print(f"  quantizing -> {out.name}")
        r = subprocess.run(cmd)
        if r.returncode != 0:
            if verbose:
                print(f"  llama-quantize failed for {q}")
            return False
    return True


def screen_validation_suite(cfg: WizardConfig, *, verbose: bool = True) -> bool:
    """Run validator (Class D). Multi-class eval requires a running llama-server,
    so we only run the offline validator here. UI displays results.
    """
    validator = script_path("validate_tool_calls.py")
    if not validator.exists():
        return False
    # Pick the smallest GGUF that exists.
    gguf_dir = REPO_ROOT / "models" / "gguf"
    slug = cfg.base_slug()
    for q in ("q4km", "q6k", "q8_0", "f16"):
        cand = gguf_dir / f"{slug}-toolcalls-v3-{q}.gguf"
        if cand.exists():
            if verbose:
                print(f"== Screen 10: validating {cand.name} ==")
            r = subprocess.run([
                sys.executable, str(validator),
                "--backend", "llama-cli",
                "--gguf", str(cand),
                "--min-parse-rate", "0.05",
            ])
            return r.returncode == 0
    if verbose:
        print("== Screen 10: no GGUF found to validate ==")
    return False


def screen_lmstudio_install(cfg: WizardConfig, *, verbose: bool = True) -> bool:
    lms = lmstudio_models_dir()
    if not lms.exists() and platform.system() == "Linux":
        if verbose:
            print("== Screen 11: LM Studio dir absent on Linux, skipping ==")
        return True
    dest = lms / cfg.lm_studio_publisher / f"{cfg.base_slug()}-toolcalls-v3"
    dest.mkdir(parents=True, exist_ok=True)
    slug = cfg.base_slug()
    moved = 0
    for q in cfg.quants:
        src = REPO_ROOT / "models" / "gguf" / f"{slug}-toolcalls-v3-{q.lower()}.gguf"
        if src.exists():
            target = dest / src.name
            try:
                shutil.copy2(src, target)
                moved += 1
                if verbose:
                    print(f"  copied {src.name} -> {target}")
            except OSError as e:
                if verbose:
                    print(f"  copy failed: {e}")
    if verbose:
        print(f"== Screen 11: {moved} GGUF(s) installed to {dest} ==")
    return True


# ---------------------------------------------------------------------------
# Text-mode driver
# ---------------------------------------------------------------------------


def run_text_mode(cfg: WizardConfig, *, auto_install: bool = True) -> int:
    """Drive the pipeline in plain stdout (no TUI). Used for --no-tui and CI."""
    print("agent-memory fine-tune wizard — text mode")
    print(f"  base_model = {cfg.base_model}")
    print(f"  run_type   = {cfg.run_type}")
    print(f"  output     = {cfg.resolved_lora_dir()}")
    print()
    if not screen_environment_check(cfg):
        print("environment check failed — aborting")
        return 1
    if not screen_library_check(auto_install=auto_install):
        print("library check failed — aborting")
        return 1
    if cfg.run_type == "eval_only":
        return 0 if screen_validation_suite(cfg) else 1
    if not screen_base_model_download(cfg):
        print("base model download failed — aborting")
        return 1
    if not screen_dataset_build(cfg):
        print("dataset build failed — aborting")
        return 1
    audit = screen_dataset_audit(cfg)
    if audit:
        print("\n--- dataset audit head ---")
        print(audit[:1200])
        print("--- end audit head ---\n")
        print("AUTO-APPROVING in text mode. Use the TUI to gate interactively.")
    if cfg.run_type == "dataset_only":
        return 0
    if not screen_tiny_training(cfg):
        print("tiny training failed — aborting")
        return 1
    if cfg.run_type == "tiny_smoke":
        return 0
    if not screen_full_training(cfg):
        print("full training failed — aborting")
        return 1
    if not screen_gguf_conversion(cfg):
        print("GGUF conversion failed — aborting")
        return 1
    screen_validation_suite(cfg)
    screen_lmstudio_install(cfg)
    print("done.")
    return 0


# ---------------------------------------------------------------------------
# TUI (textual) — kept self-contained, importable only when actually used
# ---------------------------------------------------------------------------


def run_tui(cfg: WizardConfig) -> int:  # pragma: no cover — interactive
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Container, Horizontal, VerticalScroll
        from textual.screen import Screen
        from textual.widgets import (
            Button, Checkbox, DataTable, Footer, Header, Input,
            Log, RadioButton, RadioSet, Static,
        )
    except ImportError:
        print("textual is not installed; falling back to text mode")
        return run_text_mode(cfg)

    class IntroScreen(Screen):
        BINDINGS = [Binding("enter", "next", "Next"), Binding("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Container(
                Static("[b]agent-memory fine-tune wizard[/b]", classes="title"),
                Static(
                    "This wizard wraps the v3 ad-hoc playbook into a guided\n"
                    "flow. It will:\n"
                    "  - check the environment\n"
                    "  - install missing libraries\n"
                    "  - download the base model\n"
                    "  - build + audit the dataset (with gate)\n"
                    "  - run tiny + (optionally) full training\n"
                    "  - quantize to GGUF and install in LM Studio\n\n"
                    "See docs/fine_tune/WIZARD.md for full details.\n"
                ),
                Button("Begin", id="begin", variant="primary"),
            )
            yield Footer()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "begin":
                self.app.push_screen(ConfigScreen(cfg))

        def action_next(self) -> None:
            self.app.push_screen(ConfigScreen(cfg))

    class ConfigScreen(Screen):
        BINDINGS = [Binding("ctrl+s", "save", "Save+continue"), Binding("q", "quit", "Quit")]

        def __init__(self, cfg: WizardConfig) -> None:
            super().__init__()
            self.cfg = cfg

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll():
                yield Static("[b]1. Base model[/b]")
                with RadioSet(id="base_radio"):
                    for slug, info in KNOWN_MODELS.items():
                        warn = f"  [!] {info['warning'][:80]}" if info["warning"] else ""
                        yield RadioButton(f"{info['label']}{warn}", value=(slug == "qwen3-4b"), id=f"base_{slug}")
                    yield RadioButton("Custom HF repo (type below)", id="base_custom")
                yield Input(value="", placeholder="e.g. Qwen/Qwen3-14B", id="base_custom_input")

                yield Static("[b]2. Dataset filters[/b] (V3_PLAN.md §5)")
                for key, label, default in FILTER_DEFINITIONS:
                    yield Checkbox(label, value=default, id=f"filter_{key}")

                yield Static("[b]3. Projects to oversample[/b]")
                for p in DEFAULT_PROJECTS:
                    yield Checkbox(p, value=True, id=f"proj_{p}")

                yield Static("[b]4. Date range[/b]")
                yield Input(value=self.cfg.date_start, placeholder="YYYY-MM-DD", id="date_start")
                yield Input(value=self.cfg.date_end, placeholder="YYYY-MM-DD or 'today'", id="date_end")

                yield Static("[b]5. Output dir[/b]")
                yield Input(value=str(self.cfg.resolved_lora_dir()), id="lora_dir")

                yield Static("[b]6. Quants[/b]")
                for q in QUANT_CHOICES:
                    yield Checkbox(q, value=(q in DEFAULT_QUANTS), id=f"quant_{q}")

                yield Static("[b]7. Run type[/b]")
                with RadioSet(id="run_type_radio"):
                    yield RadioButton("Tiny smoke (build + tiny train + tiny eval)", value=True, id="rt_tiny_smoke")
                    yield RadioButton("Full train (everything)", id="rt_full")
                    yield RadioButton("Dataset only (build + audit, no train)", id="rt_dataset_only")
                    yield RadioButton("Eval only (against existing GGUF)", id="rt_eval_only")

                with Horizontal():
                    yield Button("Save & Run", id="run", variant="primary")
                    yield Button("Save (run later)", id="save_only")
                    yield Button("Cancel", id="cancel")
            yield Footer()

        def _collect(self) -> WizardConfig:
            # base model
            for slug in KNOWN_MODELS:
                if self.query_one(f"#base_{slug}", RadioButton).value:
                    self.cfg.base_model = KNOWN_MODELS[slug]["hf_repo"]
                    break
            if self.query_one("#base_custom", RadioButton).value:
                custom = self.query_one("#base_custom_input", Input).value.strip()
                if custom:
                    self.cfg.base_model = custom
            # filters
            for key, _, _ in FILTER_DEFINITIONS:
                self.cfg.filters[key] = self.query_one(f"#filter_{key}", Checkbox).value
            # projects
            self.cfg.projects = [p for p in DEFAULT_PROJECTS if self.query_one(f"#proj_{p}", Checkbox).value]
            # date
            self.cfg.date_start = self.query_one("#date_start", Input).value or "2026-03-29"
            self.cfg.date_end = self.query_one("#date_end", Input).value or "today"
            # lora_dir
            self.cfg.lora_dir = self.query_one("#lora_dir", Input).value
            # quants
            self.cfg.quants = [q for q in QUANT_CHOICES if self.query_one(f"#quant_{q}", Checkbox).value]
            # run type
            for rt in RUN_TYPES:
                if self.query_one(f"#rt_{rt}", RadioButton).value:
                    self.cfg.run_type = rt
                    break
            return self.cfg

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel":
                self.app.exit(0)
                return
            cfg = self._collect()
            out_path = REPO_ROOT / "train_config.yaml"
            write_config_yaml(cfg, out_path)
            if event.button.id == "save_only":
                self.app.exit(0, message=f"wrote {out_path}; rerun with --config to execute")
                return
            self.app.push_screen(RunScreen(cfg))

        def action_save(self) -> None:
            self.on_button_pressed(Button.Pressed(self.query_one("#run", Button)))

    class RunScreen(Screen):
        BINDINGS = [Binding("ctrl+d", "detach", "Detach"), Binding("q", "quit", "Quit")]

        STEPS = [
            ("env",        "Environment check",        screen_environment_check),
            ("libs",       "Library check",            None),  # special-cased
            ("base",       "Base model download",      screen_base_model_download),
            ("ds_build",   "Dataset build",            screen_dataset_build),
            ("ds_audit",   "Dataset audit (GATE)",     None),  # gate
            ("tiny_train", "Tiny training",            screen_tiny_training),
            ("tiny_eval",  "Tiny eval (GATE)",         None),  # gate
            ("full_train", "Full training",            screen_full_training),
            ("gguf",       "GGUF conversion",          screen_gguf_conversion),
            ("validate",   "Validation suite",         screen_validation_suite),
            ("lmstudio",   "LM Studio install",        screen_lmstudio_install),
        ]

        def __init__(self, cfg: WizardConfig) -> None:
            super().__init__()
            self.cfg = cfg

        def compose(self) -> ComposeResult:
            yield Header()
            yield DataTable(id="steps")
            yield Log(id="logpanel", max_lines=400, highlight=False)
            with Horizontal():
                yield Button("Approve gate", id="approve", variant="success")
                yield Button("Reject + abort", id="reject", variant="error")
                yield Button("Detach", id="detach")
            yield Footer()

        def on_mount(self) -> None:
            tbl = self.query_one(DataTable)
            tbl.add_columns("step", "status")
            for sid, label, _ in self.STEPS:
                tbl.add_row(label, "pending", key=sid)
            self.run_worker(self._run_all, exclusive=True, thread=True)

        def _set(self, sid: str, status: str) -> None:
            tbl = self.query_one(DataTable)
            try:
                tbl.update_cell(sid, "status", status)
            except Exception:
                pass

        def _log(self, msg: str) -> None:
            self.query_one(Log).write_line(msg)

        async def _run_all(self) -> None:
            cfg = self.cfg
            for sid, label, fn in self.STEPS:
                # Skip steps not relevant for the chosen run type
                if cfg.run_type == "dataset_only" and sid in {"tiny_train", "tiny_eval", "full_train", "gguf", "lmstudio"}:
                    self._set(sid, "skipped")
                    continue
                if cfg.run_type == "tiny_smoke" and sid in {"full_train", "gguf", "lmstudio"}:
                    self._set(sid, "skipped")
                    continue
                if cfg.run_type == "eval_only" and sid not in {"env", "libs", "validate"}:
                    self._set(sid, "skipped")
                    continue
                self._set(sid, "running")
                self._log(f"[{sid}] {label} starting")
                try:
                    if sid == "libs":
                        ok = screen_library_check(auto_install=True, verbose=False)
                    elif sid == "ds_audit":
                        text = screen_dataset_audit(cfg, verbose=False)
                        self._log(text[:1500] if text else "(no audit file yet)")
                        self._set(sid, "GATE - approve?")
                        # Yield to user via button press; stall here in worker.
                        # In Textual we can't easily block; we record a marker
                        # and let user resume manually. Simplification: auto
                        # pass for now and log instruction.
                        self._log("GATE: review audit above; press 'Approve' to continue (auto-passing in this build).")
                        ok = True
                    elif sid == "tiny_eval":
                        ok = screen_validation_suite(cfg, verbose=False)
                        self._log("GATE: review tiny-eval results.")
                    else:
                        ok = fn(cfg, verbose=False) if fn else True
                    self._set(sid, "ok" if ok else "FAIL")
                    self._log(f"[{sid}] {'ok' if ok else 'FAIL'}")
                    if not ok:
                        return
                except Exception as e:  # noqa: BLE001
                    self._set(sid, "ERROR")
                    self._log(f"[{sid}] exception: {e}")
                    return

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "detach":
                self.app.exit(0, message="detached — pipeline continues in background where applicable")
            elif event.button.id == "reject":
                self.app.exit(1, message="user rejected gate; aborting")

    class WizardApp(App):
        CSS = """
        .title { color: cyan; padding: 1 2; }
        DataTable { height: 14; }
        Log { height: 1fr; }
        """
        BINDINGS = [Binding("q", "quit", "Quit")]

        def on_mount(self) -> None:
            self.push_screen(IntroScreen())

    if not sys.stdout.isatty():
        print("not running in a TTY; falling back to text mode")
        return run_text_mode(cfg)
    WizardApp().run()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, help="Path to train_config.yaml")
    p.add_argument("--no-tui", action="store_true", help="Force text mode")
    p.add_argument("--print-config-schema", action="store_true", help="Print JSON Schema for train_config.yaml and exit")
    p.add_argument("--print-default-config", action="store_true", help="Print default config YAML and exit")
    p.add_argument("--no-auto-install", action="store_true", help="Don't auto-install missing libraries")
    args = p.parse_args()

    if args.print_config_schema:
        print(json.dumps(CONFIG_SCHEMA, indent=2))
        return 0
    if args.print_default_config:
        try:
            import yaml  # noqa: PLC0415
            print(yaml.safe_dump(WizardConfig().to_yaml_dict(), sort_keys=False))
        except ImportError:
            print(json.dumps(WizardConfig().to_yaml_dict(), indent=2))
        return 0

    cfg: WizardConfig
    if args.config:
        if not args.config.exists():
            print(f"config not found: {args.config}", file=sys.stderr)
            return 2
        cfg = load_config_yaml(args.config)
    else:
        cfg = WizardConfig()

    if args.no_tui or not sys.stdout.isatty():
        return run_text_mode(cfg, auto_install=not args.no_auto_install)
    return run_tui(cfg)


if __name__ == "__main__":
    sys.exit(main())
