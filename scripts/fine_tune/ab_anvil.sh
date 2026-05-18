#!/usr/bin/env bash
# A/B harness using `anvil run` as the agentic driver.
#
# Why this exists:
#   v3 retracted because tool_response adaptation regressed. We need a
#   real agentic test (multi-turn, tool use, post-tool-response reasoning),
#   not just single-shot tool-call parsing. `anvil run` already provides
#   that loop — no need to rebuild it.
#
# Strategy:
#   For each model in {v1, v3, v4}:
#     1. anvil model set --path <gguf>
#     2. For each prompt, anvil run -p "<prompt>" --max-iter 5
#     3. Capture stdout + log to runs/<model>/<i>.log
#   Then score each run via ab_anvil_score.py:
#     - useful_answer:           final agent response addresses prompt
#     - loop_rate:               >=3 consecutive identical tool_calls
#     - adaptation_rate:         next tool_call differs after tool_response
#     - path_correctness:        no hallucinated nonexistent paths
#
# Usage:
#   scripts/fine_tune/ab_anvil.sh                          # all models + default prompts
#   PROMPTS=path/to/prompts.txt scripts/fine_tune/ab_anvil.sh
#   MODELS="v1 v4" scripts/fine_tune/ab_anvil.sh           # skip v3
#
# Output:
#   tests/fine_tune/runs/ab-<UTC>/
#     v1/{1..N}.log
#     v3/{1..N}.log
#     v4/{1..N}.log
#     scoreboard.md    (after scoring)

set -euo pipefail
cd "$(dirname "$0")/../.."

# --- Resolve anvil binary (shell aliases don't survive into caffeinate) ---
if [ -x "${ANVIL:-}" ]; then :; \
elif [ -x /Users/mz/_CODING/anvil/.venv/bin/anvil ]; then
    ANVIL=/Users/mz/_CODING/anvil/.venv/bin/anvil
elif command -v anvil >/dev/null 2>&1; then
    ANVIL=$(command -v anvil)
else
    echo "FAIL: anvil binary not found. Set ANVIL=<path> or install anvil." >&2
    exit 1
fi
echo "Using anvil: $ANVIL"

# --- Paths to model GGUFs ---
V1_GGUF="${V1_GGUF:-models/gguf/qwen2.5-3b-toolcalls-q4km.gguf}"
V3_GGUF="${V3_GGUF:-models/gguf/qwen3-4b-toolcalls-v3-q6k.gguf}"
V4_GGUF="${V4_GGUF:-models/gguf/qwen3-4b-toolcalls-v4-q6k.gguf}"

# --- Prompts (one per line, # comments ok) ---
PROMPTS="${PROMPTS:-tests/fine_tune/fixtures/vague_prompts.txt}"
MAX_PROMPTS="${MAX_PROMPTS:-10}"
MAX_ITER="${MAX_ITER:-5}"
TEMP="${TEMP:-0.2}"
MODELS="${MODELS:-v1 v3 v4}"

# --- Output dir ---
STAMP=$(date -u +"%Y%m%dT%H%M%SZ")
OUT_DIR="tests/fine_tune/runs/ab-${STAMP}"
mkdir -p "$OUT_DIR"
echo "AB run output: $OUT_DIR"

# Save the active model so we can restore it after the run
ACTIVE_BEFORE=$("$ANVIL" model info 2>/dev/null | grep -E "path|file" | head -1 || echo "")
echo "Active model before run: $ACTIVE_BEFORE"

# Resolve model name -> gguf path
gguf_for() {
    case "$1" in
        v1) echo "$V1_GGUF" ;;
        v3) echo "$V3_GGUF" ;;
        v4) echo "$V4_GGUF" ;;
        *)  echo "unknown model: $1" >&2; exit 1 ;;
    esac
}

# Read prompts, strip comments / blanks, cap at MAX_PROMPTS.
# Bash 3.2 compatible (no mapfile).
PROMPT_FILE_TMP=$(mktemp)
trap 'rm -f "$PROMPT_FILE_TMP"' EXIT
grep -vE '^[[:space:]]*(#|$)' "$PROMPTS" | head -n "$MAX_PROMPTS" > "$PROMPT_FILE_TMP"
PROMPT_ARR=()
while IFS= read -r line; do
    PROMPT_ARR+=("$line")
done < "$PROMPT_FILE_TMP"
N="${#PROMPT_ARR[@]}"
echo "Loaded $N prompts from $PROMPTS (cap=$MAX_PROMPTS)"
if [ "$N" -eq 0 ]; then
    echo "FAIL: no prompts loaded" >&2
    exit 1
fi

# --- Run loop ---
for model in $MODELS; do
    gguf=$(gguf_for "$model")
    if [ ! -f "$gguf" ]; then
        echo "SKIP $model — GGUF not found at $gguf"
        continue
    fi
    echo
    echo "============================================================"
    echo "Model: $model  ($gguf)"
    echo "============================================================"
    "$ANVIL" model set --path "$gguf" >/dev/null
    mkdir -p "$OUT_DIR/$model"

    i=0
    for prompt in "${PROMPT_ARR[@]}"; do
        i=$((i+1))
        log="$OUT_DIR/$model/$i.log"
        echo "  [$model] [$i/$N] $prompt" | head -c 100
        echo
        # Use a non-interactive workspace dir so anvil doesn't try to
        # discover the repo we're in (which could let it read source).
        # Cap max-iter to bound runtime per prompt.
        # Redirect stderr too — anvil writes tool traces there.
        if ! "$ANVIL" run \
            -p "$prompt" \
            --max-iter "$MAX_ITER" \
            --temperature "$TEMP" \
            --workspace /tmp/ab-anvil-workspace \
            > "$log" 2>&1; then
            echo "    (anvil run exited non-zero — log captured)"
        fi
        # Record the prompt at the top of the log so the scorer can find it
        sed -i '' "1i\\
=== PROMPT: $prompt ===\\
" "$log"
    done
done

# Restore previous model if known
if [ -n "$ACTIVE_BEFORE" ]; then
    echo "(would restore previous model: $ACTIVE_BEFORE — manual: anvil model set)"
fi

echo
echo "=== Runs complete: $OUT_DIR ==="
echo "Score with:"
echo "  .venv-finetune/bin/python scripts/fine_tune/ab_anvil_score.py $OUT_DIR"
