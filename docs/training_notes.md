# Fine-Tune / Training Notes

## Gemma 4 Path

- LoRA pilot training completed and merged, GGUF generated.
- Gemma 4 GGUF currently has runtime tensor mismatch in llama.cpp (`missing tensor ...`), so this path is not the current recommended test model.

## Qwen 9B Path (recommended, current)

- Official HF base downloaded locally into:
  - `models/base/qwen3.5-9b-hf`
- Local training script uses this base:
  - `models/lora/qwen3.5-9b-toolcalls-lora/run_train_lora.py`
- Clean pilot fine-tune completed:
  - adapter: `models/lora/qwen3.5-9b-toolcalls-lora/adapter_model.safetensors`
- Merge completed:
  - merged model: `models/merged/qwen3.5-9b-toolcalls-merged/model.safetensors`
- GGUF conversion + quantization completed:
  - `models/gguf/qwen3.5-9b-toolcalls-f16.gguf`
  - `models/gguf/qwen3.5-9b-toolcalls-q4km.gguf`
- llama.cpp load/generation test runs successfully (no tensor-missing load error on this Qwen path).
- Note: `models/` may be a symlink to Dropbox cold storage — only needed for fine-tuning, not runtime.

## Important Logs

- Qwen pilot train: `logs/train_qwen9b_hf_pilot_20260409_204845.log`
- Qwen merge: `logs/merge_qwen9b_hf_20260409_204913.log`
- Qwen GGUF convert/quant: `logs/gguf_convert_qwen9b_20260409_204946.log`

(Older logs remain for Gemma and earlier attempts.)

## Data Pipeline

- Raw data folder contract is in place:
  - `data/raw/` (Claude + Anvil collected)
- Processed blend dataset exists:
  - `data/processed/fine_tune_blend/train.chat.jsonl`
  - `data/processed/fine_tune_blend/valid.chat.jsonl`

## Training Commands

### Start Next Qwen Training Iteration

```bash
set -a; source .env
export FAST_PILOT=1
export PILOT_MAX_TRAIN_SAMPLES=1200
export PILOT_MAX_VALID_SAMPLES=120
export PILOT_EPOCHS=0.5
export PILOT_MAX_LENGTH=1024
export PILOT_GRAD_ACCUM=4
export PILOT_LOGGING_STEPS=10
export PILOT_EVAL_STRATEGY=steps
export PILOT_EVAL_STEPS=100
export PILOT_SAVE_STRATEGY=steps
export PILOT_SAVE_STEPS=100
set +a
./.venv-finetune/bin/python models/lora/qwen3.5-9b-toolcalls-lora/run_train_lora.py
```

### Merge + GGUF

```bash
./.venv-finetune/bin/python fine-tune/gguf/merge_lora_hf.py \
  --base-model models/base/qwen3.5-9b-hf \
  --lora-adapter models/lora/qwen3.5-9b-toolcalls-lora \
  --output-dir models/merged/qwen3.5-9b-toolcalls-merged

./.venv-finetune/bin/python fine-tune/gguf/convert_to_gguf.py \
  --llama-cpp-dir models/llama.cpp \
  --hf-model-dir models/merged/qwen3.5-9b-toolcalls-merged \
  --out-f16 models/gguf/qwen3.5-9b-toolcalls-f16.gguf \
  --out-quant models/gguf/qwen3.5-9b-toolcalls-q4km.gguf \
  --quant Q4_K_M \
  --run
```

### Quick Verify

```bash
# Confirm model artifacts
ls -lh models/gguf/qwen3.5-9b-toolcalls-*.gguf

# Quick llama.cpp test
./models/llama.cpp/build/bin/llama-cli -m models/gguf/qwen3.5-9b-toolcalls-q4km.gguf -n 64 -p "Reply with READY only."
```

## Warnings

- Avoid using the local LM Studio MLX-export Qwen copy as base for training; it produced key mismatch warnings (`UNEXPECTED`/`MISSING`) during load.
- Use `models/base/qwen3.5-9b-hf` as the training base.
