# Fine-Tune Pipeline Runbook

End-to-end procedure for fine-tuning a Qwen 2.5 / Qwen 3 / similar instruct
model on tool-call data and shipping a GGUF that works in LM Studio.

This is the runbook the next session should follow. Phase-gated. Each gate
has an objective pass criterion — no eyeballing.

## Canonical layout

```
models/base/<slug>/                       HF base snapshot + REVISION.txt
models/lora/<slug>-toolcalls-lora/        runs/<UTC>/  +  latest -> runs/<UTC>
models/merged/<slug>-toolcalls-merged/    merged HF
models/gguf/<slug>-toolcalls-{f16,q4km}.gguf  +  .sha256 sidecar
data/processed/qwen25_tools/v1/           restructured dataset + MANIFEST
logs/m-ft-1/                              every phase logs here
docs/training_runs/                       per-run report on Phase 6
```

Model slugs and HF repos are in `scripts/fine_tune/lib.py:MODELS`. Add a new
entry there to bring a new model under the runbook.

## Phase 0 — Cleanup & pre-flight

```bash
# Confirm Dropbox free space ≥ 60 GB before training a 3B (more for 7B+)
df -h ~/Dropbox
# Quit Dropbox to prevent sync-during-training corruption
osascript -e 'tell application "Dropbox" to quit'
```

**Gate:** ≥60 GB free; no Dropbox process.

## Phase 1 — Base model

```bash
.venv-finetune/bin/python scripts/fine_tune/download_base.py qwen2.5-3b-instruct
.venv-finetune/bin/python scripts/fine_tune/smoke_test_base.py qwen2.5-3b-instruct
```

**Gate:** smoke_test prints `PASS` and a non-empty generation.

## Phase 2 — Dataset restructure

```bash
.venv-finetune/bin/python fine-tune/restructure_to_qwen_tools.py
```

**Gate:** script exits 0; `MANIFEST.json` shows `rejection_rate ≤ 0.05`.

## Phase 3 — Training script

The script `models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py` is
reusable across models: override `MODEL_SLUG`, `DATASET_TIER`, `RUN_TAG`,
`EPOCHS`, etc. via env vars.

## Phase 4 — TINY end-to-end (integration gate)

```bash
DATASET_TIER=tiny RUN_TAG=tiny-smoke \
  .venv-finetune/bin/python -u models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py

.venv-finetune/bin/python fine-tune/gguf/merge_lora_hf.py \
  --base-model models/base/qwen2.5-3b-instruct \
  --lora-adapter models/lora/qwen2.5-3b-instruct-toolcalls-lora/latest \
  --output-dir models/merged/qwen2.5-3b-toolcalls-merged

.venv-finetune/bin/python fine-tune/gguf/convert_to_gguf.py \
  --llama-cpp-dir models/llama.cpp \
  --hf-model-dir models/merged/qwen2.5-3b-toolcalls-merged \
  --out-f16 models/gguf/qwen2.5-3b-toolcalls-f16.gguf \
  --out-quant models/gguf/qwen2.5-3b-toolcalls-q4km.gguf \
  --quant Q4_K_M --run

shasum -a 256 models/gguf/qwen2.5-3b-toolcalls-q4km.gguf \
  | tee models/gguf/qwen2.5-3b-toolcalls-q4km.gguf.sha256

.venv-finetune/bin/python -u scripts/fine_tune/validate_tool_calls.py \
  --backend llama-cli \
  --gguf models/gguf/qwen2.5-3b-toolcalls-q4km.gguf \
  --min-parse-rate 0.03
```

**Gate:** validator reports PASS (≥3% of trials produce parseable, schema-valid `<tool_call>` blocks).

## Phase 5 — Full training

Same training command with `DATASET_TIER=full RUN_TAG=full-v1`. Repeat
merge → GGUF → validator with `--min-parse-rate 0.8`.

**Gate:** validator PASS at ≥80%.

## Phase 6 — Observability

Write `docs/training_runs/M-FT-1-<UTC>.md` with: final loss, eval loss,
validator pass rate by suite, GGUF SHAs, run wall-clock.

## Phase 7 — LM Studio

```bash
scripts/fine_tune/lmstudio_smoke.sh \
  models/gguf/qwen2.5-3b-toolcalls-q4km.gguf 0.5
```

The script copies the GGUF into LM Studio's models dir, then waits for you
to load it + start the OpenAI server, then runs the validator against
`localhost:1234/v1`.

**Gate:** validator PASS with the openai backend; bonus pass if
`native_tool_calls > 0` (proves LM Studio parsed Hermes wire format).

## Recovery

If any gate fails:
- Check `logs/m-ft-1/` for the matching phase log.
- The `latest` symlink in the LoRA dir only updates on successful run
  completion — if training crashes, the previous good adapter is still there.
- See `docs/fine_tune/FAILURE_MODES.md` for known issues + fixes.
