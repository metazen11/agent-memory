#!/usr/bin/env python3
"""Tools-free command-line bridge to the installed Anvil inference engine.

Two modes, same request/response envelope:

- one-shot (default): read ONE JSON request from stdin, write ONE JSON
  response to stdout, exit. The model is loaded per call.
- ``--serve``: keep the process (and the loaded model) alive; read one JSON
  request PER LINE and write one JSON response per line. Exits when stdin
  closes, so it never outlives its parent. Used by app/anvil_enrichment.py's
  warm bridge so repeated calls skip the multi-second model load.

The model is whatever ANVIL_MODEL_BACKEND / ANVIL_MODEL_PATH /
ANVIL_MODEL_NAME say in this process's environment (callers pin them).
"""

import contextlib
import json
import os
import sys
from pathlib import Path


def _provider(settings) -> str:
    # Local backends (mlx) are identified by model_path; model_name is empty
    # when a caller pins ANVIL_MODEL_NAME="" (app/lesson_condense.py).
    model = settings.model_name or Path(settings.model_path).name
    return f"anvil:{settings.model_backend}:{model}"


def _load_engine():
    root = Path(os.environ.get("AGENT_MEMORY_ANVIL_ROOT", "/opt/anvil"))
    sys.path.insert(0, str(root))
    from anvil.config import settings
    from anvil.llm import raw_logging
    from anvil.llm.engine import chat_completion

    # No agent runner, hook lifecycle, memory calls, task scheduling or tools.
    # Do not retain a second raw copy of potentially sensitive memory data.
    raw_logging.log_raw_llm_interaction = lambda **kwargs: None
    settings.mlx_thinking_enabled = False
    # Anvil's shared LLM daemon (:3399) is keyed by backend only, not by
    # model path, so it could answer with Anvil's global model. Never use it.
    settings.llm_daemon_enabled = False
    return settings, chat_completion


def _complete(settings, chat_completion, request: dict) -> dict:
    result = chat_completion(
        messages=request["messages"],
        tools=[],
        max_tokens=900,
        temperature=0.1,
    )
    if result.get("tool_calls"):
        raise ValueError("Enrichment requested tools")
    return {"content": result.get("content", ""), "provider": _provider(settings)}


def main():
    out = sys.stdout
    # Import/inference chatter must not corrupt the JSON protocol on stdout.
    with contextlib.redirect_stdout(sys.stderr):
        settings, chat_completion = _load_engine()
        if "--serve" not in sys.argv[1:]:
            response = _complete(settings, chat_completion, json.load(sys.stdin))
            json.dump(response, out)
            return
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                response = _complete(settings, chat_completion, json.loads(line))
            except Exception as error:  # reported to the caller, loop survives
                response = {"error": f"{type(error).__name__}: {error}"}
            out.write(json.dumps(response) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
