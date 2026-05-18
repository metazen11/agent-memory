#!/usr/bin/env python3
"""LoRA fine-tune for Qwen2.5 (or any HF model) on tool-call training data.

Reusable across MODELS in scripts/fine_tune/lib.py. Override the model slug
and dataset tier via env vars; defaults target Qwen2.5-3B-Instruct tiny.

Usage:
    DATASET_TIER=tiny RUN_TAG=tiny-smoke .venv-finetune/bin/python \\
        models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py

    DATASET_TIER=full RUN_TAG=full-v1 ... (full 16k row run)

Key choices vs prior Qwen 3.5 script:
- apply_chat_template(tools=row["tools"]) -> native Qwen 2.5 <tool_call> format
- assistant-only label masking (loss only on tool-call tokens)
- bfloat16 on MPS with fp32 fallback if first-step loss is NaN
- output isolation: runs/<UTC>/ -> latest symlink only on completion
- seed=42 for determinism

Env vars (all optional):
    MODEL_SLUG          default 'qwen2.5-3b-instruct'
    DATASET_TIER        'tiny' (default) or 'full'
    RUN_TAG             string appended to run dir name
    DATASET_VERSION     default 'v1' (subdir under data/processed/qwen25_tools/)
    MAX_LENGTH          default 1024 (full), 512 (tiny)
    EPOCHS              default 1.0 (full), 0.5 (tiny)
    LR                  default 2e-4
    GRAD_ACCUM          default 4 (full), 2 (tiny)
    LOGGING_STEPS       default 10 (full), 5 (tiny)
    EVAL_STEPS          default 250 (full), 50 (tiny)
    SAVE_STEPS          default 500 (full), 100 (tiny)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# stdlib path bootstrap so we can import lib.py
# Avoid .resolve() — models/ symlinks to Dropbox cold storage, and lib.py
# may not have synced yet. Use the invocation path directly so we stay in
# the working tree.
sys.path.insert(0, str(Path(__file__).absolute().parents[3] / "scripts" / "fine_tune"))

import math  # noqa: E402
import time  # noqa: E402

import torch  # noqa: E402
from peft import LoraConfig, get_peft_model  # noqa: E402
from transformers import (  # noqa: E402
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainerCallback,
    EarlyStoppingCallback,
    TrainingArguments,
    set_seed,
)

from lib import REPO_ROOT, get_model  # noqa: E402  # pyright: ignore[reportMissingImports]

# --- Config -----------------------------------------------------------------

MODEL_SLUG = os.getenv("MODEL_SLUG", "qwen2.5-3b-instruct")
DATASET_TIER = os.getenv("DATASET_TIER", "tiny").lower()
RUN_TAG = os.getenv("RUN_TAG", DATASET_TIER)
DATASET_VERSION = os.getenv("DATASET_VERSION", "v1")
DATASET_FAMILY = os.getenv("DATASET_FAMILY", "qwen25_tools")
SEED = 42

assert DATASET_TIER in ("tiny", "full"), f"DATASET_TIER must be 'tiny' or 'full', got {DATASET_TIER!r}"

spec = get_model(MODEL_SLUG)

DATA_DIR = REPO_ROOT / "data" / "processed" / DATASET_FAMILY / DATASET_VERSION
if DATASET_TIER == "tiny":
    TRAIN_FILE = DATA_DIR / "train.tiny.jsonl"
    VALID_FILE = DATA_DIR / "valid.tiny.jsonl"
    DEFAULT_MAX_LENGTH = 512
    DEFAULT_EPOCHS = 0.5
    DEFAULT_GRAD_ACCUM = 2
    DEFAULT_LOGGING_STEPS = 5
    DEFAULT_EVAL_STEPS = 50
    DEFAULT_SAVE_STEPS = 100
else:
    TRAIN_FILE = DATA_DIR / "train.chat.jsonl"
    VALID_FILE = DATA_DIR / "valid.chat.jsonl"
    DEFAULT_MAX_LENGTH = 1024
    DEFAULT_EPOCHS = 1.0
    DEFAULT_GRAD_ACCUM = 4
    DEFAULT_LOGGING_STEPS = 10
    DEFAULT_EVAL_STEPS = 500  # eval is ~30min at MAX_LENGTH=1024 — don't do it too often
    DEFAULT_SAVE_STEPS = 250  # save more often than eval — survive crashes

MAX_LENGTH = int(os.getenv("MAX_LENGTH", str(DEFAULT_MAX_LENGTH)))
EPOCHS = float(os.getenv("EPOCHS", str(DEFAULT_EPOCHS)))
LR = float(os.getenv("LR", "2e-4"))
GRAD_ACCUM = int(os.getenv("GRAD_ACCUM", str(DEFAULT_GRAD_ACCUM)))
LOGGING_STEPS = int(os.getenv("LOGGING_STEPS", str(DEFAULT_LOGGING_STEPS)))
EVAL_STEPS = int(os.getenv("EVAL_STEPS", str(DEFAULT_EVAL_STEPS)))
SAVE_STEPS = int(os.getenv("SAVE_STEPS", str(DEFAULT_SAVE_STEPS)))

# v4.5 proven-practice knobs (PROVEN_PRACTICES.md, top 5).
# Defaults match v4 behavior (no cosine, no warmup, no decay, no early-stop)
# so v3/v4 reproducibility is preserved. v4.5 launcher will set these.
LR_SCHEDULER_TYPE = os.getenv("LR_SCHEDULER_TYPE", "constant")
WARMUP_RATIO = float(os.getenv("WARMUP_RATIO", "0.0"))
WEIGHT_DECAY = float(os.getenv("WEIGHT_DECAY", "0.0"))
EARLY_STOP_PATIENCE = int(os.getenv("EARLY_STOP_PATIENCE", "0"))   # 0 = disabled
LOAD_BEST_AT_END = os.getenv("LOAD_BEST_AT_END", "0") == "1"

# Output: a fresh runs/<UTC>/ dir, only promoted to `latest` on completion
RUN_STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
RUN_DIR = spec.lora_dir / "runs" / f"{RUN_STAMP}-{RUN_TAG}"
LATEST = spec.lora_dir / "latest"

# --- Banner -----------------------------------------------------------------

print("=" * 72)
print(f"Qwen tool-call LoRA training run")
print("=" * 72)
print(f"  model_slug:    {MODEL_SLUG}")
print(f"  base:          {spec.base_dir}")
print(f"  dataset_tier:  {DATASET_TIER}")
print(f"  dataset_ver:   {DATASET_VERSION}")
print(f"  train_file:    {TRAIN_FILE.relative_to(REPO_ROOT)}")
print(f"  valid_file:    {VALID_FILE.relative_to(REPO_ROOT)}")
print(f"  max_length:    {MAX_LENGTH}")
print(f"  epochs:        {EPOCHS}")
print(f"  grad_accum:    {GRAD_ACCUM}")
print(f"  lr_scheduler:  {LR_SCHEDULER_TYPE}  warmup_ratio={WARMUP_RATIO}  weight_decay={WEIGHT_DECAY}")
print(f"  early_stop:    patience={EARLY_STOP_PATIENCE}  load_best={LOAD_BEST_AT_END}")
print(f"  lr:            {LR}")
print(f"  logging_steps: {LOGGING_STEPS}")
print(f"  eval_steps:    {EVAL_STEPS}")
print(f"  save_steps:    {SAVE_STEPS}")
print(f"  seed:          {SEED}")
print(f"  run_dir:       {RUN_DIR.relative_to(REPO_ROOT)}")
print("=" * 72)

if not spec.revision_file.exists():
    sys.exit(f"FAIL: base model not downloaded. Run scripts/fine_tune/download_base.py {MODEL_SLUG}")
if not TRAIN_FILE.exists():
    sys.exit(f"FAIL: {TRAIN_FILE} not found. Run fine-tune/restructure_to_qwen_tools.py")

RUN_DIR.mkdir(parents=True, exist_ok=True)
set_seed(SEED)

# --- Load tokenizer + model -------------------------------------------------

tokenizer = AutoTokenizer.from_pretrained(
    str(spec.base_dir), use_fast=True, local_files_only=True, trust_remote_code=False
)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

device = "mps" if torch.backends.mps.is_available() else "cpu"
dtype = torch.bfloat16 if device == "mps" else torch.float32
print(f"loading model on {device} ({dtype})...")
model = AutoModelForCausalLM.from_pretrained(
    str(spec.base_dir),
    local_files_only=True,
    trust_remote_code=False,
    torch_dtype=dtype,
)

LORA_R = int(os.getenv("LORA_R", "16"))
LORA_ALPHA = int(os.getenv("LORA_ALPHA", str(LORA_R * 2)))
LORA_DROPOUT = float(os.getenv("LORA_DROPOUT", "0.05"))
lora_cfg = LoraConfig(
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    task_type="CAUSAL_LM",
)
print(f"  lora:          r={LORA_R} alpha={LORA_ALPHA} dropout={LORA_DROPOUT}")
model = get_peft_model(model, lora_cfg)
model.print_trainable_parameters()

# --- Sample builders (assistant-only label masking) -------------------------

ASSISTANT_HEADER = "<|im_start|>assistant"
IM_END = "<|im_end|>"


def iter_rows(path: Path):
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def render_with_assistant_mask(row: dict) -> tuple[list[int], list[int]]:
    """Render a row and return (input_ids, labels) with non-assistant tokens masked to -100.

    Strategy: tokenize the full conversation, then walk the rendered text to find
    each <|im_start|>assistant ... <|im_end|> span and mark those token positions
    as 'predict'; everything else is -100.
    """
    text = tokenizer.apply_chat_template(
        row["messages"], tools=row.get("tools"), tokenize=False, add_generation_prompt=False
    )
    encoded = tokenizer(text, truncation=True, max_length=MAX_LENGTH, padding=False, return_offsets_mapping=True)
    input_ids = encoded["input_ids"]
    offsets = encoded["offset_mapping"]

    # Find every assistant span [start_char, end_char) in the rendered text
    spans = []
    cursor = 0
    while True:
        i = text.find(ASSISTANT_HEADER, cursor)
        if i < 0:
            break
        content_start = i + len(ASSISTANT_HEADER)
        # Eat the newline after the header
        if content_start < len(text) and text[content_start] == "\n":
            content_start += 1
        j = text.find(IM_END, content_start)
        if j < 0:
            j = len(text)
        spans.append((content_start, j))
        cursor = j + len(IM_END)

    labels = [-100] * len(input_ids)
    for tok_i, (s, e) in enumerate(offsets):
        if s == e:  # special token, no chars
            continue
        for span_s, span_e in spans:
            if s >= span_s and e <= span_e:
                labels[tok_i] = input_ids[tok_i]
                break
    return input_ids, labels


def build_samples(path: Path):
    """Build training samples. Asserts dataset is conformant.

    Rows where the assistant span tokenizes to zero non-special tokens
    produce NaN under CrossEntropyLoss(ignore_index=-100) at batch_size=1.
    These rows MUST be filtered upstream by build_v3_dataset.py's Fix #10
    gate and verified by scripts/fine_tune/preflight.sh. If any survive
    to this point, the dataset is bad — fail loudly rather than silently
    skip, because silent skips were the original NaN-eval root cause
    (2026-05-16 incident).
    """
    samples = []
    n_predicted = 0
    bad_rows = []
    for idx, row in enumerate(iter_rows(path)):
        input_ids, labels = render_with_assistant_mask(row)
        n_pred = sum(1 for x in labels if x != -100)
        if n_pred == 0:
            bad_rows.append(idx)
            continue
        attn = [1] * len(input_ids)
        samples.append({"input_ids": input_ids, "attention_mask": attn, "labels": labels})
        n_predicted += n_pred

    if bad_rows:
        raise RuntimeError(
            f"FAIL: {path.name} has {len(bad_rows)} rows with zero predicted "
            f"tokens after assistant-only masking (first 10 indices: "
            f"{bad_rows[:10]}). Dataset is non-conformant. Rebuild with "
            f"scripts/fine_tune/build_v3_dataset.py (Fix #10 gate) and "
            f"re-run scripts/fine_tune/preflight.sh."
        )

    print(
        f"  built {len(samples)} samples from {path.name}, "
        f"mean predicted tokens/sample: {n_predicted/max(1,len(samples)):.1f}"
    )
    return samples


class ListDataset(torch.utils.data.Dataset):
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        return self.rows[idx]


def collate(batch):
    max_len = max(len(x["input_ids"]) for x in batch)
    input_ids, attention_mask, labels = [], [], []
    pad_id = tokenizer.pad_token_id
    for x in batch:
        n = len(x["input_ids"])
        pad = max_len - n
        input_ids.append(x["input_ids"] + [pad_id] * pad)
        attention_mask.append(x["attention_mask"] + [0] * pad)
        labels.append(x["labels"] + [-100] * pad)
    return {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
    }


print("[load] building train samples...")
train_ds = ListDataset(build_samples(TRAIN_FILE))
print("[load] building valid samples...")
valid_ds = ListDataset(build_samples(VALID_FILE))


# --- Train ------------------------------------------------------------------

bf16_ok = (dtype == torch.bfloat16)

training_args = TrainingArguments(
    output_dir=str(RUN_DIR),
    num_train_epochs=EPOCHS,
    per_device_train_batch_size=1,
    per_device_eval_batch_size=1,  # MPS OOMs at default 8 with 1024 context — keep 1.
    gradient_accumulation_steps=GRAD_ACCUM,
    learning_rate=LR,
    lr_scheduler_type=LR_SCHEDULER_TYPE,  # default "constant"; v4.5 sets "cosine"
    warmup_ratio=WARMUP_RATIO,            # default 0.0; v4.5 sets 0.05
    weight_decay=WEIGHT_DECAY,            # default 0.0; v4.5 sets 0.01
    logging_steps=LOGGING_STEPS,
    eval_strategy="steps",
    eval_steps=EVAL_STEPS,
    save_strategy="steps",
    save_steps=SAVE_STEPS,
    save_total_limit=3,
    bf16=bf16_ok,
    fp16=False,
    report_to=[],
    seed=SEED,
    data_seed=SEED,
    remove_unused_columns=False,
    load_best_model_at_end=LOAD_BEST_AT_END,
    metric_for_best_model="eval_loss" if LOAD_BEST_AT_END else None,
    greater_is_better=False if LOAD_BEST_AT_END else None,
)

HEARTBEAT_PATH = RUN_DIR / "heartbeat.txt"


# NOTE: An earlier Fp32EvalTrainer subclass cast the model to fp32 inside
# every prediction_step on MPS. That added 2× full 4B-param dtype casts per
# eval sample (≈ 36 s/sample vs 1.2 s/sample baseline = 30× slowdown).
# Once empty-label rows are filtered upstream (build_v3_dataset.py Fix #10),
# bf16 eval produces finite loss; the cast was solving a problem we no
# longer have. Removed 2026-05-16. See docs/training_runs/v3-incident-20260515.md.


class NanGuardAndHeartbeatCallback(TrainerCallback):
    """Abort on eval_loss=nan; touch a heartbeat file every logging step."""

    def on_log(self, args, state, control, logs=None, **kwargs):
        _ = (args, control, kwargs)  # TrainerCallback API requires these positions
        if not logs:
            return
        HEARTBEAT_PATH.write_text(f"{time.time():.0f}\tstep={state.global_step}\tepoch={state.epoch}\n")
        loss = logs.get("loss")
        if loss is not None and (math.isnan(loss) or math.isinf(loss)):
            raise RuntimeError(f"FAIL-FAST: training loss is {loss} at step {state.global_step}")

    def on_evaluate(self, args, state, control, metrics=None, **kwargs):
        _ = (args, control, kwargs)  # TrainerCallback API requires these positions
        if not metrics:
            return
        eval_loss = metrics.get("eval_loss")
        if eval_loss is None:
            return
        if math.isnan(eval_loss) or math.isinf(eval_loss):
            raise RuntimeError(
                f"FAIL-FAST: eval_loss={eval_loss} at step {state.global_step}. "
                "Empty-label rows leaked past the Fix #10 gate, or new instability."
            )


_callbacks: list[TrainerCallback] = [NanGuardAndHeartbeatCallback()]
if EARLY_STOP_PATIENCE > 0:
    if not LOAD_BEST_AT_END:
        raise RuntimeError(
            "EARLY_STOP_PATIENCE>0 requires LOAD_BEST_AT_END=1 (else early-stop "
            "ships a worse-than-best checkpoint)."
        )
    _callbacks.append(EarlyStoppingCallback(early_stopping_patience=EARLY_STOP_PATIENCE))

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_ds,
    eval_dataset=valid_ds,
    data_collator=collate,
    callbacks=_callbacks,
)

try:
    # If a previous run left a checkpoint in RUN_DIR (or sibling runs), resume.
    # HF Trainer detects last_checkpoint automatically when passed True.
    from transformers.trainer_utils import get_last_checkpoint  # noqa: PLC0415

    resume = get_last_checkpoint(str(RUN_DIR)) if RUN_DIR.exists() else None
    if resume:
        print(f"[resume] found checkpoint: {resume}")
        trainer.train(resume_from_checkpoint=resume)
    else:
        trainer.train()
except Exception as e:
    print(f"FAIL: training crashed: {e}")
    raise

# Final save (adapter only)
model.save_pretrained(str(RUN_DIR))
tokenizer.save_pretrained(str(RUN_DIR))

# Record run metadata
run_meta = {
    "model_slug": MODEL_SLUG,
    "base_revision": spec.revision_file.read_text().strip(),
    "dataset_version": DATASET_VERSION,
    "dataset_tier": DATASET_TIER,
    "epochs": EPOCHS,
    "lr": LR,
    "grad_accum": GRAD_ACCUM,
    "max_length": MAX_LENGTH,
    "seed": SEED,
    "device": device,
    "dtype": str(dtype),
    "run_stamp": RUN_STAMP,
    "run_tag": RUN_TAG,
    "train_samples": len(train_ds),
    "valid_samples": len(valid_ds),
}
(RUN_DIR / "run_meta.json").write_text(json.dumps(run_meta, indent=2, sort_keys=True) + "\n")

# Promote to latest only on success
if LATEST.exists() or LATEST.is_symlink():
    LATEST.unlink()
LATEST.symlink_to(RUN_DIR.name, target_is_directory=False)  # relative symlink
# Adjust to point at runs/<dir>/
LATEST.unlink()
LATEST.symlink_to(Path("runs") / RUN_DIR.name, target_is_directory=True)

print(f"OK: adapter saved to {RUN_DIR}")
print(f"OK: latest -> {LATEST.readlink()}")
