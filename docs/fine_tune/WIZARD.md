# Fine-Tune Wizard

A guided, interactive TUI that wraps the v3 ad-hoc playbook (`V3_PLAN.md`)
into a single command. Lives at `scripts/fine_tune/wizard.py`.

The wizard is the **front end** to the existing pipeline scripts
(`build_v2_dataset.py` / `build_v3_dataset.py`, the training runner,
`validate_tool_calls.py`, the Class A/B/E harness, `llama-quantize`).
It does not replace any of them; it sequences them and adds gates.

## Quick start

```bash
# Interactive wizard (Mac / Linux / Windows)
.venv-finetune/bin/python scripts/fine_tune/wizard.py

# Non-interactive — run from a saved config
.venv-finetune/bin/python scripts/fine_tune/wizard.py \
    --config train_config.yaml --no-tui

# Print the JSON Schema for train_config.yaml
.venv-finetune/bin/python scripts/fine_tune/wizard.py --print-config-schema

# Print a default config to stdout
.venv-finetune/bin/python scripts/fine_tune/wizard.py --print-default-config
```

## What the wizard does

The wizard surfaces **five choice screens** and then runs **twelve pipeline
screens** end-to-end. Each pipeline screen wraps an existing script — the
wizard injects config via env vars and streams stdout into a log panel.

### Choice screens

1. **Base model** — Qwen3-4B (default), Qwen3-8B, Qwen3.5-9B (with hybrid
   Mamba/thinking-by-default warning per V3_PLAN.md §3), or a custom HF
   repo id.
2. **Dataset filters** — checkbox per V3_PLAN.md §5 fix. All 8 filters
   default on except `negative_reemit_pairs` (the DPO step, deferred to v3.1).
3. **Project oversampling** — agent-memory, fire-map, daily-dispatch, anvil.
4. **Date range** — defaults `2026-03-29 → today` (the V3 cutoff window).
5. **Quants + run type** — Q4_K_M and Q6_K by default; run type is one of
   `tiny_smoke` / `full` / `dataset_only` / `eval_only`.

After the choice screens the wizard writes `train_config.yaml` to the repo
root and either runs the pipeline immediately or exits so you can re-run
with `--config train_config.yaml` later.

### Pipeline screens

| # | Screen | Wraps | Notes |
|---|---|---|---|
| 1 | Environment check | introspection | Python, venv, device (CUDA/MPS/CPU), disk, llama.cpp, `gh`, Dropbox state |
| 2 | Library check | `pip install` | Auto-installs missing libs via `sys.executable -m pip` so the venv is respected |
| 3 | Base model download | `huggingface_hub.snapshot_download` | Skips if `models/base/<slug>/REVISION.txt` exists |
| 4 | Dataset build | `build_v3_dataset.py` (falls back to v2) | Filters + projects + dates passed as env vars |
| 5 | Dataset audit (**GATE**) | reads `docs/training_runs/v3-dataset-audit.md` / MANIFEST.json | User clicks Approve / Reject. No auto-skip in TUI. |
| 6 | Tiny training | `run_train_lora.py` with `DATASET_TIER=tiny` | ~30 min on MPS |
| 7 | Tiny eval (**GATE**) | `validate_tool_calls.py --min-parse-rate 0.05` | User approves continuing to full |
| 8 | Full training | `run_train_lora.py` with `DATASET_TIER=full` | ~36-40h on MPS for 8B. Detach with Ctrl-D. |
| 9 | GGUF conversion | `llama-quantize` | Skipped gracefully if llama.cpp not built |
| 10 | Validation suite | `validate_tool_calls.py` + Class A/B/E harness | Pass/fail per V3_PLAN.md gates |
| 11 | LM Studio install | `shutil.copy2` -> `~/.lmstudio/models/<publisher>/...` | Skipped on Linux if `~/.lmstudio/` missing |
| 12 | Done | summary | Paths, next steps |

## Mark's PC (verify_gguf.py)

A separate small script for collaborators who receive a shipped GGUF and
want a sanity check. No Textual, ~400 lines, cross-platform.

```bash
# Print metadata: architecture, params, context, quant, chat template
python scripts/fine_tune/verify_gguf.py info path/to/model.gguf

# Boot llama-server on a free port, send 3 prompts, verify tool_calls
python scripts/fine_tune/verify_gguf.py smoke path/to/model.gguf

# Run Class B + E harness against the model
python scripts/fine_tune/verify_gguf.py eval path/to/model.gguf
```

`verify_gguf.py` only requires `psutil` (and the harness scripts for
`eval`). It uses `pathlib`, `shutil.which`, and a free-port picker — no
Mac- or Linux-specific paths.

## Where artifacts land

| Artifact | Path |
|---|---|
| Saved config | `train_config.yaml` (repo root) |
| Base model snapshot | `models/base/<slug>/` |
| LoRA adapter | `models/lora/<slug>-toolcalls-v3-lora/` |
| Merged HF model | `models/merged/<slug>-toolcalls-v3-merged/` |
| GGUF files | `models/gguf/<slug>-toolcalls-v3-{q4km,q6k,...}.gguf` |
| Validator reports | `tests/fine_tune/real_world/baselines/` |
| LM Studio install | `~/.lmstudio/models/<publisher>/<slug>-toolcalls-v3/` |

`<slug>` is derived from the base model name (`Qwen/Qwen3-4B` → `qwen3-4b`).

## Gates and how to override them

The wizard enforces two interactive gates:

1. **Dataset audit** (Screen 5) — renders the audit markdown; user must
   click Approve.
2. **Tiny eval** (Screen 7) — must pass before full training kicks off.

In `--no-tui` mode there is no user to click, so gates **auto-approve and
log a warning**. Use the TUI when you want the gate to actually block. If
you need to bypass a gate in TUI mode, click "Reject + abort" and re-run
with `--no-tui` (acknowledging that you're skipping a safety check).

## Cross-platform notes

- **Mac (MPS)** — primary target. llama.cpp expected at
  `models/llama.cpp/build/bin/`. LM Studio at `~/.lmstudio/models/`.
- **Linux (CUDA / CPU)** — same llama.cpp path. LM Studio dir often
  absent; Screen 11 is skipped gracefully.
- **Windows (CUDA / CPU)** — llama.cpp expected at
  `models/llama.cpp/build/Release/`. Dropbox detection uses `tasklist`.
  GGUF conversion is skipped on PCs without llama.cpp (Mark's typical
  case); he uses `verify_gguf.py` against a shipped GGUF instead.

The wizard uses `platform.system()` and `torch.cuda.is_available()` /
`torch.backends.mps.is_available()` to choose the device. It never
hardcodes `/Users/` paths — everything flows through `Path.home()`,
`Path(__file__).absolute()`, and config-derived paths.

## Auto-install

Missing libraries are installed via `sys.executable -m pip install` so the
**active venv** receives them. Bare `pip` is never invoked. Disable with
`--no-auto-install`.

## TTY fallback

If the wizard is launched outside a TTY (e.g. CI, a captured subprocess),
it falls back to text mode automatically — no Textual import, no
interactive widgets, gates auto-approve with a warning.

## Known limitations / TODOs

- **Live GPU utilisation** in Screen 8 is shown via `psutil` CPU/RAM only.
  CUDA usage from `nvidia-smi` and MPS usage from `powermetrics` are
  TODO — Screen 8 currently runs the trainer as a subprocess and surfaces
  stdout but doesn't graph utilisation.
- **`negative_reemit_pairs`** filter is wired through the config but DPO
  infrastructure is deferred to v3.1 (V3_PLAN.md §5 fix #3).
- **Merge step** (LoRA → HF → GGUF f16) is **not** orchestrated by the
  wizard. Run the existing merge script manually or expect the f16 GGUF
  to be present before Screen 9.
- **Detach (Ctrl-D)** in TUI exits the wizard but does not background the
  training subprocess. Use `caffeinate -i` on Mac if you need to leave
  the lid open during a 40h run.
- **Schema validation** of `train_config.yaml` is loose (basic shape
  checks via the embedded JSON Schema). It does not enforce date format
  or numeric ranges — you'll see the error at the script that consumes
  the bad value.

## Related docs

- `docs/fine_tune/V3_PLAN.md` — the source plan this wizard executes
- `docs/fine_tune/V2_TRAINING_PLAN.md` — superseded, kept for context
- `docs/fine_tune/FAILURE_MODES.md` — the list of known landmines the
  wizard tries to dodge
- `docs/training_runs/v2-real-world-test.md` — why v2 was retracted and
  v3 needed a rebuild
