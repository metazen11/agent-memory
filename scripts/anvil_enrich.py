#!/usr/bin/env python3
"""Tools-free command-line bridge to the installed Anvil inference engine."""

import contextlib
import json
import os
import sys
from pathlib import Path


def main():
    root = Path(os.environ.get("AGENT_MEMORY_ANVIL_ROOT", "/opt/anvil"))
    sys.path.insert(0, str(root))
    request = json.load(sys.stdin)
    # Import/inference chatter must not corrupt the JSON protocol on stdout.
    with contextlib.redirect_stdout(sys.stderr):
        from anvil.config import settings
        from anvil.llm import raw_logging
        from anvil.llm.engine import chat_completion

        # No agent runner, hook lifecycle, memory calls, task scheduling or tools.
        # Do not retain a second raw copy of potentially sensitive memory data.
        raw_logging.log_raw_llm_interaction = lambda **kwargs: None
        settings.mlx_thinking_enabled = False
        settings.llm_daemon_enabled = False
        result = chat_completion(
            messages=request["messages"],
            tools=[],
            max_tokens=900,
            temperature=0.1,
        )
    if result.get("tool_calls"):
        raise ValueError("Enrichment requested tools")
    json.dump(
        {
            "content": result.get("content", ""),
            "provider": f"anvil:{settings.model_backend}:{settings.model_name}",
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
