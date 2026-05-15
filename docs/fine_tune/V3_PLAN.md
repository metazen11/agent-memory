# V3 Fine-Tune Plan

**Status:** PROPOSED — 2026-05-15
**Supersedes:** v2 (retracted, see `docs/training_runs/v2-real-world-test.md`)
**Base:** `Qwen/Qwen3-VL-8B-Instruct`
**Training:** cloud A100/H100 (Runpod), NOT MPS

## 1. Why v3 exists (the v2 postmortem in one paragraph)

v2 shipped with a Phase-9 gate that measured the wrong symptom. The eval
counted empty-args `{}` emissions on vague single-turn prompts; v2 hit
0/50 there and was declared production-ready. Real-world multi-turn A/B
on 2026-05-15 showed the truth: v2 produces 0/10 useful answers vs v1's
3/10, loops on 90% of sessions vs v1's 40%, and adapts to tool_response
only 30% of the time vs v1's 90%. v2 fixed path hallucination (real win
— stays in the right repo) but broke tool_response grounding
(catastrophic regression). v3 must fix that regression and the latent
v1 regressions it inherits.

Full A/B: `docs/training_runs/v2-real-world-test.md`.

## 2. Goals (in priority order)

1. **Multi-turn tool_response grounding.** After a `<tool_response>` user
   turn, the assistant must adapt — emit a different tool_call, OR
   produce a text answer. Re-emitting the same call is the v2 regression
   to kill.
2. **Stop-after-tool_call.** Generation ends at `</tool_call>` cleanly.
   v2 hallucinated fake `user`/`tool_response`/`assistant` scaffolding
   inside one generation.
3. **On-topic action selection.** v2's prompt-3 emitting `gh issue create`
   for a "is there a test for X?" question is a training-distribution
   bleed that must not survive.
4. **Carry forward v2's wins:** stays in the right repo (no fire-map
   path hallucination), plausible local commands, populated arg values.
5. **Add capabilities:** 256K context (8× v2's 32K), native vision, more
   modern base model with native tool-calling in the chat template.

Non-goals (deferred):
- Audio modality (Qwen3-Omni is 30B MoE, way over budget)
- Function-calling reasoning chains (v3 stays one-step at a time)
- Sub-1B distillation (separate ticket if v3 ships)

## 3. Base model decision

**Pick:** `Qwen/Qwen3-VL-8B-Instruct`

| Attribute | Value | Source |
|---|---|---|
| HF repo | `Qwen/Qwen3-VL-8B-Instruct` | huggingface.co/Qwen/Qwen3-VL-8B-Instruct |
| Params | ~9B | model card |
| Native context | 262,144 (extendable to 1M with YaRN) | config.json |
| Vision | Yes (separate mmproj file at inference) | model card |
| Tool calling in chat template | Yes — verified `<tools>` branch + `<tool_call>`/`</tool_call>` emit logic in tokenizer_config.json | direct Jinja read |
| License | Apache-2.0 | repo |
| Released | 2025-10-15 | release notes |
| GGUF available | Yes, official: `Qwen/Qwen3-VL-8B-Instruct-GGUF` (Q4_K_M/Q8_0/F16 + mmproj Q8_0/F16) | repo |
| llama.cpp floor | b6907 (Qwen3-VL architecture support) | llama.cpp release notes |
| transformers floor | v4.57.0 | model card |

**Why not Qwen2.5-VL-7B:** Stock chat template has NO tools branch.
Vocab has `<tool_call>` tokens but the Jinja doesn't reference them.
Training would fight the template. Documented in QwenLM/Qwen3-VL #1093.

**Why not Qwen2.5-7B-Instruct-1M:** 1M context is great but no vision,
and tool-calling not in default chat template either.

**Why not Qwen3-8B (text-only):** Same template story as VL but no vision.
Fallback if vision-pipeline plumbing slips.

**Why not Qwen3.5/3.6:** Only exist as 35B-A3B MoE — too large for our
budget (cloud cost) and inference target (LM Studio / llama.cpp local).

## 4. Training infrastructure

**Cloud, not local.** v2 took 12h 38m on MPS for 23k rows / 3B model /
1 epoch. v3 is ~3× the data (75K+ rows after dedupe/filter) and ~3× the
model (9B). Linear scaling = ~100h on MPS, which is unworkable. Cloud
A100 80GB ≈ ~6h for the same job. Cost ~$15–20.

| Provider | GPU | Hourly | Est. total | Notes |
|---|---|---|---|---|
| Runpod | A100 80GB | ~$2/h | ~$12–18 | already used in flight test |
| Vast.ai | A100 80GB | ~$1.50/h | ~$9–14 | cheaper, less predictable |
| Modal | A100 80GB | ~$3/h | ~$18–24 | nicer DX, more expensive |
| Local MPS | M-series | $0 | 100h+ | unworkable for 8B |

**Pick:** Runpod A100 80GB first. Fallback Vast if Runpod has no capacity
on training day.

Workflow:
1. `rclone` dataset + base model to Runpod pod-attached volume
2. Train via PEFT LoRA (same recipe shape as v2)
3. Download adapter back to laptop
4. Merge + convert to GGUF locally (still uses llama.cpp on the Mac)
5. Local validation + chat-loop verify

Adapter download is ~150–300 MB so the merge-locally step is cheap.

## 5. Dataset rebuild

**v3 must rebuild.** Delta since v2 cutoff (2026-03-29 → 2026-05-15) is
303K+ new rows / 1.3 GB / 1,141 files. That's 12× v2's row count. Reusing
v2 would lose diversity and bias toward stale workflows.

### Pipeline changes vs v2

1. **Stop-after-tool_call cut.** When building chat rows from a Claude
   session, truncate the assistant turn at the first `</tool_call>`. Drop
   any subsequent text the model emitted in the same assistant turn. This
   kills the v2 "hallucinated chat scaffolding" failure mode at the source.

2. **Tool_response → text-answer rows oversampled.** Find Claude session
   rows where the structure is `[tool_call] → [tool_response] → [text
   answer, no further tool_call]` and oversample them at 2× the natural
   rate. Caps at 20% of training set so we don't break tool-call shape
   learning. This directly addresses v2's "ignores tool_response"
   regression.

3. **Negative training: identical-re-emit pairs.** For ~5% of training
   rows, synthesise a preference pair where the chosen turn is "adapt
   after tool_response" and rejected is "re-emit identical call". Use
   DPO or KTO on top of the SFT base. Optional — if DPO infrastructure
   is too much for v3 scope, defer to v3.1.

4. **Filter out subagent transcripts.** The dataset-delta scan showed
   `subagents/agent-*.jsonl` files in the post-cutoff data. Those are
   model self-talk, not user prompts. Filter on path prefix.

5. **Filter out off-distribution actions for the user's repo.** If a
   prompt is about agent-memory, drop rows where the assistant calls
   `gh issue create`, `git commit`, or any action that wouldn't make
   sense as a first-turn response. v2 picked these up from training-data
   mid-workflow continuations. Keep the tool_call → tool_response pairs
   but require the tool_call to be a *discovery* action (Bash/Read/Grep)
   not a *mutation* action.

6. **Vision examples (NEW — if scope allows).** If we want v3 to use
   vision, sample ~5% of training rows from Anvil's screenshot capture
   (already exists in `~/Dropbox/_CODING/anvil/screenshots/`). Pair each
   screenshot with a real prompt like "what does this UI look like" or
   "is this button styled correctly". Keep small at first; vision is a
   nice-to-have, not the goal. **Decision needed:** ship vision in v3
   or hold for v3.1?

### Build script — additional filter for the in-args loop

7. **Cap argument-value length and repetition.** During row build, if
   any single argument value is longer than 2,000 chars OR contains > 3
   consecutive identical lines, drop the row OR truncate the argument
   to the first non-repeating prefix. This kills the v2 in-args
   generative-loop pathology at the data level. The most likely cause
   is real-Claude rows where a `python -c "..."` heredoc had a long
   generated string in it; we don't need those for tool-call shape
   learning.

New `scripts/fine_tune/build_v3_dataset.py`, derived from `build_v2_dataset.py`,
with the changes above. Spec the data path:

```
data/processed/qwen3_vl_tools/v3/
  train.chat.jsonl        ~60K-90K rows
  valid.chat.jsonl        ~5% session-aware split
  train.tiny.jsonl        200 rows for smoke
  valid.tiny.jsonl        30 rows
  tool_schemas.json       (carry v2 schemas, possibly add Glob/Vision-specific)
  MANIFEST.json           drop reasons, oversample ratios, vision-row count
```

## 6. Eval suite — the v2 mistake corrected

v2 shipped with two gates:
- Single-turn offline validator (85% threshold) — passed at 85%
- Chat-loop "0 empty-args on 50 vague prompts" — passed at 0/50

Both passed; v2 was strictly worse than v1 in production. **The gates
must change for v3.**

### What "the gates must change" means concretely

The old gates measured generic capabilities ("does it emit valid tool
calls", "does it avoid one specific failure shape"). They were blind
to whether the model behaves on **our** prompts in **our** sessions.
v3 evals are anchored to our training data so that "before vs after"
is a direct comparison on the distribution we actually care about.

### Three test classes anchored to our training data

#### Class A — Replayed session starts (in-distribution)

Take 30 random sessions from the training corpus (held out from
training — pre-split before dataset build). For each:
- Feed the model the first user prompt only
- Compare model's turn-1 tool_call to what the *held-out* session's
  assistant actually did

Match criteria (any one passes):
- Same tool name AND same set of arg keys ("shape match")
- Same tool name AND ≥ 50% overlap in arg values ("close match")
- Different tool name but plausible alternative for the same intent
  (e.g. Bash with `git log` vs Read of a CHANGELOG — needs manual review)

Output:
- shape-match rate
- close-match rate
- manual-review-needed rate

v1 baseline (to be measured before v3 training, never done before):
TBD. v2 baseline: TBD. v3 gate: ≥ 60% shape-match.

#### Class B — Tool_response → next-turn adaptation (the v2 regression)

Take 30 sessions where the structure is at least:
```
[user prompt] → [assistant tool_call] → [tool_response] → [assistant ???]
```

Feed the model the first 3 turns. Measure what the model emits at the
4th turn:
- **adapted_tool_call** — different tool name OR different args than turn-2
- **text_answer** — synthesised a text response from the tool_response
- **identical_reemit** — same tool name AND identical args (the v2 bug)
- **off_topic** — different but unrelated to the tool_response content

Compute: `adaptation_rate = (adapted_tool_call + text_answer) / 30`.

v1 baseline from real-world A/B: 9/10 = 90%.
v2 baseline from real-world A/B: 3/10 = 30%.
**v3 gate: ≥ 85%.**

#### Class C — Out-of-distribution agentic prompts (generalization)

The 10 prompts already in `tests/fine_tune/real_world/harness.py` —
none of which match training-data shape exactly. Same harness, same
metrics (useful answer rate, loop rate, adaptation rate). Generalization
signal.

v1 baseline: 30% useful, 40% loop, 90% adapt.
v2 baseline: 0% useful, 90% loop, 30% adapt.
**v3 gates: ≥ 50% useful, ≤ 30% loop, ≥ 80% adapt.**

#### Class D — Single-turn validator (carryover, necessary not sufficient)

Existing `scripts/fine_tune/validate_tool_calls.py` at ≥ 85% aggregate.
Keep as a sanity check; v2 passed this and was still broken in real use.

### Tool-call shape gate (cross-cutting)

Across all four classes, count instances where the model continues
generating after `</tool_call>` (the v2 "hallucinated chat scaffolding"
regression). Pass: 0 / total prompts. Fail: any.

### In-args repetition gate (new — caught in v2 chat-template retest)

The 2026-05-15 chat-completions retest discovered v2 also has a
**within-argument generative loop**: on 5/10 prompts the model produced
a tool_call whose JSON arguments string was so long it hit
max_tokens=512 mid-string, with hundreds of repeated lines (e.g.
`print('Tool_log: ...')` × 12 in a single shell one-liner). This is
distinct from the multi-turn loop — it happens inside argument values
on a single turn.

v1 never produced this; v2 produces it on 50% of prompts. **v3 must
not regress here.**

Gate: per-prompt, longest line repetition inside any string-valued
argument ≤ 3× consecutive repeats. Pass: 0 violations / 100 prompts.
Fail: any. Measured via simple line-grouping over the rendered
arguments JSON.

### Before/after table format

The deliverable from each v3 evaluation pass is one table:

```
                          v1       v2       v3      Δ vs v1
Class A shape-match       TBD%     TBD%     XX%     +YY pp
Class B adapt rate        90%      30%      XX%     +YY pp
Class C useful answer     30%      0%       XX%     +YY pp
Class C loop rate         40%      90%      XX%     +YY pp
Class D aggregate         80%      85%      XX%     +YY pp
Shape violations          TBD      TBD      XX
In-args repetition viols  0        5/10     XX
```

**Ship gate:** Class B ≥ 85%, Class C all green, Class D ≥ 85%, shape
violations = 0, in-args repetition violations = 0, Class A documented
(no hard threshold first run — we need v1 + v2 baselines first).

### What we measure BEFORE v3 training

Before starting v3, run all four classes against v1 and v2. This
establishes baselines we can actually compare against — without them
we have no idea whether v3 improvements are real or measurement noise.
This is Phase 0.5 in §7.

### When to ship

All gates green AND eval suite committed to repo as
`tests/fine_tune/eval_v3/` AND baseline numbers for v1/v2 documented
in `docs/training_runs/v3-baselines.md` AND human sanity review.

## 7. Phases

| Phase | Action | Gate | Rollback |
|---|---|---|---|
| 0 | Pre-flight: verify base model HF revision SHA, llama.cpp ≥ b6907, transformers ≥ v4.57, Runpod credit ≥ $30, dataset delta still ≤ 30 days old | all green | restart |
| 0.5 | **Baseline v1 + v2 on all four eval classes (A/B/C/D).** Write numbers to `docs/training_runs/v3-baselines.md`. WITHOUT this baseline, no v3 improvement is provable. | doc committed | NA |
| 1 | Build v3 dataset (`scripts/fine_tune/build_v3_dataset.py --write`) | MANIFEST shows < 5% reject rate, vision row count documented, **held-out eval split written separately** (30 sessions for Class A, 30 for Class B) | rebuild with relaxed filters |
| 2 | Push dataset + base to Runpod pod | uploads complete, SHAs match | retry rclone |
| 3 | Tiny training (`DATASET_TIER=tiny`) on Runpod | adapter saved, train loss curve sensible | abort, fix data |
| 4 | Tiny validator (single-turn, --min-parse-rate 0.05) | PASS | fix dataset shape |
| 5 | Full training on Runpod (~6h A100) | exit 0, train_loss < v2's 0.91 final | abort, halve LR |
| 6 | Download adapter, merge locally, convert to GGUF f16 + Q4_K_M + Q6_K + Q8_0 | files written, SHAs computed | re-quantize from f16 |
| 7 | Single-turn validator ≥ 85% on Q6_K | PASS | drop to Q8_0, retry |
| 8 | Tool-call shape gate (Gate 5 above) | PASS | retrain with stricter stop-after-tool_call |
| 9 | **Multi-turn real-world A/B (Gates 1, 2, 3)** | PASS — primary gate | retrain with adjusted oversample ratios |
| 10 | LM Studio install + manual smoke test by user | works | uninstall, keep v1 |
| 11 | Run report + PR + close v3 parent issue | merged | abandon |

Phase 5 on Runpod requires:
- Pod with A100 80GB and 200 GB ephemeral
- `pip install -r requirements-cloud.txt` (new file: pinned transformers ≥ 4.57, peft latest, datasets, accelerate)
- Wandb integration so we can watch loss curves from the laptop

## 8. v3 training config (proposed)

```
base:           Qwen/Qwen3-VL-8B-Instruct (at known revision SHA)
adapter:        LoRA r=32 alpha=64 dropout=0.05 (up from v2's r=16)
target_modules: q_proj k_proj v_proj o_proj gate_proj up_proj down_proj
epochs:         1.0
max_length:     4096 (up from v2's 1024 — most sessions need more context)
batch_size:     1
grad_accum:     8 (up from v2's 4 — 8B model + longer seqs need more accum)
lr:             1.5e-4 cosine (down from v2's 2e-4 — larger model, smaller LR)
warmup_ratio:   0.03
dtype:          bf16
device:         cuda (Runpod), NOT mps
eval_steps:     500
save_steps:     250
seed:           42
```

`max_length=4096` is a big jump. The base supports 262K but training at
that length on a single A100 is a memory cliff. 4K covers ~95% of real
Claude sessions per the v2 length histogram. Inference still gets the
full 256K from the base position embeddings.

## 9. Mitigations from v2 carry-forward

These v2 lessons still apply unchanged:

- `.absolute()` not `.resolve()` for paths under `models/` (FAILURE_MODES #1)
- Quit Dropbox before training (irrelevant on Runpod, still relevant for
  laptop merge step)
- ENV-var-driven training script (v3's `run_train_lora_v3.py` keeps the
  pattern, just swaps base + cuda)
- GGUF output names include version tag — `qwen3-vl-8b-toolcalls-v3-q6k.gguf`
- Validator suite alignment — the new multi-turn gate makes this less
  fragile but still: validator system prompt must match training format
- Q6_K is the safe ship quant; Q4_K_M may not preserve arg commitment;
  ship the smallest quant that passes Gate 7

## 10. Vision: ship in v3 or hold?

**Recommendation: HOLD vision for v3.1.** Reasoning:

1. v3's primary mandate is fixing the v2 multi-turn regression. Adding
   vision is orthogonal and doubles the eval surface.
2. Vision adds an mmproj file (separate GGUF), a different inference
   path (`llama-mtmd-cli`), and a different training-data shape (image
   tokens). Compounding risk.
3. The user's actual day-to-day pain is tool-call quality on text
   prompts, not screenshot interpretation.
4. v3 base supports vision regardless — if v3.1 wants to add it, the
   base is already vision-capable, just need to add vision rows to the
   dataset and retrain.

If you want vision in v3 anyway, the scope add is ~+1 day for dataset
work, ~+2h for training (image tokens are cheap), and an extra GGUF
artifact to ship. **Your call.**

## 11. What we are NOT doing in v3

- DPO/KTO preference training (defer to v3.1 if SFT alone fixes the
  re-emit regression)
- Audio modality (Qwen3-Omni is too large)
- Distillation (would require teacher generations from v3 + smaller base)
- Replacing the MCP runtime anti-loop guard (it's a belt-and-suspenders
  layer that should stay regardless of model quality)
- Re-evaluating v1 across all prompt categories (we have enough data;
  v1 ships)

## 12. Approval gate

Before kicking off v3:

- [ ] User reviews this plan
- [ ] User confirms vision in/out
- [ ] User confirms Runpod or alternative cloud provider
- [ ] User confirms budget (~$15–20 estimated, $50 cap)
- [ ] User confirms gate thresholds in §6 (especially the 50% useful-answer target)
- [ ] Quality-gate agent runs against this plan and produces a JSON
      finding (per CLAUDE.md "Plans require senior production review")

Once approved: open v3 parent issue, branch off main, run Phase 0.

## 13. Reference

- Real-world A/B: `docs/training_runs/v2-real-world-test.md`
- v2 training report: `docs/training_runs/v2-20260514T055422Z.md` (with retraction header)
- v2 plan (superseded): `docs/fine_tune/V2_TRAINING_PLAN.md`
- Pipeline runbook (general): `docs/fine_tune/PIPELINE_RUNBOOK.md`
- Failure modes: `docs/fine_tune/FAILURE_MODES.md` (will get v3 additions during the run)
- Harness: `tests/fine_tune/real_world/harness.py`
- Base model card: huggingface.co/Qwen/Qwen3-VL-8B-Instruct
- Base GGUF: huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF
