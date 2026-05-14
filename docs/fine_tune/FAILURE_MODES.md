# Fine-Tune Pipeline — Failure Modes & Fixes

Every failure we've hit, the symptom, the root cause, and the fix. Add to
this file every time something breaks so the next session doesn't relearn.

## 1. `ModuleNotFoundError: No module named 'lib'` at training start

**Symptom:** training script crashes immediately on `from lib import ...`.

**Root cause:** `models/` is a symlink to `~/Dropbox/_CODING/agentMemory/models/`.
Using `Path(__file__).resolve()` chases the symlink into Dropbox, where the
working-copy `scripts/fine_tune/lib.py` may not have synced yet.

**Fix:** use `Path(__file__).absolute()` (no symlink chase). Working tree
files live in `~/_CODING/agentMemory/`; symlinked artifacts live in Dropbox.

Codified in `models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py`.

## 2. Dataset rejection rate > 5% at restructure

**Symptom:** `fine-tune/restructure_to_qwen_tools.py` exits non-zero with
"rejection rate exceeded".

**Root cause:** schema inference's `SCHEMA_REQUIRED_PCT` (default 0.95) is
too tight or loose for your data. Fields present in ~80-94% of rows
straddle the boundary.

**Fix:** raise `SCHEMA_REQUIRED_PCT` to 0.95 (current default — covers
Bash.description at 82%, Grep.output_mode at 92%). Lower only if you want
stricter required-field constraints.

## 3. llama-cli hangs forever (no output)

**Symptom:** `llama-cli -m ... -p "..." -n 8` produces a "> > >" prompt
loop and never exits.

**Root cause:** default mode is conversational; `-no-cnv` alone isn't enough.

**Fix:** use `-st` (single-turn) and pipe `</dev/null` to close stdin. The
process exits after one generation:
```bash
llama-cli ... -st --no-warmup </dev/null
```

## 4. Validator hangs for tens of minutes with no output

**Symptom:** `validate_tool_calls.py | tee log.txt` produces no log output
for 5+ min even though child processes are running.

**Root cause:** Python stdout is fully buffered when stdout is a pipe (vs
a TTY). `tee` doesn't see anything until process exit.

**Fix:** invoke Python with `-u` (unbuffered) flag, or set `PYTHONUNBUFFERED=1`.

## 5. Generation too slow (≥10 s per call)

**Symptom:** validator runs at < 0.5 gens/s.

**Root cause:** too many tool schemas in the system prompt. Each generation
re-prefills the entire `<tools>` block.

**Fix:** trim to ≤ 5 most-common tools for validation (the model only needs
to choose from a small set per prompt anyway). See
`DEFAULT_TRAINED_TOOLS` in `validate_tool_calls.py`.

## 6. Broken Qwen 3.5 9B GGUF fails in LM Studio

**Symptom:** GGUF loads but generation breaks in LM Studio.

**Root cause:** the artifact downloaded under name "qwen3.5-9b-hf" was a
hybrid model with `ssm_*` (Mamba SSM) tensors. Stock llama.cpp builds
don't support that architecture.

**Fix:** use a pure-transformer base. Canonical now is Qwen 2.5 Instruct
(3B or 7B). Verify before training by listing tensor names in
`config.json` — no `ssm_*`, no exotic prefixes.

## 7. PII leakage into model weights

**Symptom:** generated tool calls contain real tokens, API keys, or
specific home-directory paths.

**Root cause:** training data was sourced from real Claude transcripts
without scrubbing.

**Fix:** `restructure_to_qwen_tools.py` applies regex scrub for:
- `sk-[A-Za-z0-9_-]{20,}`
- `Bearer \S+`
- `AGENT_MEMORY_TOKEN[=:\s]*\S+`
- `xoxb-\S+`, `ghp_[A-Za-z0-9]{36,}`
- Path rewrite `/Users/mz/` → `/Users/<user>/`

Counts recorded in `MANIFEST.json:pii_substitutions`. Failure = unscrubbed
key in the dataset → model memorizes it.

## 8. Full training crashes during first eval (~step 250)

**Symptom:** training proceeds normally to step 250 (first eval at `eval_steps=250`), the
eval phase starts but progressively slows from ~7s/batch to 25s+/batch, then the
Python process is killed (semaphore leak warning at shutdown).

**Root cause:** HF `TrainingArguments`'s default `per_device_eval_batch_size=8`. With
`MAX_LENGTH=1024` on a Qwen2.5-3B on MPS, eval batches of 8 exhaust memory and
swap-thrash. Train batches survive because they default to 1.

**Fix:** pin `per_device_eval_batch_size=1` in TrainingArguments. Also useful:
- raise `eval_steps` to 500 (less frequent eval),
- lower `save_steps` below `eval_steps` (250) so a successful save happens before
  the fragile eval,
- pass `resume_from_checkpoint=True` to `trainer.train()` so a crash mid-eval
  can resume from the last checkpoint instead of restarting at step 0.

All three fixes are now in
`models/lora/qwen2.5-3b-toolcalls-lora/run_train_lora.py`.

## 10. GGUF copied to LM Studio but doesn't appear in My Models

**Symptom:** You copy your GGUF into `~/.cache/lm-studio/models/<name>/`,
open LM Studio, and your model isn't in the My Models tab.

**Root cause:** Current LM Studio scans `~/.lmstudio/models/`, NOT
`~/.cache/lm-studio/`. The cache path was the old layout and is silently
ignored.

**Fix:** copy to `~/.lmstudio/models/<publisher>/<repo>/<file>.gguf`. The
`<publisher>` segment can be any name; convention is `mz` (or your username)
for locally-trained models. Restart LM Studio if the model still doesn't
appear after the copy.

```bash
mkdir -p ~/.lmstudio/models/mz/qwen25-toolcalls
cp models/gguf/qwen2.5-3b-toolcalls-q4km.gguf ~/.lmstudio/models/mz/qwen25-toolcalls/
```

The fixed `scripts/fine_tune/lmstudio_smoke.sh` uses the correct path.

## 9. eval_loss is NaN but train_loss is finite

**Symptom:** During full training, eval_loss prints as `nan` while train_loss
keeps descending normally. Training does NOT halt; subsequent steps continue.

**Root cause:** bfloat16 on MPS occasionally overflows attention softmax for a
single long sequence in the validation set. The NaN poisons the per-batch
reduce for *that eval pass* but doesn't reach the model weights. Train batches
are smaller per gradient step (batch=1, GRAD_ACCUM=4) and don't hit the issue.

**Severity:** Cosmetic. The model still learns. Validator at the end is the
real check.

**Fix (optional):** Upcast eval to fp32. In TrainingArguments, the cheapest
patch is to wrap eval in `torch.autocast(dtype=torch.float32)`, but that
requires a custom Trainer subclass. The simpler option is to skip eval
entirely (`eval_strategy="no"`) and rely on the post-training validator.

## 10. Output paths reference wrong slug

**Symptom:** training writes to `models/lora/<wrong-slug>-toolcalls-lora/`.

**Root cause:** `MODEL_SLUG` env var overrides the hard-coded slug in the
training script. Whatever you set MODEL_SLUG to becomes the path.

**Fix:** either set `MODEL_SLUG` explicitly or accept the default. The
default in the current script is `qwen2.5-3b-instruct`, which produces
`models/lora/qwen2.5-3b-instruct-toolcalls-lora/`. (Note the `-instruct-`
in the middle — that's the full slug, not a typo.)

## 11. Empty-args infinite loop in LM Studio

**Symptom:** Vague natural prompts ("find the fire-map codebase") cause the
model to emit `<tool_call>` blocks with empty `arguments`, get a generic
tool-result back, then emit the same empty-args call again. Loop continues
until the context window fills.

**Root cause:** v1 dataset was 83 % synthetic prompts of the form
`"Call tool 'X' with appropriate arguments."` — the model never had to
commit to argument content from a real user prompt. v2 fixes this at the
training-data level by backfilling from `~/.claude/projects/**/*.jsonl`.

**Mitigation (belt-and-suspenders):** `AntiLoopDetector` in
`scripts/fine_tune/validate_tool_calls.py`. Tracks the last N normalized
tool calls in a conversation; on the 3rd consecutive identical call,
suppresses the tool_call block and forces a text response. Emits WARN log
tagged with `model_version`. Increments `empty_args_emissions_total`
counter for production observability.

Enable in offline eval:
```bash
python scripts/fine_tune/validate_tool_calls.py \
    --backend openai --model qwen25-toolcalls \
    --anti-loop --model-version v2
```

Production hook points (not yet wired): `mcp_server.py` and the Claude
hooks. Wiring is part of issue #33 retrain follow-up.

Tests: `tests/fine_tune/test_anti_loop.py` (10 unit tests).
