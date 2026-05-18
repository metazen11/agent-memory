# V3 Fine-Tune Plan

**Status:** PROPOSED — 2026-05-15
**Supersedes:** v2 (retracted, see `docs/training_runs/v2-real-world-test.md`)
**Base:** `Qwen/Qwen3-8B`
**Training:** LOCAL MPS only (~36–40h wall clock)

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

v3 also constrains training to models ≤ 6 GB Q4_K_M while the pipeline
is being de-risked. Iteration speed matters more than capability right
now; v2's 12-hour training + bad-gate cycle was too slow to debug
effectively.

Full A/B: `docs/training_runs/v2-real-world-test.md`.

## 2. Goals (in priority order)

1. **Better at OUR work, doesn't degrade.** Concretely: better tool
   selection on agent-memory, fire-map, daily-dispatch, anvil. Measured
   by Class A (in-distribution shape match), Class B (tool_response
   adaptation), and Class E (project recall).
2. **Multi-turn tool_response grounding.** After a `<tool_response>` user
   turn, the assistant must adapt — emit a different tool_call, OR
   produce a text answer. Re-emitting the same call is the v2 regression
   to kill.
3. **Stop-after-tool_call.** Generation ends at `</tool_call>` cleanly.
   v2 hallucinated fake `user`/`tool_response`/`assistant` scaffolding
   inside one generation.
4. **On-topic action selection.** v2's prompt-3 emitting `gh issue create`
   for a "is there a test for X?" question is a training-distribution
   bleed that must not survive.
5. **No in-args generative loops.** Argument-value strings must not loop
   repeated content until max_tokens.
6. **≥125k effective context.** Via YaRN at serve time on Qwen3-8B's
   32k native window (`--rope-scaling yarn --rope-scale 4
   --yarn-orig-ctx 32768`, per FAILURE_MODES.md #12).
7. **Local-only.** Train on the user's Mac (~40h). No cloud.

Non-goals (deferred):
- Vision in the trained model — handled in the harness as a pre-pass
  (see §10).
- Audio modality.
- Function-calling reasoning chains (v3 stays one-step at a time).
- DPO/KTO preference training — defer to v3.1.

## 3. Base model decision

**Pick:** `Qwen/Qwen3-8B`

| Attribute | Value | Source |
|---|---|---|
| HF repo | `Qwen/Qwen3-8B` | huggingface.co/Qwen/Qwen3-8B |
| Params | 8.2B | model card |
| Native context | 32,768 | config.json |
| YaRN context | 131,072 (at scale 4, orig 32k) | rope_scaling docs |
| Q4_K_M size | ~5 GB | GGUF community ports |
| Tool calling in chat template | Yes (native `<tools>` branch) | tokenizer_config.json |
| License | Apache-2.0 | repo |

**Hard size rule:** any candidate base for v3 training must produce a
Q4_K_M GGUF of ≤ 6 GB. Qwen3-8B at ~5 GB clears this comfortably.

**Why not Qwen3.5-9B (the 9B is real but rejected for training):**
Hybrid Mamba/SSM architecture; LoRA tools have unproven injection
points for SSM tensors. Thinking-by-default mode fights tool-call SFT.
Multimodal weights wasted (vision moved to harness). 5.6 GB at the
size limit. Stays loaded in LM Studio for general inference but NOT
for training.

**Why not Qwen3-VL-8B-Instruct:** Vision moved to harness pre-pass per
user decision 2026-05-15 ("sacrifice vision in lieu of context").

**Why not Qwen2.5-7B-Instruct-1M:** No `<tools>` branch in default chat
template. Retrofitting one is exactly the kind of yak-shave v2 taught
us to avoid.

## 4. Training infrastructure

**Local MPS training, ~40h wall clock.** Apple Silicon M-series, MPS
backend, bf16 dtype. No cloud, no remote, no upload.

Wall clock estimate: ~3× v2's 12h 38m = **36–40h for 1 epoch** on the
v3 dataset at 8B. The user can use the machine for other work during
training (slow but possible — see Phase 0 pre-flight in §7).

Hard environmental requirement: **Dropbox MUST be quit during
training** (FAILURE_MODES #1 — symlink resolution under
`~/Dropbox/_CODING/...` corrupts checkpoint paths). Phase 2 in §7
makes this an explicit gate.

No `rclone`, no SSH, no pod provisioning, no upload step. Adapter is
saved straight to local disk; merge + GGUF conversion happen on the
same machine in the same shell.

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

6. **Project-tagged oversampling.** Tag rows whose prompts or paths
   mention `agent-memory`, `fire-map`, `daily-dispatch`, `anvil`, or
   known TDD/QA patterns. Oversample tagged rows at 2× the natural
   rate, capped so any single project ≤ 25% of training set. Document
   per-project row counts in MANIFEST. This is the data-side lever
   for the Class E "project recall" eval (§6).

### Build script — additional filters

7. **Cap argument-value length and repetition.** During row build, if
   any single argument value is longer than 2,000 chars OR contains > 3
   consecutive identical lines, drop the row OR truncate the argument
   to the first non-repeating prefix. This kills the v2 in-args
   generative-loop pathology at the data level. The most likely cause
   is real-Claude rows where a `python -c "..."` heredoc had a long
   generated string in it; we don't need those for tool-call shape
   learning.

8. **Vision-row filter.** The trained model is text-only (see §10). If
   any source rows reference images (screenshot prompts, image
   attachments), strip the image and replace with a `[VISION]`
   placeholder OR drop the row entirely. Default: drop. The MANIFEST
   records the count of vision-dropped rows so we can audit before
   training.

New `scripts/fine_tune/build_v3_dataset.py`, derived from `build_v2_dataset.py`,
with the changes above. Spec the data path:

```
data/processed/qwen3_tools/v3/
  train.chat.jsonl        ~60K-90K rows
  valid.chat.jsonl        ~5% session-aware split
  train.tiny.jsonl        200 rows for smoke
  valid.tiny.jsonl        30 rows
  tool_schemas.json       (carry v2 schemas)
  MANIFEST.json           drop reasons, oversample ratios, per-project row
                          counts, vision-dropped count
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

### Five test classes anchored to our training data

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

#### Class E — Project recall (the "justifies the work" gate)

Hand-curated set of ~20 prompts that mention the user's projects by
name — `agent-memory`, `fire-map`, `daily-dispatch`, `anvil` — and
require the model to use project-specific knowledge from training data
(paths, schemas, scripts, conventions). Examples: "where does fire-map
load WUI polygons from", "what's the agent-memory backfill script
called", "show me the daily-dispatch cron entry".

Match criteria (any one passes):
- References a real path/script/command from that project
- Uses the project's actual tool conventions (e.g. `make` targets,
  `scripts/` layout)
- Plausibly correct first action for that project's workflow

This is the gate that **justifies the whole exercise.** v1 and v2 will
both score very low here (they were trained without the project-tagged
oversampling from §5 fix #6). If v3 doesn't beat them on Class E, we
gained nothing from the rebuild.

**v3 gate: ≥ 50% project-correct recall, AND strictly better than both
v1 and v2 baselines.**

### Tool-call shape gate (cross-cutting)

Across all five classes, count instances where the model continues
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
Class E project recall    TBD%     TBD%     XX%     +YY pp
Shape violations          TBD      TBD      XX
In-args repetition viols  0        5/10     XX
```

**Ship gate:** Class B ≥ 85%, Class C all green, Class D ≥ 85%, Class
E ≥ 50% AND strictly better than v1 and v2, shape violations = 0,
in-args repetition violations = 0, Class A documented (no hard
threshold first run — we need v1 + v2 baselines first).

### What we measure BEFORE v3 training

Before starting v3, run all five classes against v1 and v2. This
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
| 0 | Pre-flight: verify base model HF revision SHA, llama.cpp version ≥ b6907 (Qwen3 family), transformers ≥ v4.57.0, ≥ 40 GB free RAM during training, ≥ 80 GB free disk, dataset delta still ≤ 30 days old | all green | restart |
| 0.5 | **Baseline v1 + v2 on all 5 eval classes (A/B/C/D/E).** Write to `docs/training_runs/v3-baselines.md`. | docs committed | NA |
| 1 | Build v3 dataset (`scripts/fine_tune/build_v3_dataset.py --write`) — includes project-tagged oversampling | MANIFEST shows < 5% reject rate, per-project row counts documented, held-out eval split written separately | rebuild with relaxed filters |
| 2 | Quit Dropbox; verify symlink integrity | clean | NA |
| 3 | Tiny training (`DATASET_TIER=tiny`) on LOCAL MPS | adapter saved, train loss curve sensible | abort, fix data |
| 4 | Tiny validator (single-turn, --min-parse-rate 0.05) | PASS | fix dataset shape |
| 5 | Full training on LOCAL MPS (~36–40h). User uses machine sparingly during this. | exit 0, train_loss < v2's 0.91 final | abort, halve LR |
| 6 | Merge LoRA, convert to GGUF f16 + Q4_K_M + Q6_K | files written, **Q4_K_M ≤ 6 GB confirmed** | re-quantize from f16 |
| 7 | Single-turn validator ≥ 85% on Q6_K | PASS | drop to Q8_0, retry |
| 8 | Tool-call shape gate (0 hallucinated post-tool_call scaffolding) | PASS | retrain with stricter stop-after-tool_call |
| 9 | **Multi-turn real-world A/B (Classes B + C + E)** | PASS — primary gate | retrain with adjusted oversample ratios |
| 10 | Restart Dropbox; LM Studio install + manual smoke test | works | uninstall, keep v1 |
| 11 | Run report + PR + close v3 parent issue | merged | abandon |

## 8. v3 training config (proposed)

```
base:           Qwen/Qwen3-8B (at pinned HF revision SHA)
adapter:        LoRA r=32 alpha=64 dropout=0.05
target_modules: q_proj k_proj v_proj o_proj gate_proj up_proj down_proj
epochs:         1.0
max_length:     4096
batch_size:     1
grad_accum:     8
lr:             1.5e-4 cosine
warmup_ratio:   0.03
dtype:          bf16
device:         mps (NOT cuda)
eval_steps:     500
save_steps:     250
per_device_eval_batch_size: 1   # FAILURE_MODES #8
seed:           42
```

`max_length=4096` covers ~95% of real Claude sessions per the v2 length
histogram. Training above that on MPS is a memory cliff. Inference
still gets ≥125k via YaRN at serve time (`--rope-scaling yarn
--rope-scale 4 --yarn-orig-ctx 32768`).

## 9. Mitigations from v2 carry-forward

These v2 lessons still apply unchanged:

- `.absolute()` not `.resolve()` for paths under `models/` (FAILURE_MODES #1)
- Quit Dropbox before training (Phase 2 gate in §7)
- ENV-var-driven training script (v3's `run_train_lora_v3.py` keeps the
  pattern, just swaps base)
- GGUF output names include version tag — `qwen3-8b-toolcalls-v3-q6k.gguf`
- Validator suite alignment — the new multi-turn gate makes this less
  fragile but still: validator system prompt must match training format
- Q6_K is the safe ship quant; Q4_K_M may not preserve arg commitment;
  ship the smallest quant that passes Gate 7

## 10. Vision: harness pre-pass, not in the trained model

Vision is OUT of the trained model. Image inputs are routed through a
separate harness pre-pass — an off-the-shelf vision model (Qwen2.5-VL
or the existing mmproj at
`~/.lmstudio/models/lmstudio-community/Qwen3.5-9B-GGUF/mmproj-Qwen3.5-9B-BF16.gguf`)
generates a text description, then the trained Qwen3-8B handles the
tool call. This keeps training under 6 GB Q4_K_M, removes
vision-encoder complexity from the LoRA, and lets us swap vision
models without retraining.

Flow:
```
[image] → vision model (untrained, swappable) → [text description]
       → trained Qwen3-8B (tool-call SFT)      → [tool_call]
```

This is referenced as a harness feature, not a training feature.
Dataset fix #8 (§5) keeps vision rows out of the training corpus.

## 11. What we are NOT doing in v3

- Vision-in-model (see §10 — harness pre-pass instead)
- DPO/KTO preference training (defer to v3.1 if SFT alone fixes the
  re-emit regression)
- Audio modality
- Distillation (would require teacher generations from v3 + smaller base)
- Replacing the MCP runtime anti-loop guard (it's a belt-and-suspenders
  layer that should stay regardless of model quality)
- Re-evaluating v1 across all prompt categories (we have enough data;
  v1 ships)

## 12. Approval gate

Before kicking off v3:

- [ ] User reviews this plan
- [ ] User confirms Qwen3-8B base
- [ ] User confirms ≥40 hr local training is acceptable
- [ ] User confirms quitting Dropbox during training
- [ ] User confirms gate thresholds in §6 (especially Class E ≥ 50%
      AND strictly better than v1/v2)
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
- Base model card: huggingface.co/Qwen/Qwen3-8B
