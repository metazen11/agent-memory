#!/usr/bin/env python3
"""HF/PEFT LoRA training entrypoint for GGUF-compatible workflow.

This path is designed so output adapters can be merged and converted to GGUF.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train LoRA adapter (HF/PEFT)")
    p.add_argument("--base-model", default="google/gemma-2-2b-it")
    p.add_argument("--train-file", default="data/processed/fine_tune_global/train.chat.jsonl")
    p.add_argument("--valid-file", default="data/processed/fine_tune_global/valid.chat.jsonl")
    p.add_argument("--output-dir", default="models/lora/gemma2-toolcalls-lora")
    p.add_argument("--epochs", type=float, default=1.0)
    p.add_argument("--batch-size", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--max-length", type=int, default=2048)
    p.add_argument("--lora-r", type=int, default=16)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--max-train-samples", type=int, default=3000)
    p.add_argument("--max-valid-samples", type=int, default=400)
    p.add_argument("--run", action="store_true")
    return p.parse_args()


def _script_template(args: argparse.Namespace) -> str:
    return f'''#!/usr/bin/env python3
import json
import random
from pathlib import Path
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, Trainer
from peft import LoraConfig, get_peft_model

base_model = {args.base_model!r}
train_file = {args.train_file!r}
valid_file = {args.valid_file!r}
output_dir = {args.output_dir!r}
max_train_samples = {args.max_train_samples}
max_valid_samples = {args.max_valid_samples}

tokenizer = AutoTokenizer.from_pretrained(base_model, use_fast=True)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(base_model)

lora_cfg = LoraConfig(
    r={args.lora_r},
    lora_alpha={args.lora_alpha},
    lora_dropout={args.lora_dropout},
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    task_type="CAUSAL_LM",
)
model = get_peft_model(model, lora_cfg)


def iter_chat_rows(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, list):
                yield obj


def render_chat(msgs):
    text = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)
    return text


def build_samples(path, max_samples):
    samples = []
    for msgs in iter_chat_rows(path):
        text = render_chat(msgs)
        tok = tokenizer(text, truncation=True, max_length={args.max_length}, padding=False)
        samples.append({{"input_ids": tok["input_ids"], "attention_mask": tok["attention_mask"], "labels": tok["input_ids"][:]}})
        if len(samples) >= max_samples:
            break
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
    for x in batch:
        n = len(x["input_ids"])
        pad = max_len - n
        input_ids.append(x["input_ids"] + [tokenizer.pad_token_id] * pad)
        attention_mask.append(x["attention_mask"] + [0] * pad)
        labels.append(x["labels"] + [-100] * pad)
    return {{
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
    }}


train_ds = ListDataset(build_samples(train_file, max_train_samples))
valid_ds = ListDataset(build_samples(valid_file, max_valid_samples))
print("train samples:", len(train_ds), "valid samples:", len(valid_ds))

training_args = TrainingArguments(
    output_dir=output_dir,
    num_train_epochs={args.epochs},
    per_device_train_batch_size={args.batch_size},
    gradient_accumulation_steps={args.grad_accum},
    learning_rate={args.lr},
    logging_steps=10,
    eval_strategy="steps",
    eval_steps=100,
    save_steps=100,
    save_total_limit=2,
    bf16=False,
    fp16=False,
    report_to=[],
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_ds,
    eval_dataset=valid_ds,
    data_collator=collate,
)
trainer.train()
model.save_pretrained(output_dir)
tokenizer.save_pretrained(output_dir)
print("saved lora adapter:", output_dir)
'''


def main() -> None:
    args = parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runner = out_dir / "run_train_lora.py"
    runner.write_text(_script_template(args), encoding="utf-8")

    config_file = out_dir / "train_config.json"
    config_file.write_text(json.dumps(vars(args), indent=2), encoding="utf-8")

    print(f"generated trainer script -> {runner}")
    if not args.run:
        print("dry-run only. execute the generated script when ready:")
        print(f"  ./.venv/bin/python {runner}")


if __name__ == "__main__":
    main()
