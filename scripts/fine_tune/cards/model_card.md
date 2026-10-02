---
license: other
base_model: Qwen/Qwen3-4B
library_name: peft
tags:
  - lora
  - tool-calling
  - qwen3
  - agent-memory
  - internal
pipeline_tag: text-generation
---

# qwen3-4b-toolcalls-v5-pilot (PRIVATE / INTERNAL)

> **INTERNAL — DO NOT MAKE PUBLIC.** This repo is private by policy. The
> training data contains sensitive internal references. Keep this repo and any
> derived artifacts private.

## What this is

A **LoRA adapter** for [`Qwen/Qwen3-4B`](https://huggingface.co/Qwen/Qwen3-4B),
fine-tuned to emit well-formed **tool calls** (Qwen native `<tool_call>` format)
for the agent-memory assistant. It is the **v5 pilot** — a cheap end-to-end
proof of the Hugging Face Jobs training pipeline (5000-row dataset, 1 epoch on a
single L4 GPU).

This is a PEFT adapter, **not** a merged model. You load `Qwen/Qwen3-4B` and
apply this adapter on top.

## Intended use

- Given a user request + a set of tool definitions, produce a correct tool call
  (name + JSON arguments) instead of prose.
- Target harness: the agent-memory tool-call assistant. Not a general chat model.

## How to load (PEFT adapter)

```python
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

base = "Qwen/Qwen3-4B"
adapter = "WFCA/qwen3-4b-toolcalls-v5-pilot"

tok = AutoTokenizer.from_pretrained(base)
model = AutoModelForCausalLM.from_pretrained(base, torch_dtype="bfloat16", device_map="auto")
model = PeftModel.from_pretrained(model, adapter)

messages = [{"role": "user", "content": "list the files in the current directory"}]
tools = [ ... ]  # your tool schema list
prompt = tok.apply_chat_template(messages, tools=tools, tokenize=False, add_generation_prompt=True)
# generate -> expect a <tool_call>{"name": "Bash", "arguments": {...}}</tool_call>
```

## Local GGUF path (for LM Studio / llama.cpp)

To run locally, merge the adapter into the base and convert to GGUF:

1. `hf download WFCA/qwen3-4b-toolcalls-v5-pilot --local-dir <dir>`
2. `scripts/fine_tune/merge_checkpoint.py` — merges adapter → base, produces a
   Q6_K GGUF.
3. `scripts/fine_tune/verify_gguf.py` + `lmstudio_smoke.sh` — validate tool calls.

See `docs/runbooks/pilot-4b-reboot-checklist.md` in the agent-memory repo.

## Training

| | |
|---|---|
| Base model | `Qwen/Qwen3-4B` (4.02B, qwen3 arch, native tool template) |
| Method | LoRA (r=16, alpha=32, dropout=0.05) on all attention + MLP projections |
| Dataset | `WFCA/agentmem-toolcalls-v5-pilot` (private, 5000 rows) |
| Epochs / LR | 1.0 / 2e-4, cosine, warmup 0.05, weight decay 0.01 |
| Max length | 4096 |
| Hardware | 1× L4 24GB (HF Jobs, `l4x1`) |
| Label masking | assistant-only (span-walk, deterministic `-100` labels) |

Trained via `hf jobs uv run scripts/fine_tune/train_hf_job.py` on Hugging Face
Jobs. Loss is computed only on assistant tool-call tokens (assistant-only
masking) so the model learns to *produce* tool calls, not to echo the prompt.

## Limitations

- Pilot scale (5000 rows, 1 epoch) — a capability demonstration, not a
  production model.
- Tool-calling only; not tuned for open-ended chat.
- Private/internal training data — outputs may reflect internal project
  vocabulary. Do not distribute.
