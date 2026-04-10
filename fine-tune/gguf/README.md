# GGUF-Compatible Fine-Tune Path (Anvil + LM Studio + llama.cpp)

This workflow produces:
- HF LoRA adapter
- merged HF model
- GGUF `f16` and quantized (`Q4_K_M`) artifacts

Use optional fine-tune env:

```bash
bash fine-tune/install_finetune_env.sh
source .venv-finetune/bin/activate
```

## Recommended current path (Qwen 9B)

Base model directory:
- `models/base/qwen3.5-9b-hf`

Training data:
- `data/processed/fine_tune_blend/train.chat.jsonl`
- `data/processed/fine_tune_blend/valid.chat.jsonl`

### 1) Generate trainer script

```bash
./.venv-finetune/bin/python fine-tune/gguf/train_lora_hf.py \
  --base-model models/base/qwen3.5-9b-hf \
  --train-file data/processed/fine_tune_blend/train.chat.jsonl \
  --valid-file data/processed/fine_tune_blend/valid.chat.jsonl \
  --output-dir models/lora/qwen3.5-9b-toolcalls-lora
```

### 2) Run trainer

```bash
./.venv-finetune/bin/python models/lora/qwen3.5-9b-toolcalls-lora/run_train_lora.py
```

Pilot mode (fast smoke test):

```bash
export FAST_PILOT=1
export PILOT_MAX_TRAIN_SAMPLES=120
export PILOT_MAX_VALID_SAMPLES=24
export PILOT_EPOCHS=0.1
./.venv-finetune/bin/python models/lora/qwen3.5-9b-toolcalls-lora/run_train_lora.py
```

### 3) Merge LoRA into base

```bash
./.venv-finetune/bin/python fine-tune/gguf/merge_lora_hf.py \
  --base-model models/base/qwen3.5-9b-hf \
  --lora-adapter models/lora/qwen3.5-9b-toolcalls-lora \
  --output-dir models/merged/qwen3.5-9b-toolcalls-merged
```

### 4) Convert + quantize to GGUF

```bash
./.venv-finetune/bin/python fine-tune/gguf/convert_to_gguf.py \
  --llama-cpp-dir models/llama.cpp \
  --hf-model-dir models/merged/qwen3.5-9b-toolcalls-merged \
  --out-f16 models/gguf/qwen3.5-9b-toolcalls-f16.gguf \
  --out-quant models/gguf/qwen3.5-9b-toolcalls-q4km.gguf \
  --quant Q4_K_M \
  --run
```

### 5) Test in llama.cpp

```bash
./models/llama.cpp/build/bin/llama-cli \
  -m models/gguf/qwen3.5-9b-toolcalls-q4km.gguf \
  -n 64 \
  -p "Reply with READY only."
```

Load this same `q4km.gguf` in LM Studio or Anvil GGUF runtime.

## Known caveat

Gemma 4 pilot artifacts were produced, but current Gemma path has runtime tensor mismatch in llama.cpp for this repo setup. Prefer the Qwen 9B path above for now.
