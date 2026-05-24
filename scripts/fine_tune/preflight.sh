#!/bin/bash
# Pre-flight gate for training. Run before any DATASET_TIER=full job.
#
# Checks (in order — fails fast):
#  - models/ and .venv-finetune/ are real local dirs, NOT Dropbox symlinks
#    (root cause of 2026-05-15 v3 mid-run kill — see docs/training_runs/)
#  - disk free on the volume holding `models/` (~30GB for full 3B run)
#  - Dropbox daemon is NOT running (will corrupt artifacts on sync)
#  - caffeinate(8) is available (run wrapper needs it to prevent sleep)
#  - models/llama.cpp/build/bin/llama-cli + llama-quantize built
#  - base model REVISION.txt exists for the slug
#  - Python venv exists
#  - dataset files exist (parameterized by FAMILY/VERSION)
#  - dataset has zero rows that produce zero predicted tokens under the
#    trainer's assistant-only label mask (the NaN-eval gate, ref Fix #10
#    in build_v3_dataset.py — 2026-05-16 incident)
#
# Usage:
#   # default (legacy) — qwen2.5-3b-instruct + qwen25_tools/v1
#   scripts/fine_tune/preflight.sh
#
#   # v3 — qwen3-4b + qwen3_tools/v3
#   scripts/fine_tune/preflight.sh qwen3-4b qwen3_tools v3
#
#   # env-var overrides also accepted
#   DATASET_FAMILY=qwen3_tools DATASET_VERSION=v3 \
#       scripts/fine_tune/preflight.sh qwen3-4b
#
# Skip the zero-label gate (it loads the tokenizer + scans the full
# dataset, takes ~30s on the full v3 dataset):
#   SKIP_LABEL_GATE=1 scripts/fine_tune/preflight.sh ...
set -euo pipefail
cd "$(dirname "$0")/../.."

SLUG="${1:-qwen2.5-3b-instruct}"
DATASET_FAMILY="${2:-${DATASET_FAMILY:-qwen25_tools}}"
DATASET_VERSION="${3:-${DATASET_VERSION:-v1}}"
MIN_FREE_GB="${MIN_FREE_GB:-30}"
SKIP_LABEL_GATE="${SKIP_LABEL_GATE:-0}"

ok() { printf "  \033[32m✓\033[0m %s\n" "$*"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$*"; FAIL=1; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }

FAIL=0
echo "=== Pre-flight: $SLUG ($DATASET_FAMILY/$DATASET_VERSION) ==="

# --- A. models/ and .venv-finetune/ MUST NOT be Dropbox symlinks ----------
for path in models .venv-finetune; do
    if [ -L "$path" ]; then
        TARGET=$(readlink "$path")
        if echo "$TARGET" | grep -qi dropbox; then
            fail "$path is a symlink into Dropbox ($TARGET) — move it local first"
        else
            warn "$path is a symlink ($TARGET) — not Dropbox, acceptable"
        fi
    elif [ -d "$path" ]; then
        ok "$path: real local dir"
    else
        fail "$path: missing"
    fi
done

# --- B. Disk free ----------------------------------------------------------
FREE_GB=$(df -Pg . | awk 'NR==2 {print $4}')
if [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
    fail "disk free: ${FREE_GB}GB (need ≥${MIN_FREE_GB}GB)"
else
    ok "disk free: ${FREE_GB}GB"
fi

# --- C. Dropbox (must be down for training) --------------------------------
if pgrep -f "Dropbox.app/Contents/MacOS/Dropbox\$" > /dev/null; then
    warn "Dropbox is running — will corrupt artifacts on sync. Quitting..."
    osascript -e 'tell application "Dropbox" to quit' || true
    sleep 3
    if pgrep -f "Dropbox.app/Contents/MacOS/Dropbox\$" > /dev/null; then
        fail "Dropbox still running after quit attempt"
    else
        ok "Dropbox quit"
    fi
else
    ok "Dropbox not running"
fi

# --- D. caffeinate available (used by launch_v3.sh to block sleep) -------
if command -v caffeinate > /dev/null 2>&1; then
    ok "caffeinate available"
else
    fail "caffeinate(8) not found — required to prevent sleep mid-run"
fi

# --- E. llama.cpp ----------------------------------------------------------
if [ -x models/llama.cpp/build/bin/llama-cli ]; then
    ok "llama-cli present"
else
    fail "llama-cli not built at models/llama.cpp/build/bin/llama-cli"
fi
if [ -x models/llama.cpp/build/bin/llama-quantize ]; then
    ok "llama-quantize present"
else
    fail "llama-quantize not built"
fi

# --- F. Base model ---------------------------------------------------------
REV="models/base/${SLUG}/REVISION.txt"
if [ -f "$REV" ]; then
    ok "base model: $(cat "$REV" | head -c 12)…"
else
    fail "base not downloaded: run scripts/fine_tune/download_base.py $SLUG"
fi

# --- G. Python venv --------------------------------------------------------
if [ -x ".venv-finetune/bin/python" ]; then
    ok "venv: .venv-finetune"
else
    fail "venv not found at .venv-finetune"
fi

# --- H. Dataset files ------------------------------------------------------
DATASET_DIR="data/processed/${DATASET_FAMILY}/${DATASET_VERSION}"
for f in train.chat.jsonl valid.chat.jsonl train.tiny.jsonl valid.tiny.jsonl MANIFEST.json; do
    if [ -f "$DATASET_DIR/$f" ]; then
        ok "dataset: $DATASET_DIR/$f"
    else
        fail "dataset missing: $DATASET_DIR/$f"
    fi
done

# --- I. Zero-label gate (the new one — Fix #10 verifier) ----------------
# This is the gate that would have caught the 2026-05-16 NaN-eval failure.
# Only run if dataset files all exist and the venv works.
if [ "$FAIL" -eq 0 ] && [ "$SKIP_LABEL_GATE" != "1" ]; then
    echo "  (running zero-label gate — this loads tokenizer + scans dataset, ~30s)"
    GATE_OUT=$(.venv-finetune/bin/python - <<PY
import json, sys
from pathlib import Path
sys.path.insert(0, 'scripts/fine_tune')
from transformers import AutoTokenizer

SLUG = "$SLUG"
DSET = Path("$DATASET_DIR")
TOK_BASE = Path(f"models/base/{SLUG}")
MAX_LEN = 1024
HEADER = "<|im_start|>assistant"
END = "<|im_end|>"

tok = AutoTokenizer.from_pretrained(str(TOK_BASE), use_fast=True, local_files_only=True, trust_remote_code=False)
if tok.pad_token is None: tok.pad_token = tok.eos_token

def row_predicted_tokens(row):
    text = tok.apply_chat_template(row['messages'], tools=row.get('tools'), tokenize=False, add_generation_prompt=False)
    enc = tok(text, truncation=True, max_length=MAX_LEN, padding=False, return_offsets_mapping=True)
    offs = enc['offset_mapping']
    spans = []; cur = 0
    while True:
        i = text.find(HEADER, cur)
        if i < 0: break
        cs = i + len(HEADER)
        if cs < len(text) and text[cs] == '\n': cs += 1
        j = text.find(END, cs)
        if j < 0: j = len(text)
        spans.append((cs, j)); cur = j + len(END)
    if not spans: return 0
    n = 0
    for s, e in offs:
        if s == e: continue
        for ss, ee in spans:
            if s >= ss and e <= ee:
                n += 1; break
    return n

results = {}
for split in ('train.chat.jsonl', 'valid.chat.jsonl'):
    total = bad = 0
    for line in (DSET / split).open():
        line = line.strip()
        if not line: continue
        total += 1
        if row_predicted_tokens(json.loads(line)) == 0:
            bad += 1
    results[split] = (total, bad)

bad_total = sum(b for _, b in results.values())
for split, (total, bad) in results.items():
    pct = 100*bad/max(total,1)
    print(f"{split}: total={total}, zero_label={bad} ({pct:.2f}%)")
print(f"SUMMARY: total_bad_rows={bad_total}")
sys.exit(1 if bad_total > 0 else 0)
PY
    ) && GATE_RC=0 || GATE_RC=$?
    echo "$GATE_OUT" | sed 's/^/    /'
    if [ "$GATE_RC" -eq 0 ]; then
        ok "zero-label gate (Fix #10): 0 bad rows"
    else
        fail "zero-label gate (Fix #10): bad rows present — rebuild dataset (see build_v3_dataset.py Fix #10)"
    fi
fi

# --- J. Data-quality audit (path bias, agentic-narrative fabrication) ----
# Sees content patterns the builder can't easily catch row-by-row.
# Refuses pass if any threshold in docs/fine_tune/DATA_QUALITY_GATES.md
# is exceeded. Skip with SKIP_AUDIT=1 (tiny smoke tests only).
if [ "$FAIL" -eq 0 ] && [ "${SKIP_AUDIT:-0}" != "1" ]; then
    echo "  (running data-quality audit — Categories 1 + 8a)"
    if .venv-finetune/bin/python scripts/fine_tune/audit_dataset.py "$DATASET_DIR" > /tmp/preflight-audit.log 2>&1; then
        ok "data-quality audit: all categories within threshold"
    else
        cat /tmp/preflight-audit.log | sed 's/^/    /'
        fail "data-quality audit: failures present — see docs/fine_tune/DATA_QUALITY_GATES.md"
    fi
fi

echo
if [ "$FAIL" -eq 0 ]; then
    echo "PASS — pre-flight green."
    exit 0
else
    echo "FAIL — see failures above."
    exit 1
fi
