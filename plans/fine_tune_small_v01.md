# Fine-Tune Plan: Small Model Proof of Concept (v0.1)

**Goal:** Train a small (1.5B) model on agent-memory data to prove the pipeline works and the model learns real project knowledge, before scaling to 9B.

**Date:** 2026-05-12
**Status:** planned

---

## Why Start Small

- Qwen 2.5-1.5B trains in minutes, not hours — fast iteration on data quality
- GGUF output is ~1GB — trivial to test in llama.cpp or LM Studio
- Proves the full pipeline end-to-end before committing to 9B
- If a 1.5B model can answer "what tools were used in the fire-map project?" from memory, the approach is validated
- Catches data quality issues (bad formatting, noisy rows, hallucination sources) cheaply

## Data Source

**Primary:** 68,985 observations in agent-memory PostgreSQL (localhost:3377)

**Export pipeline (already built):**
1. `fine-tune/export_from_agent_memory.py` — queries DB, produces SFT/trajectory/preference datasets
2. `fine-tune/prepare_jsonl.py` — converts to chat JSONL format
3. `fine-tune/blend_chat_datasets.py` — weighted blending + deduplication + validation split

**Existing blend dataset:** `data/processed/fine_tune_blend/` — 16,117 train + 849 valid rows

**Dataset types available:**
- **SFT** — prompt → tool call pairs with context
- **Trajectory** — multi-step tool call sequences with outcome rewards
- **Preference (DPO)** — chosen/rejected pairs based on reward scores

**Reward scoring (built in):**
- Base: +1.0 success, -1.0 failure
- Bonuses: +0.25 observation linked, +0.10 session completed
- Penalties: -0.10 no observation, -0.25 tool_error, -0.25 problematic pattern

## Model Configuration

| Setting | Value |
|---------|-------|
| Base model | `Qwen/Qwen2.5-1.5B-Instruct` |
| Training rows | 3,000 (SFT format) |
| Validation rows | 150 (5% split) |
| LoRA rank | r=8, alpha=16, dropout=0.05 |
| Target modules | q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj |
| Epochs | 2 |
| Batch size | 1 (per device) |
| Gradient accumulation | 4 (effective batch = 4) |
| Learning rate | 2e-4 |
| Max sequence length | 1024 |
| Precision | float32 (MPS doesn't support fp16 well) |
| Hardware | 64GB Apple Silicon Mac, MPS backend |
| Estimated train time | 5-10 minutes |
| GGUF output size | ~1GB (Q4_K_M) |

## Framework

**HF Transformers + PEFT** (already set up, proven working with Qwen 9B pilot)

Not using Unsloth or HuggingFace Hub — the existing local pipeline is proven and avoids new dependencies. Can revisit Unsloth for 2x speedup on the 9B run if needed.

## Execution Steps

### Step 1: Fresh Data Export (5 min)

```bash
cd ~/_CODING/agentMemory

# Export fresh SFT dataset from the full 68k observation DB
.venv/bin/python fine-tune/export_from_agent_memory.py \
  --dataset-type sft \
  --include-observations \
  --limit 5000 \
  --format jsonl

# Prepare JSONL in chat format
.venv/bin/python fine-tune/prepare_jsonl.py \
  --input data/raw/agent_memory/ \
  --output data/processed/fine_tune_small_v01/ \
  --format chat

# Blend with Claude session data (lower weight for diversity)
.venv/bin/python fine-tune/blend_chat_datasets.py \
  --sources "data/processed/fine_tune_small_v01/:3,data/processed/claude_all/:1" \
  --output data/processed/fine_tune_blend_small/ \
  --max-samples 3000 \
  --valid-pct 0.05
```

### Step 2: Download Base Model (2 min)

```bash
# Download Qwen 2.5-1.5B-Instruct (if not cached)
.venv-finetune/bin/python -c "
from transformers import AutoModelForCausalLM, AutoTokenizer
AutoTokenizer.from_pretrained('Qwen/Qwen2.5-1.5B-Instruct')
AutoModelForCausalLM.from_pretrained('Qwen/Qwen2.5-1.5B-Instruct')
print('Downloaded successfully')
"
```

### Step 3: Generate & Run Training Script (5-10 min)

```bash
# Generate training script
.venv-finetune/bin/python fine-tune/gguf/train_lora_hf.py \
  --base-model Qwen/Qwen2.5-1.5B-Instruct \
  --adapter-name qwen1.5b-agentmem-v01 \
  --dataset data/processed/fine_tune_blend_small/train.chat.jsonl \
  --valid-dataset data/processed/fine_tune_blend_small/valid.chat.jsonl \
  --max-train-samples 3000 \
  --max-valid-samples 150 \
  --epochs 2 \
  --max-length 1024 \
  --lora-r 8 \
  --lora-alpha 16 \
  --grad-accum 4 \
  --lr 2e-4

# Run training
.venv-finetune/bin/python models/lora/qwen1.5b-agentmem-v01/run_train_lora.py
```

### Step 4: Merge + GGUF (2 min)

```bash
# Merge LoRA adapter into base
.venv-finetune/bin/python fine-tune/gguf/merge_lora_hf.py \
  --base-model Qwen/Qwen2.5-1.5B-Instruct \
  --lora-adapter models/lora/qwen1.5b-agentmem-v01 \
  --output-dir models/merged/qwen1.5b-agentmem-v01

# Convert to GGUF + quantize
.venv-finetune/bin/python fine-tune/gguf/convert_to_gguf.py \
  --llama-cpp-dir models/llama.cpp \
  --hf-model-dir models/merged/qwen1.5b-agentmem-v01 \
  --out-f16 models/gguf/qwen1.5b-agentmem-v01-f16.gguf \
  --out-quant models/gguf/qwen1.5b-agentmem-v01-q4km.gguf \
  --quant Q4_K_M \
  --run
```

### Step 5: Evaluation (5-10 min)

See Verification Plan below.

## Verification Plan

### 1. Fixed Eval Prompts

Create `fine-tune/eval_prompts.jsonl`:

```jsonl
{"prompt": "What tools are available for file editing in Anvil?", "expected_keywords": ["edit_file", "write_file", "read_file", "bash_run"]}
{"prompt": "What are the common patterns in the fire-map project?", "expected_keywords": ["ETL", "GeoServer", "PostGIS", "FIRMS", "fire_events"]}
{"prompt": "What bugs were found related to authentication?", "expected_keywords": ["JWT", "token", "session", "cookie"]}
{"prompt": "How should database migrations be handled in agent-memory?", "expected_keywords": ["migration", "schema", "ALTER", "scripts/migrations"]}
{"prompt": "What lessons were learned about git operations?", "expected_keywords": ["git init", "home directory", ".git", "commit"]}
{"prompt": "What is the agent-memory observation pipeline?", "expected_keywords": ["queue", "embedding", "observation", "tool_call", "nomic"]}
{"prompt": "How does the Anvil agent runner work?", "expected_keywords": ["iteration", "tool", "LLM", "runner", "middleware"]}
{"prompt": "What projects have been worked on?", "expected_keywords": ["anvil", "fire-map", "agentMemory", "agent-memory"]}
{"prompt": "What are the most common tool call failures?", "expected_keywords": ["permission", "not found", "timeout", "error"]}
{"prompt": "How is search implemented in agent-memory?", "expected_keywords": ["hybrid", "vector", "FTS", "ILIKE", "RRF", "pgvector"]}
```

### 2. A/B Comparison Script

Create `fine-tune/eval_compare.py`:

```
For each eval prompt:
  1. Run through base Qwen 1.5B → capture response
  2. Run through fine-tuned Qwen 1.5B → capture response
  3. Score each on:
     - Keyword hit rate (how many expected_keywords appear)
     - Specificity (does it mention real project names, file paths, tools?)
     - Hallucination check (does it invent fake observations?)
  4. Print side-by-side comparison
```

### 3. Quantitative Metrics

| Metric | How to measure | Pass criteria |
|--------|---------------|---------------|
| Validation loss | Training logs | < base model perplexity |
| Keyword hit rate | eval_compare.py | Fine-tuned > 2x base on avg |
| Specificity | Manual review | References real projects/tools |
| Hallucination rate | Manual review | < 20% of responses contain fabricated info |
| GGUF load test | llama.cpp -p "READY" | Loads without tensor errors |

### 4. Practical Smoke Test

```bash
# Load in llama.cpp and ask it questions
./models/llama.cpp/build/bin/llama-cli \
  -m models/gguf/qwen1.5b-agentmem-v01-q4km.gguf \
  -n 256 \
  -p "What tools does the Anvil agent use for file operations?"

# Or load in LM Studio and chat interactively
```

**Success criteria:** The fine-tuned model mentions specific tools (edit_file, bash_run, grep_search) and projects (anvil, fire-map) that only exist in the training data. The base model will give generic answers about "common file tools."

## Scale-Up Path

Once v0.1 proves the approach:

| Phase | Model | Data | Time |
|-------|-------|------|------|
| v0.1 (this plan) | Qwen 2.5-1.5B | 3k rows SFT | ~10 min |
| v0.2 | Qwen 2.5-1.5B | 3k rows + DPO preference pairs | ~15 min |
| v1.0 | Qwen 3.5-9B | 16k rows SFT | ~2 hours |
| v1.1 | Qwen 3.5-9B | 16k SFT + DPO | ~3 hours |

## What Makes This Data Special

This isn't generic instruction tuning — the dataset contains:

- **Real tool call trajectories** with success/failure labels and reward scores
- **Cross-project engineering knowledge** spanning fire-map, anvil, agent-memory, ETrade, and more
- **Lessons learned** — actual bugfixes, gotchas, and architectural decisions
- **Project-specific patterns** — how tools are used in context, not in isolation
- **Temporal context** — what was tried, what failed, what eventually worked

The model is learning *your* engineering judgment, not generic coding patterns.

## Files Created/Modified

- `plans/fine_tune_small_v01.md` — this plan
- `fine-tune/eval_prompts.jsonl` — fixed evaluation prompts (to create)
- `fine-tune/eval_compare.py` — A/B comparison script (to create)
- `data/processed/fine_tune_blend_small/` — training data output (to generate)
- `models/lora/qwen1.5b-agentmem-v01/` — LoRA adapter output
- `models/gguf/qwen1.5b-agentmem-v01-q4km.gguf` — final GGUF

## Known Risks & Prior Failures

- **Qwen 3.x fine-tuning failed previously** — architecture changes (attention, chat template, tool call schema) broke LoRA training scripts that worked on Qwen 2.5. This plan deliberately uses **Qwen 2.5-1.5B-Instruct** as the proven-safe path.
- **Gemma 4 GGUF has tensor mismatch** — do not use Gemma for this round.
- **Multimodal (vision + tools) is a separate concern** — v0.1 is text-only tool calling. Vision-capable models (Qwen-VL) require different data formats and layer freezing strategies. Defer to v2+.
- **Qwen 3.x as stretch goal** — once v0.1 works on 2.5, try Qwen 3.x as v0.3 to identify what specifically breaks and fix it with a known-good baseline to compare against.

## GitHub Issue

Tracked at: metazen11/agent-memory#15
