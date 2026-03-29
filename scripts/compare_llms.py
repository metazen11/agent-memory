#!/usr/bin/env python3
"""Side-by-side comparison of observation LLMs: local GGUF vs Anthropic Haiku.

Pulls pending queue items and runs both LLMs on each, printing a comparison table.

Usage:
    .venv/bin/python scripts/compare_llms.py
    .venv/bin/python scripts/compare_llms.py --count 5
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.observation_llm import (
    build_user_prompt,
    build_chat_prompt,
    parse_llm_response,
    SKIP_TOOLS,
    SYSTEM_PROMPT,
)


# ── Local GGUF ────────────────────────────────────────────────

def run_local(prompt: str, llm) -> tuple[dict | None, float]:
    t0 = time.time()
    out = llm(prompt, max_tokens=300, stop=["<|im_end|>"], temperature=0)
    elapsed = time.time() - t0
    llm.reset()
    text = out["choices"][0]["text"]
    return parse_llm_response(text), elapsed


# ── Anthropic Haiku ───────────────────────────────────────────

async def run_haiku(user_prompt: str) -> tuple[dict | None, float]:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    for attempt in range(3):
        try:
            t0 = time.time()
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            elapsed = time.time() - t0
            return parse_llm_response(message.content[0].text), elapsed
        except anthropic.RateLimitError:
            wait = 15 * (attempt + 1)
            print(f"    [rate limited, waiting {wait}s...]")
            await asyncio.sleep(wait)
    return None, 0.0


# ── Main ──────────────────────────────────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()

    # Load test cases from DB
    import asyncpg
    dsn = settings.effective_database_url.replace("postgresql://", "postgres://", 1)
    conn = await asyncpg.connect(dsn)
    rows = await conn.fetch("""
        SELECT id, tool_name, tool_input, tool_response_preview, cwd, last_user_message
        FROM mem_observation_queue
        WHERE status = 'pending' AND tool_name NOT IN ({})
        ORDER BY created_at DESC
        LIMIT $1
    """.format(",".join(f"'{t}'" for t in SKIP_TOOLS)), args.count)
    await conn.close()

    if not rows:
        print("No pending queue items to test")
        return

    # Load local LLM
    from llama_cpp import Llama
    print(f"Loading local 7B model...")
    llm = Llama(
        model_path=settings.observation_llm_model,
        n_ctx=4096,
        n_threads=4,
        n_gpu_layers=-1,
        verbose=False,
    )
    print(f"Loaded. Testing {len(rows)} items...\n")

    # Run both on each item
    sep = "=" * 100
    results = {"local_wins": 0, "haiku_wins": 0, "ties": 0,
               "local_skip": 0, "haiku_skip": 0,
               "local_times": [], "haiku_times": []}

    for i, row in enumerate(rows):
        tool_input = json.loads(row["tool_input"]) if row["tool_input"] else None
        user_prompt = build_user_prompt(
            tool_name=row["tool_name"],
            tool_input=tool_input,
            tool_response_preview=row["tool_response_preview"],
            cwd=row["cwd"],
            last_user_message=row["last_user_message"],
        )

        # Run both
        chat_prompt = build_chat_prompt(user_prompt)
        local_result, local_time = run_local(chat_prompt, llm)
        haiku_result, haiku_time = await run_haiku(user_prompt)

        results["local_times"].append(local_time)
        results["haiku_times"].append(haiku_time)

        print(sep)
        print(f"[{i+1}/{len(rows)}] Queue #{row['id']} | Tool: {row['tool_name']}")
        print(f"  CWD: {row['cwd'] or '?'}")
        input_preview = (row["tool_input"] or "")[:120]
        print(f"  Input: {input_preview}...")
        print()

        # Local result
        print(f"  LOCAL 7B ({local_time:.1f}s):")
        if local_result is None:
            print(f"    [SKIPPED]")
            results["local_skip"] += 1
        else:
            print(f"    Title:     {local_result.get('title', '?')}")
            print(f"    Type:      {local_result.get('type', '?')}")
            print(f"    Narrative: {(local_result.get('narrative') or '?')[:120]}")
            facts = local_result.get("facts", [])
            if facts:
                print(f"    Facts:     {'; '.join(str(f) for f in facts[:3])}")

        print()

        # Haiku result
        print(f"  HAIKU ({haiku_time:.1f}s):")
        if haiku_result is None:
            print(f"    [SKIPPED]")
            results["haiku_skip"] += 1
        else:
            print(f"    Title:     {haiku_result.get('title', '?')}")
            print(f"    Type:      {haiku_result.get('type', '?')}")
            print(f"    Narrative: {(haiku_result.get('narrative') or '?')[:120]}")
            facts = haiku_result.get("facts", [])
            if facts:
                print(f"    Facts:     {'; '.join(str(f) for f in facts[:3])}")

        # Quick quality comparison
        local_ok = local_result is not None and local_result.get("title") not in ("Untitled", "Short descriptive title", None)
        haiku_ok = haiku_result is not None and haiku_result.get("title") not in ("Untitled", "Short descriptive title", None)

        if local_ok and haiku_ok:
            # Both produced output — check if both agree on skip-worthiness
            results["ties"] += 1
            verdict = "TIE"
        elif local_ok and not haiku_ok:
            results["local_wins"] += 1
            verdict = "LOCAL WINS"
        elif haiku_ok and not local_ok:
            results["haiku_wins"] += 1
            verdict = "HAIKU WINS"
        else:
            results["ties"] += 1
            verdict = "BOTH SKIPPED"

        print(f"\n  >> {verdict}")
        print()

        # Throttle Haiku (free tier = 5 RPM)
        await asyncio.sleep(13.0)

    # Summary
    print(sep)
    print(f"\n{'SUMMARY':=^60}")
    print(f"  Items tested:   {len(rows)}")
    print(f"  Local wins:     {results['local_wins']}")
    print(f"  Haiku wins:     {results['haiku_wins']}")
    print(f"  Ties:           {results['ties']}")
    print(f"  Local skips:    {results['local_skip']}")
    print(f"  Haiku skips:    {results['haiku_skip']}")
    avg_local = sum(results["local_times"]) / len(results["local_times"])
    avg_haiku = sum(results["haiku_times"]) / len(results["haiku_times"])
    print(f"  Avg local time: {avg_local:.1f}s")
    print(f"  Avg Haiku time: {avg_haiku:.1f}s")
    print()


if __name__ == "__main__":
    asyncio.run(main())
