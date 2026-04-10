# Fine-Tune Toolkit

End-to-end local workflow for:
- collecting raw logs from Claude + Anvil
- exporting successful tool-call behavior from agentMemory
- preparing/blending datasets
- running SFT LoRA training
- building RL-style scored/preference datasets
- exporting GGUF artifacts for LM Studio / llama.cpp

## Notebook-first tutorial

Primary walkthrough notebook:
- `notebooks/fine_tune_blender_tutorial.ipynb`

Secondary coaching notebook:
- `notebooks/fine_tune_coach.ipynb`

The tutorial notebook is designed to show outputs at each step (extract, format, blend, score, pair generation).

## Dependency isolation (required)

Fine-tune dependencies are optional and intentionally separate from core `agentMemory`.

```bash
bash fine-tune/install_finetune_env.sh
source .venv-finetune/bin/activate
```

Core app can run without these deps. Only fine-tune scripts require this env.

## Folder contract

- Raw input only: `data/raw/`
- Processed train/val: `data/processed/`
- Base/adapter/merged/GGUF models: `models/`
- Training logs and outputs: `fine-tune/outputs/` and `logs/`

## API keys and auth

Hugging Face token env name supported by scripts:

```bash
export HUGGING_FACE_API=hf_xxx
# optional fallback:
export HF_TOKEN=hf_xxx
```

`fine-tune/download_models.py` checks `HUGGING_FACE_API` first.

## 1) Collect raw logs into `data/raw/`

```bash
bash fine-tune/collect_raw_data.sh
```

What this does:
- copies all Claude JSONL logs from `~/.claude/projects` into `data/raw/claude/`
- copies Anvil session JSON from `.anvil/sessions` into `data/raw/anvil/`

## 2) Build datasets from Claude logs (all projects)

```bash
./.venv-finetune/bin/python fine-tune/prepare_from_claude_dir.py \
  --input-dir data/raw/claude \
  --output-dir data/processed/claude_all
```

If you want a focused slice (for example `fire-map` and `DailyDispatch`), create a filtered folder under `data/raw/claude/` and run this script on that folder.

## 3) Export successful tool-call datasets from agentMemory DB

SFT export:

```bash
./.venv-finetune/bin/python fine-tune/export_from_agent_memory.py \
  --dataset-type sft \
  --project /Users/mz/Dropbox/_CODING/agentMemory \
  --limit 12000 \
  --output-dir data/raw/agent_memory
```

Trajectory export:

```bash
./.venv-finetune/bin/python fine-tune/export_from_agent_memory.py \
  --dataset-type trajectory \
  --project /Users/mz/Dropbox/_CODING/agentMemory \
  --limit 12000 \
  --output-dir data/raw/agent_memory
```

Preference export:

```bash
./.venv-finetune/bin/python fine-tune/export_from_agent_memory.py \
  --dataset-type preference \
  --project /Users/mz/Dropbox/_CODING/agentMemory \
  --limit 12000 \
  --output-dir data/raw/agent_memory
```

## 4) Convert exports/logs to trainable JSONL

From agentMemory export:

```bash
./.venv-finetune/bin/python fine-tune/prepare_jsonl.py \
  --input data/raw/agent_memory/<export_file>.jsonl \
  --input-format agent_memory \
  --output-dir data/processed/fine_tune_agent_memory
```

From Anvil session:

```bash
./.venv-finetune/bin/python fine-tune/prepare_jsonl.py \
  --input data/raw/anvil/<session>.json \
  --input-format anvil_session \
  --output-dir data/processed/fine_tune_anvil
```

## 5) Blend datasets with explicit weighting

Example blend favoring successful tool-calls plus Claude conversation coverage:

```bash
./.venv-finetune/bin/python fine-tune/blend_chat_datasets.py \
  --source data/processed/fine_tune_agent_memory/train.chat.jsonl:3 \
  --source data/processed/claude_all/train.chat.jsonl:1 \
  --source data/processed/fine_tune_anvil/train.chat.jsonl:1 \
  --output-dir data/processed/fine_tune_blend
```

Outputs:
- `train.chat.jsonl`
- `valid.chat.jsonl`
- instruction/response versions
- `stats.json`

## 6) Training paths

### Path A: MLX tune (16GB Mac starter)

Dry run:

```bash
./.venv-finetune/bin/python fine-tune/train_mlx_tune.py
```

Run:

```bash
./.venv-finetune/bin/python fine-tune/train_mlx_tune.py --run
```

### Path B: GGUF-compatible HF/PEFT LoRA (recommended for LM Studio/anvil/llama.cpp)

Use `fine-tune/gguf/README.md` for full commands.

Current recommended base path in this repo:
- `models/base/qwen3.5-9b-hf`

## 7) RL-style continuous improvement loop

Build scored episodes from multi-step tool traces:

```bash
./.venv-finetune/bin/python fine-tune/build_success_trajectories.py \
  --project /Users/mz/Dropbox/_CODING/agentMemory \
  --successful-only \
  --profile fine-tune/rl_reward_profile.json
```

Create preference pairs:

```bash
./.venv-finetune/bin/python fine-tune/continuous_rl_loop.py \
  --episodes data/processed/rl/<latest_scored_file>.jsonl
```

Scoring definitions:
- `fine-tune/qa_docs_plan_rubric.md`
- `fine-tune/rl_reward_profile.json`

## 8) Generate custom scripts from natural language

```bash
./.venv-finetune/bin/python fine-tune/generate_training_script.py \
  --request "write a Python script using mlx-tune to fine-tune Gemma 4 E4B on a 16GB Mac" \
  --output fine-tune/generated/train_gemma_16gb.py
```

## 9) Debug local failures

```bash
./.venv-finetune/bin/python fine-tune/debug_training_log.py \
  --log fine-tune/outputs/train.log
```

Detects common classes:
- memory/OOM
- dependency/import
- auth/token
- path/file-not-found
- network/SSL

## 10) Suggested iteration cadence

1. Run small pilot on latest blend.
2. Validate outputs manually in llama.cpp or LM Studio.
3. Score production sessions and regenerate preference pairs.
4. Reblend with higher weight on recent successful trajectories.
5. Train next adapter and compare on fixed eval prompts.

## Practical defaults (16GB baseline)

- `batch_size=1`
- `max_length=1024`
- `lora_rank=8` for first pass
- short pilot epochs before full run
