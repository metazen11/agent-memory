# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "torch>=2.3.0",
#     "transformers>=4.44.0",
#     "peft>=0.12.0",
#     "trl>=0.9.0",
#     "datasets>=2.20.0",
#     "accelerate>=0.33.0",
#     "huggingface_hub>=0.24.0",
# ]
# ///
"""train_hf_job.py — self-contained TRL/PEFT LoRA SFT trainer for HF Jobs.

This is a UV script (PEP 723 inline metadata above). It is designed to be run
via `hf jobs uv run scripts/fine_tune/train_hf_job.py ...` on a rented GPU
(default target: l4x1, 1x L4 24GB). The `hf jobs uv run` CLI uploads THIS local
file to the ephemeral container automatically — it does NOT need to be
pre-published to the Hub (verified: `hf jobs uv run SCRIPT` accepts a local
file path and ships it, 2026-07-04).

Everything is parameterized via environment variables so the launcher
(`run_hf_job.sh` / `launch_pilot_4b.sh`) can pass knobs with `--env`. The
matching runbook is docs/runbooks/hf-jobs-finetune.md.

WHAT IT DOES
------------
1. Loads a base model (default Qwen/Qwen3-4B) + its tokenizer.
2. Loads a Hub dataset (default WFCA/agentmem-toolcalls-v5-pilot, PRIVATE).
   Rows carry keys: messages, tools, project_tag, multi_turn, source_ids.
3. Renders each row with `tokenizer.apply_chat_template(messages, tools=...)`
   and applies PROVEN assistant-only label masking (span-walk on the rendered
   text, deterministic -100 labels). This is ported verbatim in behaviour from
   the local trainer models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py.
   We do NOT rely on the chat template emitting generation markers — we locate
   `<|im_start|>assistant ... <|im_end|>` spans ourselves.
4. Trains a LoRA adapter with TRL's SFTTrainer (custom collator supplies the
   pre-masked labels; SFT's own formatting is bypassed).
5. Pushes the adapter to HUB_MODEL_ID (PRIVATE) at the end so it survives the
   ephemeral container teardown.

MAX_LENGTH NOTE
---------------
The local Mac trainer capped MAX_LENGTH at 2048 ONLY because of Apple unified-
memory swap thrash (the failure that killed the v5 pilot at step 68). On a real
VRAM budget (L4 24GB) that cap does NOT apply. Default here is 4096 to cover
full multi-turn transcripts without truncating the assistant tool-call spans.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

import torch
from datasets import Dataset, load_dataset
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainerCallback,
    TrainingArguments,
    set_seed,
)

# --- Config (env-var driven; defaults are the pilot values) -----------------

BASE_MODEL = os.getenv("BASE_MODEL", "Qwen/Qwen3-4B")
DATASET = os.getenv("DATASET", "WFCA/agentmem-toolcalls-v5-pilot")
DATASET_SPLIT = os.getenv("DATASET_SPLIT", "train")
DATASET_CONFIG = os.getenv("DATASET_CONFIG", "") or None
HUB_MODEL_ID = os.getenv("HUB_MODEL_ID", "WFCA/qwen3-4b-toolcalls-v5-pilot")
PUSH_PRIVATE = os.getenv("PUSH_PRIVATE", "1") != "0"  # default PRIVATE — data governance

MAX_LENGTH = int(os.getenv("MAX_LENGTH", "4096"))
EPOCHS = float(os.getenv("EPOCHS", "1.0"))
LR = float(os.getenv("LR", "2e-4"))
LR_SCHEDULER_TYPE = os.getenv("LR_SCHEDULER_TYPE", "cosine")
WARMUP_RATIO = float(os.getenv("WARMUP_RATIO", "0.05"))
WEIGHT_DECAY = float(os.getenv("WEIGHT_DECAY", "0.01"))
PER_DEVICE_BATCH = int(os.getenv("PER_DEVICE_BATCH", "2"))
GRAD_ACCUM = int(os.getenv("GRAD_ACCUM", "4"))
EVAL_STEPS = int(os.getenv("EVAL_STEPS", "50"))
SAVE_STEPS = int(os.getenv("SAVE_STEPS", "50"))
LOGGING_STEPS = int(os.getenv("LOGGING_STEPS", "5"))
VALID_SPLIT_PCT = float(os.getenv("VALID_SPLIT_PCT", "0.05"))

LORA_R = int(os.getenv("LORA_R", "16"))
LORA_ALPHA = int(os.getenv("LORA_ALPHA", str(LORA_R * 2)))
LORA_DROPOUT = float(os.getenv("LORA_DROPOUT", "0.05"))

SEED = int(os.getenv("SEED", "42"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/tmp/agentmem-lora-out")

# --- Banner -----------------------------------------------------------------

print("=" * 72, flush=True)
print("agent-memory tool-call LoRA — HF Jobs trainer", flush=True)
print("=" * 72, flush=True)
print(f"  base_model:    {BASE_MODEL}", flush=True)
print(f"  dataset:       {DATASET} (split={DATASET_SPLIT}, config={DATASET_CONFIG})", flush=True)
print(f"  hub_model_id:  {HUB_MODEL_ID}  (private={PUSH_PRIVATE})", flush=True)
print(f"  max_length:    {MAX_LENGTH}   (Mac 2048 cap does NOT apply on VRAM)", flush=True)
print(f"  epochs:        {EPOCHS}  lr={LR}  sched={LR_SCHEDULER_TYPE}  warmup={WARMUP_RATIO}", flush=True)
print(f"  batch/accum:   {PER_DEVICE_BATCH} x {GRAD_ACCUM}", flush=True)
print(f"  lora:          r={LORA_R} alpha={LORA_ALPHA} dropout={LORA_DROPOUT}", flush=True)
print(f"  eval/save:     {EVAL_STEPS}/{SAVE_STEPS}  valid_split={VALID_SPLIT_PCT}", flush=True)
print(f"  seed:          {SEED}", flush=True)
print("=" * 72, flush=True)

if not os.getenv("HF_TOKEN"):
    print(
        "WARN: HF_TOKEN is not set in the container. push_to_hub will fail and "
        "the adapter will be LOST on teardown. Launch with `--secrets HF_TOKEN`.",
        file=sys.stderr,
        flush=True,
    )

set_seed(SEED)

# --- Load tokenizer + model -------------------------------------------------

device = "cuda" if torch.cuda.is_available() else "cpu"
dtype = torch.bfloat16 if device == "cuda" else torch.float32
print(f"[load] device={device} dtype={dtype}", flush=True)

tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, use_fast=True, trust_remote_code=False)
# offset_mapping (used by the assistant-mask span-walk) requires a FAST tokenizer.
# Assert now so we fail in seconds, not after paying for the model download.
assert tokenizer.is_fast, "need a fast tokenizer for return_offsets_mapping (assistant-mask)"
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL,
    torch_dtype=dtype,
    trust_remote_code=False,
    device_map={"": 0} if device == "cuda" else None,
)
model.config.use_cache = False  # required for gradient checkpointing / training

lora_cfg = LoraConfig(
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    task_type="CAUSAL_LM",
)
model = get_peft_model(model, lora_cfg)
# Belt-and-suspenders for gradient checkpointing + PEFT: ensure the base's input
# embeddings require grad so gradients flow through the frozen base to the LoRA
# adapters. peft>=0.12 wires this via gradient_checkpointing_enable, but calling
# it explicitly removes all doubt (avoids a silent "does not require grad" crash
# on the first backward — which would burn GPU minutes).
model.enable_input_require_grads()  # type: ignore[operator]  # PeftModel method; Pyright mis-infers the union
model.print_trainable_parameters()

# --- Assistant-only label masking (ported from the local trainer) -----------
# Strategy: render the full conversation, tokenize with offset mapping, then
# walk the rendered TEXT to find each `<|im_start|>assistant ... <|im_end|>`
# span and mark ONLY those token positions as 'predict' (label = input_id);
# everything else is -100. We do NOT trust template-generated markers — the
# span-walk is deterministic and is the proven approach.

ASSISTANT_HEADER = "<|im_start|>assistant"
IM_END = "<|im_end|>"


def render_with_assistant_mask(row: dict) -> tuple[list[int], list[int], int, int]:
    """Return (input_ids, labels, n_assistant_spans, n_assistant_msgs).

    The two counts let the caller assert the span-walk found exactly the
    assistant turns present in the source `messages` — a guard against
    chat-template drift silently masking the wrong turns (Qwen3's template
    differs from the qwen2.5 original this was ported from).
    """
    n_assistant_msgs = sum(1 for m in row["messages"] if m.get("role") == "assistant")

    text = tokenizer.apply_chat_template(
        row["messages"], tools=row.get("tools"), tokenize=False, add_generation_prompt=False
    )
    encoded = tokenizer(
        text, truncation=True, max_length=MAX_LENGTH, padding=False, return_offsets_mapping=True
    )
    input_ids = encoded["input_ids"]
    offsets = encoded["offset_mapping"]

    # Find every assistant span [start_char, end_char) in the rendered text.
    spans = []
    cursor = 0
    while True:
        i = text.find(ASSISTANT_HEADER, cursor)
        if i < 0:
            break
        content_start = i + len(ASSISTANT_HEADER)
        if content_start < len(text) and text[content_start] == "\n":
            content_start += 1  # eat the newline after the header
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
    return input_ids, labels, len(spans), n_assistant_msgs


def build_samples(rows) -> list[dict]:
    """Build masked samples. Fail loudly on any zero-predicted-token row.

    A row whose assistant span tokenizes to zero non-special tokens produces
    NaN under CrossEntropyLoss(ignore_index=-100). Upstream dataset builders
    gate these out; if any survive, the dataset is non-conformant — fail rather
    than silently skip (silent skips were the original NaN-eval root cause).
    """
    samples = []
    n_predicted = 0
    bad_rows = []       # zero predicted tokens (empty/truncated assistant span)
    span_mismatch = []  # span-walk found != number of assistant messages
    for idx, row in enumerate(rows):
        input_ids, labels, n_spans, n_asst_msgs = render_with_assistant_mask(row)
        # Guard against chat-template drift: the span-walk must find exactly the
        # assistant turns present in the source. A mismatch means the mask is
        # keying on the wrong markers (would train on / ignore the wrong tokens).
        if n_spans != n_asst_msgs:
            span_mismatch.append((idx, n_spans, n_asst_msgs))
        n_pred = sum(1 for x in labels if x != -100)
        if n_pred == 0:
            bad_rows.append(idx)
            continue
        samples.append(
            {
                "input_ids": input_ids,
                "attention_mask": [1] * len(input_ids),
                "labels": labels,
            }
        )
        n_predicted += n_pred

    if span_mismatch:
        head = span_mismatch[:5]
        raise RuntimeError(
            f"FAIL: {len(span_mismatch)} rows where the assistant span-walk count "
            f"!= number of assistant messages (first 5 as (row, spans_found, "
            f"asst_msgs): {head}). The chat template markers "
            f"('{ASSISTANT_HEADER}'...'{IM_END}') do not match this model's "
            f"template — masking is unreliable. Do NOT train; fix the marker "
            f"detection for BASE_MODEL={BASE_MODEL}."
        )
    if bad_rows:
        raise RuntimeError(
            f"FAIL: {len(bad_rows)} rows have zero predicted tokens after "
            f"assistant-only masking (first 10 indices: {bad_rows[:10]}). Either "
            f"the dataset is non-conformant, or MAX_LENGTH={MAX_LENGTH} truncated "
            f"the assistant turn — raise MAX_LENGTH or rebuild the dataset."
        )

    print(
        f"  built {len(samples)} samples, mean predicted tokens/sample: "
        f"{n_predicted / max(1, len(samples)):.1f}",
        flush=True,
    )
    return samples


# --- Load dataset from the Hub + split --------------------------------------

print(f"[data] loading {DATASET} split={DATASET_SPLIT} ...", flush=True)
ds = load_dataset(DATASET, name=DATASET_CONFIG, split=DATASET_SPLIT)
# Passing a concrete `split=` string yields a map-style Dataset (sized,
# splittable). Assert it so we fail loud on a streaming/dict handle rather
# than crashing deep in train_test_split — and so the type is narrowed.
assert isinstance(ds, Dataset), f"expected a map-style Dataset, got {type(ds).__name__}"
print(f"[data] {len(ds)} rows loaded", flush=True)

split = ds.train_test_split(test_size=VALID_SPLIT_PCT, seed=SEED)
train_rows, valid_rows = split["train"], split["test"]

print("[data] building train samples...", flush=True)
train_samples = build_samples(train_rows)
print("[data] building valid samples...", flush=True)
valid_samples = build_samples(valid_rows)


class ListDataset(torch.utils.data.Dataset):
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, idx):
        return self.rows[idx]


def collate(batch):
    max_len = max(len(x["input_ids"]) for x in batch)
    pad_id = tokenizer.pad_token_id
    input_ids, attention_mask, labels = [], [], []
    for x in batch:
        pad = max_len - len(x["input_ids"])
        input_ids.append(x["input_ids"] + [pad_id] * pad)
        attention_mask.append(x["attention_mask"] + [0] * pad)
        labels.append(x["labels"] + [-100] * pad)
    return {
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
    }


train_ds = ListDataset(train_samples)
valid_ds = ListDataset(valid_samples)

# --- Train ------------------------------------------------------------------

bf16_ok = dtype == torch.bfloat16

training_args = TrainingArguments(
    output_dir=OUTPUT_DIR,
    num_train_epochs=EPOCHS,
    per_device_train_batch_size=PER_DEVICE_BATCH,
    per_device_eval_batch_size=PER_DEVICE_BATCH,
    gradient_accumulation_steps=GRAD_ACCUM,
    learning_rate=LR,
    lr_scheduler_type=LR_SCHEDULER_TYPE,
    warmup_ratio=WARMUP_RATIO,
    weight_decay=WEIGHT_DECAY,
    logging_steps=LOGGING_STEPS,
    eval_strategy="steps",
    eval_steps=EVAL_STEPS,
    save_strategy="steps",
    save_steps=SAVE_STEPS,
    save_total_limit=2,
    bf16=bf16_ok,
    fp16=False,
    gradient_checkpointing=True,
    report_to=[],
    seed=SEED,
    data_seed=SEED,
    remove_unused_columns=False,
)


class NanGuardCallback(TrainerCallback):
    """Abort on NaN/Inf loss (train or eval) — the proven fail-fast guard."""

    def on_log(self, args, state, control, logs=None, **kwargs):
        _ = (args, control, kwargs)
        if not logs:
            return
        loss = logs.get("loss")
        if loss is not None and (math.isnan(loss) or math.isinf(loss)):
            raise RuntimeError(f"FAIL-FAST: training loss is {loss} at step {state.global_step}")

    def on_evaluate(self, args, state, control, metrics=None, **kwargs):
        _ = (args, control, kwargs)
        if not metrics:
            return
        eval_loss = metrics.get("eval_loss")
        if eval_loss is not None and (math.isnan(eval_loss) or math.isinf(eval_loss)):
            raise RuntimeError(f"FAIL-FAST: eval_loss={eval_loss} at step {state.global_step}")


trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_ds,
    eval_dataset=valid_ds,
    data_collator=collate,
    callbacks=[NanGuardCallback()],
)

t0 = time.time()
trainer.train()
print(f"[train] finished in {time.time() - t0:.0f}s", flush=True)

# --- Save + push the adapter (PRIVATE) --------------------------------------

model.save_pretrained(OUTPUT_DIR)
tokenizer.save_pretrained(OUTPUT_DIR)

run_meta = {
    "base_model": BASE_MODEL,
    "dataset": DATASET,
    "hub_model_id": HUB_MODEL_ID,
    "epochs": EPOCHS,
    "lr": LR,
    "max_length": MAX_LENGTH,
    "lora_r": LORA_R,
    "lora_alpha": LORA_ALPHA,
    "lora_dropout": LORA_DROPOUT,
    "per_device_batch": PER_DEVICE_BATCH,
    "grad_accum": GRAD_ACCUM,
    "seed": SEED,
    "train_samples": len(train_ds),
    "valid_samples": len(valid_ds),
}
with open(os.path.join(OUTPUT_DIR, "run_meta.json"), "w") as fh:
    json.dump(run_meta, fh, indent=2, sort_keys=True)

# DATA GOVERNANCE: push_to_hub(private=...) is IGNORED if the repo already
# exists, so we cannot rely on it to make an existing repo private. Force the
# private setting explicitly: create the repo private (idempotent), then push.
# This guarantees a re-run never leaks the private adapter to a public repo.
from huggingface_hub import HfApi  # noqa: E402  (kept local; only needed at push time)

api = HfApi()
print(f"[push] ensuring {HUB_MODEL_ID} exists and is private={PUSH_PRIVATE} ...", flush=True)
api.create_repo(HUB_MODEL_ID, repo_type="model", private=PUSH_PRIVATE, exist_ok=True)
# Belt-and-suspenders: if the repo pre-existed, create_repo won't change its
# visibility — force it to match PUSH_PRIVATE.
api.update_repo_settings(HUB_MODEL_ID, private=PUSH_PRIVATE, repo_type="model")

print(f"[push] pushing adapter to {HUB_MODEL_ID} ...", flush=True)
model.push_to_hub(HUB_MODEL_ID, private=PUSH_PRIVATE)
tokenizer.push_to_hub(HUB_MODEL_ID, private=PUSH_PRIVATE)
print(f"OK: adapter pushed to https://huggingface.co/{HUB_MODEL_ID}", flush=True)
