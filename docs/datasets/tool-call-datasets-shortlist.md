# Tool-call datasets shortlist — v2 training-mix candidates

Curated 2026-07-07 from a Hugging Face Hub search, for strengthening the
WFCA Qwen3-4B tool-call model beyond the in-house 5k v5-pilot dataset.

## Design principle (read first)
Our model's differentiator is **project-conditioned** agent traces (fire-map,
agentMemory, real WFCA paths). Generic function-calling data adds robustness
but, in excess, DILUTES that conditioning — the same cross-project
hallucination that plagued v4. Recipe: **in-house 5k as the CORE + a curated
slice of general data**, not a generic flood. Match our Hermes/ChatML +
Qwen3 template.

## Tier 1 — use
| Dataset | Size | Role | License | Link |
|---|---|---|---|---|
| Salesforce/xlam-function-calling-60k | 60k | Gold standard, APIGen *verified* executable calls, parallel/multiple | cc-by-4.0 (🔒 gated — request access) | https://hf.co/datasets/Salesforce/xlam-function-calling-60k |
| NousResearch/hermes-function-calling-v1 | ~15k | **Same Hermes/ChatML format as our model** — lowest-friction merge | apache-2.0 | https://hf.co/datasets/NousResearch/hermes-function-calling-v1 |
| gorilla-llm/Berkeley-Function-Calling-Leaderboard | eval | **EVAL only** — *the* tool-call benchmark; score our model vs baselines | apache-2.0 | https://hf.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard |

## Tier 2 — consider
| Dataset | Size | Note | Link |
|---|---|---|---|
| minpeter/xlam-function-calling-60k-parsed | 60k | xLAM pre-parsed to parquet w/ multi-turn/parallel subsets — avoids reprocessing the gated original | https://hf.co/datasets/minpeter/xlam-function-calling-60k-parsed |
| nvidia/Nemotron-RL-Agentic-Function-Calling-Pivot-v1 | 1-10k | RL trajectories (Mar 2026) — only if we go beyond SFT | https://hf.co/datasets/nvidia/Nemotron-RL-Agentic-Function-Calling-Pivot-v1 |
| fireworks-ai/function-calling-eval-dataset-v0 | <1k | Small single/multi-turn eval set — supplementary to BFCL | https://hf.co/datasets/fireworks-ai/function-calling-eval-dataset-v0 |

## Not recommended as-is
- glaiveai/glaive-function-calling-v2 & its many reformats (Locutusque, hiyouga,
  lilacai, hypervariance): large + popular but older (2023), synthetic, and its
  format/quality is below xLAM/Hermes. Skip unless a specific gap needs it.

## Next step (v2 dataset build — REQUIRES REVIEW before training)
1. Request access to the gated Salesforce/xlam (or use minpeter's parsed mirror).
2. Curate a slice (~5-10k) of Hermes + xLAM, converted to our Qwen3 template.
3. Blend: in-house 5k (core, project-conditioned) + curated general slice.
4. Hold out BFCL for eval — get real before/after numbers vs the v5-pilot model.
5. Ablate the blend ratio; too much generic data = cross-project dilution risk.
