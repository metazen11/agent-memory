# LM Studio Integration

How to load a fine-tuned GGUF in LM Studio and verify native tool calling
works.

## Why this matters

Training and merging produces a GGUF; the only way to know it really works
is to load it in the consumer tool you care about. LM Studio is the most
common: it has an OpenAI-compatible local server that parses Hermes-style
`<tool_call>` blocks into the OpenAI `tool_calls` API response field. If
LM Studio renders a tool-call card in its chat UI, the model is shipping
correctly.

## Setup (one-time)

1. Install LM Studio (https://lmstudio.ai).
2. Settings → Developer → enable "Local LLM Service".
3. **LM Studio models directory:** `~/.lmstudio/models/<publisher>/<repo>/` —
   **NOT** `~/.cache/lm-studio/` (that was the old layout, will be silently
   ignored). Use any subdir name for `<publisher>` (e.g. `mz` for your own
   locally-trained models).

## Load the GGUF

Either:

- Drag-and-drop the `.gguf` file from Finder into LM Studio's "My Models"
  tab, **or**
- Use the smoke-test script:
  ```bash
  scripts/fine_tune/lmstudio_smoke.sh models/gguf/qwen2.5-3b-toolcalls-q4km.gguf 0.5
  ```
  This copies the GGUF into `~/.lmstudio/models/mz/qwen25-toolcalls/`
  then prompts you for the manual steps below. **You may need to restart
  LM Studio** for the new model to appear in the My Models list.

## Start the OpenAI-compatible server

1. LM Studio → "Local Server" tab.
2. Pick the qwen25-toolcalls model from the dropdown.
3. Click "Start Server" — defaults to `http://localhost:1234`.
4. Verify:
   ```bash
   curl -s http://localhost:1234/v1/models | jq '.data[].id'
   ```

## Verify tool calling

LM Studio's server parses native Hermes `<tool_call>` blocks back into the
OpenAI Chat Completions `tool_calls` response field. You don't need a
plugin or special config — it Just Works if the model was trained on the
Qwen 2.5 tool-call format.

Quick test from a terminal:

```bash
curl -s http://localhost:1234/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen25-toolcalls",
    "messages": [{"role": "user", "content": "Read the file at /etc/hostname"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "Read",
        "description": "Read a file from disk",
        "parameters": {
          "type": "object",
          "properties": {"file_path": {"type": "string"}},
          "required": ["file_path"]
        }
      }
    }]
  }' | jq '.choices[0].message'
```

**Expected:** `tool_calls` array populated with `{"name": "Read",
"arguments": "{\"file_path\": ...}"}`. If you see `content` populated with
raw `<tool_call>` text instead, LM Studio's parser didn't recognize the
format — see Failure Modes below.

## Run the validator against LM Studio

```bash
.venv-finetune/bin/python scripts/fine_tune/validate_tool_calls.py \
  --backend openai \
  --base-url http://localhost:1234/v1 \
  --model qwen25-toolcalls \
  --min-parse-rate 0.5
```

The validator runs 10 prompts × the temperatures you pass it, then prints
parse-rate per suite (in-distribution + natural). Bonus signal: the
`native_tool_calls` counter — if non-zero, LM Studio's server **parsed
Hermes format into structured tool_calls** rather than just returning raw
text. That's the production-grade pass.

## Failure modes

### LM Studio returns raw `<tool_call>` text in `content`, not `tool_calls`

Most likely: the model isn't emitting the exact wire format LM Studio
expects. Qwen 2.5's chat template wraps tool calls as:

```
<tool_call>
{"name": "...", "arguments": {...}}
</tool_call>
```

If the model emits `<function_call>` or some other tag, LM Studio won't
parse it. Verify training data uses the same format `apply_chat_template`
produces — see `fine-tune/restructure_to_qwen_tools.py`.

### Model loads but generates gibberish

Architecture mismatch in llama.cpp. Check the GGUF's tensor names — pure
Qwen 2.5 has only `blk.N.{attn,ffn}_*` tensors, no `ssm_*` (Mamba SSM)
prefixes. If you see `ssm_*`, the base model was a hybrid arch and won't
work in LM Studio's stock llama.cpp build.

### LM Studio server returns 422 / "tools not supported"

Older LM Studio versions disable tool calling unless you toggle "Tool Use"
in the server config. As of LM Studio 0.3+, it's on by default for any
model with a chat template that includes a `tools` block.

### Schema mismatch — wrong arg names

The trained model emits whatever it saw in the dataset. If your dataset
called the path arg `file_path` but your tool definition says `path`,
LM Studio's UI may reject the call. Keep schemas consistent end-to-end:
the schema in your `tools=` definition should match the keys in the
training-set's `tool_calls[].function.arguments`.

## See also

- `scripts/fine_tune/lmstudio_smoke.sh` — automation
- `docs/fine_tune/PIPELINE_RUNBOOK.md` — Phase 7 of the pipeline
- `docs/fine_tune/FAILURE_MODES.md` — broader failure catalog
