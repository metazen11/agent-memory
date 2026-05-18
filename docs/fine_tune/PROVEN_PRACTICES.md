# PROVEN PRACTICES — Pre-v4.5 LoRA Training Brief

**Audience:** single operator, Qwen3-4B tool-call LoRA, Apple Silicon (MPS), plain HF transformers + PEFT (no Unsloth, no MLX).
**Goal:** compare established practice against our current recipe and identify cheap wins.
**Date:** 2026-05-18.
**Status:** research-only, no code touched.

## Baseline (what we do today)

| Knob | Value |
|---|---|
| Base | Qwen3-4B |
| Adapter | PEFT LoRA, r=32, α=64, dropout=0.05 |
| target_modules | `all-linear` |
| Optimizer | AdamW (torch), lr=2e-4 constant, no warmup |
| Schedule | 1.0 epoch, grad_accum=4, no early stop |
| MAX_LENGTH | 1024 (full), 512 (tiny) |
| Device / dtype | MPS, bfloat16 |
| Eval | every 500 steps (NaN watch only) |
| Checkpoints | every 250 steps, kept |

## Source map

Citations used throughout this brief:

- Unsloth — [Fine-tuning LLMs Guide](https://docs.unsloth.ai/get-started/fine-tuning-llms-guide)
- Unsloth — [LoRA Hyperparameters Guide](https://docs.unsloth.ai/get-started/fine-tuning-llms-guide/lora-hyperparameters-guide)
- Unsloth — [Qwen3 How to Run & Fine-tune](https://unsloth.ai/docs/models/tutorials/qwen3-how-to-run-and-fine-tune)
- Unsloth — [Qwen3.5 Fine-tuning Guide](https://unsloth.ai/docs/models/qwen3.5/fine-tune)
- Unsloth — [Troubleshooting & FAQs](https://docs.unsloth.ai/basics/troubleshooting-and-faqs)
- HuggingFace PEFT — [LoRA conceptual guide](https://huggingface.co/docs/peft/en/conceptual_guides/lora)
- HuggingFace PEFT — [LoRA developer guide](https://huggingface.co/docs/peft/developer_guides/lora)
- HuggingFace TRL — [LoRA Without Regret](https://huggingface.co/docs/trl/main/en/lora_without_regret)
- Thinking Machines Lab — [LoRA Without Regret blog](https://thinkingmachines.ai/blog/lora/)
- Qwen / HF — [Qwen3-4B model card](https://huggingface.co/Qwen/Qwen3-4B), [Qwen3-4B-Base](https://huggingface.co/Qwen/Qwen3-4B-Base)
- HF Optimum-Neuron — [Fine-Tune Qwen3 8B with LoRA](https://huggingface.co/docs/optimum-neuron/training_tutorials/finetune_qwen3)
- Databricks — [Efficient Fine-Tuning with LoRA](https://www.databricks.com/blog/efficient-fine-tuning-lora-guide-llms)
- ArXiv — [Leaner Training, Lower Leakage (memorization in LoRA SFT)](https://arxiv.org/pdf/2506.20856)
- ArXiv — [Mitigating Unintended Memorization with LoRA](https://arxiv.org/pdf/2502.05087)
- ArXiv — [Deduplicating Training Data Makes LMs Better](https://arxiv.org/pdf/2107.06499)
- ArXiv — [LongLoRA](https://arxiv.org/pdf/2309.12307)
- Aionlinecourse — [PEFT target modules by architecture](https://www.aionlinecourse.com/blog/target-modules-for-applying-peft-lora-on-different-models)
- HF bitsandbytes — [8-bit optimizers](https://huggingface.co/docs/bitsandbytes/main/en/optimizers)
- Medium / Haldankar — [LoRA Fine-Tuning on Apple Silicon](https://medium.com/@haldankar.deven/lora-fine-tuning-on-apple-silicon-d000ea38453c)
- GitHub — [mps-bitsandbytes](https://github.com/mpsops/mps-bitsandbytes)
- ArXiv — [Beware of the Batch Size (LoRA hyperparameter bias)](https://arxiv.org/pdf/2602.09492)
- Gist — [Medina 14B: Multi-GPU LoRA Hot-Swap Benchmark](https://gist.github.com/synchronic1/22ad2e229fe760f0ccd5313f53adea59)
- ArXiv — [Align, Don't Divide: LoRA in Multi-Task Learning](https://arxiv.org/pdf/2508.05078)

---

## 1. Learning rate + schedule

**(a) Established practice:**
- Unsloth's default and widely cited recommendation is **lr = 2e-4** for LoRA, with **warmup_ratio = 0.1** (or `warmup_steps = 5` for very short runs) and a decaying schedule (linear or cosine) rather than constant. The first-principles reason cited is "prevents drastic, unstable changes at the very beginning of training" ([Unsloth LoRA Hyperparameters Guide](https://docs.unsloth.ai/get-started/fine-tuning-llms-guide/lora-hyperparameters-guide)).
- Thinking Machines' "LoRA Without Regret" gives a more nuanced rule: **optimal LoRA LR is ~10× the optimal FullFT LR**, and **scales with rank as roughly r^-0.84**, not r^-1. Concretely they report ~2.5e-4 at r=256 and ~1.2e-4 at r=1 for their setup. At our r=32 this lands between, roughly 1.5–2e-4 ([Thinking Machines blog](https://thinkingmachines.ai/blog/lora/)).
- AWS / HF Optimum-Neuron Qwen3-8B LoRA tutorial uses **5e-4** ([HF Optimum-Neuron Qwen3](https://huggingface.co/docs/optimum-neuron/training_tutorials/finetune_qwen3)). Community examples for Qwen3-4B use 2e-4.
- Cosine vs linear: most sources say they're close, cosine often "slightly better" or at worst not significantly worse than linear ([kaitchup guide](https://kaitchup.substack.com/p/a-guide-on-hyperparameters-and-training)).

**(b) What we do:** lr=2e-4 constant, no warmup.

**(c) Change:** Add `warmup_ratio=0.05` (≈3% of a 1-epoch run) and switch scheduler to `cosine` with min LR ~5e-6. Keep peak at 2e-4. The lack of warmup is the higher-risk gap — first ~50 steps of training a fresh adapter at the full peak LR on bf16 MPS is a known NaN-loss generator and undermines the very thing we use the eval-every-500 for.

**(d) Confidence:** High on adding warmup. Medium on cosine vs linear (sources say they're close).

### Verdict
**Add warmup_ratio=0.05 and cosine decay. Keep lr=2e-4 peak.**

---

## 2. LoRA rank / alpha / dropout

**(a) Established practice:**
- Unsloth: "keep rank low, 4–8 works best; range 4–64; rank should be bigger for smaller models or more complex datasets." Alpha **equal to rank, or 2× rank** (so α/r = 1 or 2). Dropout 0 for 4-bit QLoRA; otherwise small (~0.05–0.1) ([Unsloth LoRA Hyperparameters Guide](https://docs.unsloth.ai/get-started/fine-tuning-llms-guide/lora-hyperparameters-guide)).
- Thinking Machines: in their experiments **r=1 to r=512 all matched FullFT** on datasets within LoRA's "capacity," with capacity governed mostly by dataset size, not by model size. A **rank-32 adapter on a 7B model matched FullFT up to ~50k examples**. Larger ranks help only when the dataset demands more capacity ([Thinking Machines blog](https://thinkingmachines.ai/blog/lora/)).
- Common Unsloth notebook configs for Qwen3: r=8/α=8 (4-bit) or r=24/α=32 (bf16).
- Dropout 0.05 is the canonical PEFT default and is fine for non-quantized runs.

**(b) What we do:** r=32, α=64 (α/r = 2), dropout=0.05.

**(c) Change:** r=32 is **defensible but on the high end** for a 4B model on a tool-call domain. Our dataset is in the 20–90k row range — right at the boundary where the Thinking Machines paper says r=32 stops being free. Options:
1. **Hold at r=32, α=64 (current).** Safe — they explicitly verified r=32 matches FullFT up to 50k examples.
2. **Drop to r=16, α=32.** Halves trainable params, halves the optimizer state, and is what most recent Qwen3-4B community recipes use. Risk: minor underfit if dataset has high diversity.
3. **Drop to r=8, α=16 (Unsloth's preferred low end).** Most aggressive; only worth it if we see no eval-loss penalty.

Dropout 0.05 is fine — keep it.

**(d) Confidence:** Medium. The "right" rank is dataset-dependent and we should test rather than assume.

### Verdict
**Hold r=32/α=64 for v4.5 (no evidence we need more), and add a r=16/α=32 A/B as a "free win" candidate for v4.6.**

---

## 3. target_modules

**(a) Established practice:**
- HF PEFT default and Unsloth recommend the **full set** for attention + MLP: `["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]`. The `all-linear` shortcut produces approximately this set on Qwen ([PEFT LoRA developer guide](https://huggingface.co/docs/peft/developer_guides/lora), [Aionlinecourse](https://www.aionlinecourse.com/blog/target-modules-for-applying-peft-lora-on-different-models)).
- Thinking Machines is **emphatic**: attention-only underperformed FullFT by **5–15% on downstream metrics even at r=64**; adding MLP adapters closes the gap entirely. They name this directly as one of their two main findings ([Thinking Machines blog](https://thinkingmachines.ai/blog/lora/)).
- The older "q_proj + v_proj only" recipe (from the original LoRA paper) survives in many tutorials but is widely treated as outdated for modern LLM adapter tuning.

**(b) What we do:** `all-linear` — matches both Unsloth and Thinking Machines.

**(c) Change:** No. Do **not** narrow to q_proj/v_proj. The 5–15% downstream penalty is far worse than the marginal memory savings.

**(d) Confidence:** High — multiple independent sources agree.

### Verdict
**Keep `all-linear`. Do not narrow.**

---

## 4. Batch size + grad accum

**(a) Established practice:**
- Unsloth defaults: `per_device_train_batch_size=2`, `gradient_accumulation_steps=4`, effective batch = 8 ([Unsloth LoRA Hyperparameters Guide](https://docs.unsloth.ai/get-started/fine-tuning-llms-guide/lora-hyperparameters-guide)).
- The "Beware of the Batch Size" paper finds an optimal batch size for LoRA in the **32–128 range** and that **LoRA degrades noticeably above ~128**, unlike FullFT which tolerates 256–512. The optimal batch size is **scale-invariant under rank and model size** but sensitive to total data scale, meaning a value tuned on a small run transfers ([ArXiv 2602.09492](https://arxiv.org/pdf/2602.09492)).
- Thinking Machines aligns: LoRA is less tolerant of large batches than FullFT; the gap grows past ~128.

**(b) What we do:** grad_accum=4. We don't have the per-device batch size in the task description but assuming 1–2 on MPS, our effective batch is 4–8.

**(c) Change:** Effective batch 4–8 is on the **low end** but inside the safe zone. If we have headroom on MPS we could push grad_accum to 8 (effective 8–16) to get smoother gradients. **Do not exceed effective batch 64** for a 4B / r=32 LoRA. If memory is the binding constraint on MPS we should leave it alone — bigger batches mostly buy smoother eval curves, not better final quality, at our scale.

**(d) Confidence:** Medium. The "optimal" depends on dataset size; safer to slightly increase than to stay at 4.

### Verdict
**Optional bump to grad_accum=8 if memory allows. Do not exceed effective batch 32 for v4.5.**

---

## 5. Epochs + early stopping

**(a) Established practice:**
- Unsloth: **1–3 epochs**; >3 has diminishing returns and overfits on instruction-style data. For small datasets (<2k) some sources suggest 2–3 with strict early stopping ([Unsloth LoRA Hyperparameters Guide](https://docs.unsloth.ai/get-started/fine-tuning-llms-guide/lora-hyperparameters-guide)).
- HF Trainer convention: `EarlyStoppingCallback(early_stopping_patience=3)` watching `eval_loss`, paired with `load_best_model_at_end=True`. Standard signal: stop when eval_loss does not improve for N evals. Some sources (Latitude, neptune.ai) recommend monitoring **eval_loss curvature** (plateau) rather than absolute increase, since LoRA eval_loss is often noisy.
- Common failure mode: a single eval_loss spike at step ~80% caused by a hard mini-batch, then recovery. Patience ≥ 2 is essential or you stop on a transient.

**(b) What we do:** 1.0 epoch, no early stopping, eval every 500 steps only as a NaN watchdog.

**(c) Change:** Two cheap wins:
1. **Add `EarlyStoppingCallback(early_stopping_patience=3, early_stopping_threshold=1e-3)`** so a run that's clearly diverging at, say, 60% of the way through stops instead of burning compute.
2. **Set `load_best_model_at_end=True`** with `metric_for_best_model="eval_loss"` and `greater_is_better=False`. This costs nothing and gives us the best checkpoint instead of the last one. The last-checkpoint failure mode is exactly the one that produced the v4 path-bias regression.

Keep epochs=1 for v4.5 — adding a second epoch on a 50k+ tool-call dataset is a known memorization risk and not what we need right now.

**(d) Confidence:** High on load_best_model_at_end. High on early stopping. Medium on epoch count (depends on dataset).

### Verdict
**Add EarlyStoppingCallback (patience=3) and load_best_model_at_end=True. Keep epochs=1.**

---

## 6. MAX_LENGTH

**(a) Established practice:**
- Qwen3-4B supports **32,768 tokens native, 131,072 with YaRN** — plenty of headroom ([Qwen3-4B model card](https://huggingface.co/Qwen/Qwen3-4B)).
- Unsloth's Qwen3 fine-tuning examples default to **max_seq_length = 2048** as a memory-friendly default, with 1024 only recommended for very constrained environments (Colab free tier) ([Unsloth Qwen3 guide](https://unsloth.ai/docs/models/tutorials/qwen3-how-to-run-and-fine-tune)).
- Memory cost of MAX_LENGTH=2048 vs 1024 is roughly 1.5–2× (sub-quadratic in practice with bf16 attention). Truncating multi-turn tool-call traces at 1024 is a real risk for the v3/v4 data shape — tool calls + observations + responses regularly exceed 1024 tokens on multi-step traces.

**(b) What we do:** 1024 (full tier).

**(c) Change:** **Bump to 2048.** This is the single change most likely to materially affect tool-call quality. If our dataset has any multi-turn rows >1024 tokens, we are training on truncated, incoherent supervision and the model learns to be incoherent. Audit recommendation: before bumping, run `len(tokenized[i]) for i in dataset` and report the fraction of rows >1024 and >2048. If the >1024 fraction is non-trivial (>5%), bumping to 2048 is mandatory, not optional.

If MPS memory blows up at 2048: try 1536 first, drop grad_accum to compensate.

**(d) Confidence:** High that 1024 is too low for tool-call traces. Medium on whether 2048 vs 1536 is the right landing.

### Verdict
**Audit token-length distribution, then bump to 2048 (or 1536 if MPS-bound).**

---

## 7. Optimizer choice (adamw_8bit vs adamw_torch)

**(a) Established practice:**
- `adamw_8bit` (bitsandbytes) gives ~78% optimizer-state memory reduction on CUDA with ~98% accuracy retention ([HF bitsandbytes 8-bit optimizers](https://huggingface.co/docs/bitsandbytes/main/en/optimizers)).
- **On Apple Silicon MPS, bitsandbytes is not officially supported.** Multiple sources confirm: "8-bit loading (via bitsandbytes) isn't supported on Mac" ([Medium LoRA on Apple Silicon](https://medium.com/@haldankar.deven/lora-fine-tuning-on-apple-silicon-d000ea38453c)). The `mps-bitsandbytes` community fork exists but is unmaintained/experimental ([mps-bitsandbytes GitHub](https://github.com/mpsops/mps-bitsandbytes)).
- For MPS, **`adamw_torch` is the standard.** `adafactor` is an alternative if memory is binding (it stores less optimizer state at the cost of slower convergence).

**(b) What we do:** `adamw_torch`. Correct for MPS.

**(c) Change:** None. Do **not** try `adamw_8bit` on MPS — it will either silently fall back to CPU offload (slow) or fail to import. If memory is binding, swap to `adafactor` and re-test — but it changes loss curves so it's an actual experiment, not a free swap.

**(d) Confidence:** High that adamw_8bit is the wrong choice on MPS. High that adamw_torch is the right baseline.

### Verdict
**Keep adamw_torch. Do not adopt adamw_8bit on MPS.**

---

## 8. Eval cadence

**(a) Established practice:**
- Unsloth: **eval_steps=0.2** is shown as a common pattern (eval at 20% intervals → 5 evals per run). They explicitly warn that `eval_steps=1` ruins throughput. Suggest reducing eval dataset to ~100 rows to keep evals cheap, plus `fp16_full_eval=True` to halve eval memory ([Unsloth FAQs](https://docs.unsloth.ai/basics/troubleshooting-and-faqs)).
- HF Trainer convention: 5–10 evals per epoch is typical when actually using eval_loss for early stopping. Far less often when eval is only a NaN watchdog.

**(b) What we do:** every 500 steps (NaN-only). With typical 20k–90k row datasets and effective batch 4–8, that's ~30–90 evals per epoch — far too many if we're not using eval, and too few if we are.

**(c) Change:** If we adopt early stopping (topic 5), evaluating every 500 steps is **fine and matches Unsloth's "5–10 evals" guidance** for our dataset size. The action item is to make sure the eval dataset is small (~100–500 rows) so each eval is cheap. **Cap eval set size at 500 rows.** This is the actual lever.

**(d) Confidence:** Medium. Depends on what the v4 eval dataset actually looks like — we should check before changing.

### Verdict
**Keep eval_steps=500 but cap eval dataset at ≤500 rows.**

---

## 9. Per-task vs generalist adapters

**(a) Established practice:**
- The single-adapter / multi-task literature is mixed. Thinking Machines is the strongest recent voice for a single generalist adapter at sufficient rank — they explicitly find one adapter with good rank matches multiple per-task adapters and avoids the routing overhead ([Thinking Machines blog](https://thinkingmachines.ai/blog/lora/), [Align Don't Divide](https://arxiv.org/pdf/2508.05078)).
- Multi-adapter hot-swap reports (Medina 14B benchmark, others) show **13–18 ms swap latency** at inference once a base model is resident, which is negligible relative to a tool-call round trip ([Medina hot-swap benchmark](https://gist.github.com/synchronic1/22ad2e229fe760f0ccd5313f53adea59)).
- Per-task adapters are most valuable when tasks have **genuinely conflicting objectives** (catastrophic forgetting risk) or when one task is much smaller than the others (dataset imbalance).
- Per-task adapters add **operational complexity** — routing, version management, eval drift — that is rarely worth it for <5 distinct tasks.

**(b) What we do:** single generalist adapter for tool calling. Matches Thinking Machines.

**(c) Change:** No change for v4.5. Revisit only if we see evidence of one tool family bleeding into another in eval (e.g., file_read patterns leaking into git_commit responses). The complexity cost of multi-adapter is real and we don't have the eval infrastructure to A/B them cleanly yet.

**(d) Confidence:** Medium. The literature mildly favors single-adapter for our scale; multi-adapter is a future-work option.

### Verdict
**Keep single generalist adapter. Revisit only on observed cross-task bleed.**

---

## 10. Path / project / identifier bias avoidance (v4 regression prevention)

**(a) Established practice:**
- Memorization in LoRA SFT is real but **substantially lower than full-FT** at matched performance ([ArXiv 2506.20856](https://arxiv.org/pdf/2506.20856)). The dominant levers, in order of impact:
  1. **Deduplication of the training set.** "Deduplicating Training Data Makes Language Models Better" — exact-substring dedup before training reduces memorization rate by ~10× without hurting quality ([ArXiv 2107.06499](https://arxiv.org/pdf/2107.06499)). For tool-call data, dedup keys should include the prompt + the tool call's stringified args, since identical prompts with identical args are pure memorization fodder.
  2. **Diversity of identifiers.** If 60% of rows reference `/Users/mz/_CODING/agentMemory/...`, the adapter learns the path as part of the tool-call grammar. Counterweight options: (a) strip / placeholderize project paths before training, (b) augment with synthetic alternate paths, (c) downsample over-represented projects to a cap.
  3. **Hard-negative coverage.** Including counter-examples where a path-like string is *not* the right tool argument forces the model to attend to context, not surface tokens.
  4. **Dropout 0.05–0.1.** Standard PEFT regularizer. We already have this.
  5. **Weight decay 0.01.** Standard. Add if not present.
  6. **Lower epochs.** More epochs = more memorization. Holding at 1 (current) is correct for memorization control.
  7. **Eval out-of-domain.** Unsloth and others stress: **eval on data the adapter has never seen including paths/projects it has never seen.** If our eval dataset shares the same project paths as training, we won't see the regression in the metric. ([Unsloth LoRA Hyperparameters Guide](https://docs.unsloth.ai/get-started/fine-tuning-llms-guide/lora-hyperparameters-guide))

**(b) What we do:** Per the v2 data audit memory note, mem_projects is over-fragmented and we have no path-stripping or downsampling step. Eval is presumed in-distribution (worth confirming).

**(c) Change:** This is the **most important section of this brief.** Recommended additions to the v4.5 data pipeline:
1. Dedup on `(rendered_prompt + tool_call_json)` hash. Drop exact dupes.
2. Cap any single `mem_projects` value at N% of the dataset (N=5 is a defensible start).
3. Add a path-normalization pass: replace `/Users/mz/_CODING/<project>` with `<PROJECT_ROOT>` in 50% of rows (keep 50% raw to preserve realism).
4. Add `weight_decay=0.01` to TrainingArguments if not already set.
5. Hold out an **OOD eval slice** — rows whose `mem_projects` value does not appear in train. Track eval_loss on this slice separately. If train eval_loss decreases but OOD eval_loss increases, that *is* the memorization signal.

**(d) Confidence:** High on dedup. High on weight decay. High on OOD eval slice. Medium on path normalization (50/50 is a heuristic, not from a source).

### Verdict
**Dedup + per-project cap + OOD eval slice + weight_decay=0.01. This is the v4 regression fix.**

---

## Top 5 changes to adopt before v4.5

Prioritized by (impact on the v4 regression / cost to implement) ratio.

1. **Hold-out OOD eval slice + dedup train set.** No knob change — pure data pipeline work. Catches the path-bias regression before it ships. (Topic 10.)
2. **Bump MAX_LENGTH from 1024 → 2048** after auditing token-length distribution. Tool-call traces routinely exceed 1024 tokens; training on truncated supervision is teaching the model to truncate. (Topic 6.)
3. **Add warmup_ratio=0.05 + cosine LR decay.** Eliminates first-50-steps instability that motivates the NaN-only eval cadence in the first place. (Topic 1.)
4. **Add EarlyStoppingCallback(patience=3) + load_best_model_at_end=True + weight_decay=0.01.** Three lines in TrainingArguments. Saves compute on diverging runs and prevents shipping a worse-than-best checkpoint. (Topics 5, 10.)
5. **Cap eval dataset at ≤500 rows.** Lets us actually use eval_loss for stopping without paying the throughput tax. (Topic 8.)

### Explicitly NOT changing for v4.5

- **target_modules** stays `all-linear` (topic 3, high confidence)
- **rank/alpha/dropout** stays at r=32/α=64/0.05 (topic 2, save the r=16 A/B for v4.6)
- **optimizer** stays adamw_torch on MPS (topic 7)
- **single generalist adapter** (topic 9)
- **epoch count** stays at 1 (topic 5)

### Unresolved / sources conflict

- **bf16 on MPS:** one community source claims bf16 is "blocked on MPS, fp16 gives NaN," but this contradicts the PyTorch 2.1+ MPS bf16 support and our current working bf16 baseline. Treat that source as out-of-date and continue using bf16. Flag for re-check if NaN losses reappear after the warmup change.
- **Exact "optimal" rank** for our specific dataset shape — Thinking Machines says r=32 matches FullFT to 50k examples, but our dataset may be at or above that boundary. The right answer is the v4.6 A/B, not a guess now.

---

*Sources: see "Source map" section. Where a single fact has only one citation, treat as medium confidence and verify before relying on it for a production decision.*
