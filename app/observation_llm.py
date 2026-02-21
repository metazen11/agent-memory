import asyncio
import json
import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)

# Tools to skip (low-value for memory)
SKIP_TOOLS = {
    "ListMcpResourcesTool", "SlashCommand", "Skill", "TodoWrite",
    "AskUserQuestion", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
    "TaskOutput", "TaskStop", "EnterPlanMode", "ExitPlanMode",
}

SYSTEM_PROMPT = """You are an observation recorder for a coding agent's memory system.
Given a tool call and its result, extract what was LEARNED, BUILT, FIXED, or DECIDED.

Focus on:
- What the system NOW DOES differently
- What shipped or changed
- Technical discoveries, patterns, gotchas
- Decisions made and their rationale

Do NOT record:
- "Analyzed X and stored findings" - record the FINDING itself
- Empty status checks, package installs, simple file listings
- Repetitive operations that don't produce new knowledge

Respond with JSON only (no markdown fences):
{
  "skip": false,
  "title": "Short descriptive title",
  "subtitle": "One-line context",
  "type": "discovery|bugfix|feature|refactor|decision|change|pattern|gotcha",
  "narrative": "2-3 sentence description of what was learned or changed",
  "facts": ["fact1", "fact2"],
  "concepts": ["how-it-works", "problem-solution"],
  "files_read": ["/path/to/file"],
  "files_modified": ["/path/to/file"]
}

If the tool call has no meaningful learning value, respond:
{"skip": true}"""

# ── Local LLM (llama-cpp-python, lazy singleton) ──────────────

_llm = None
_llm_inference_count = 0
_LLM_RECYCLE_INTERVAL = 500  # Recreate model every N inferences to prevent memory leaks


def _get_llm():
    """Lazy-load the GGUF model (cached singleton). Recycles periodically."""
    global _llm, _llm_inference_count
    if _llm is not None and _llm_inference_count >= _LLM_RECYCLE_INTERVAL:
        logger.info(f"Recycling observation LLM after {_llm_inference_count} inferences")
        del _llm
        _llm = None
        _llm_inference_count = 0
        import gc
        gc.collect()
    if _llm is None:
        from llama_cpp import Llama
        logger.info(f"Loading observation LLM: {settings.observation_llm_model}")
        _llm = Llama(
            model_path=settings.observation_llm_model,
            n_ctx=2048,
            n_threads=4,
            verbose=False,
        )
        logger.info("Observation LLM loaded")
    return _llm


def _generate_local_sync(prompt: str) -> str | None:
    """Run inference synchronously (called from thread pool)."""
    global _llm_inference_count
    llm = _get_llm()
    out = llm(prompt, max_tokens=300, stop=["<|im_end|>"], temperature=0)
    _llm_inference_count += 1
    # Reset KV cache to prevent per-inference memory accumulation
    llm.reset()
    return out["choices"][0]["text"]


# ── Prompt builders ───────────────────────────────────────────

def build_user_prompt(
    tool_name: str,
    tool_input: dict | None,
    tool_response_preview: str | None,
    cwd: str | None,
    last_user_message: str | None,
) -> str:
    """Build the user prompt for the observation LLM."""
    parts = []
    if cwd:
        parts.append(f"Working directory: {cwd}")
    if last_user_message:
        parts.append(f"User request: {last_user_message[:500]}")
    parts.append(f"Tool: {tool_name}")
    if tool_input:
        input_str = json.dumps(tool_input, default=str)
        parts.append(f"Input: {input_str[:1500]}")
    if tool_response_preview:
        parts.append(f"Response: {tool_response_preview[:2000]}")
    return "\n\n".join(parts)


def build_chat_prompt(user_prompt: str) -> str:
    """Build Qwen chat-template prompt for llama.cpp."""
    return (
        f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
        f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


def parse_llm_response(text: str) -> dict | None:
    """Parse JSON from LLM response, handling markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        data = json.loads(text)
        if data.get("skip"):
            return None
        return data
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse LLM response: {text[:200]}")
        return None


# ── Generators ────────────────────────────────────────────────

async def generate_observation_local(
    tool_name: str,
    tool_input: dict | None,
    tool_response_preview: str | None,
    cwd: str | None,
    last_user_message: str | None,
) -> dict | None:
    """Generate observation using local GGUF model (llama-cpp-python)."""
    user_prompt = build_user_prompt(
        tool_name, tool_input, tool_response_preview, cwd, last_user_message
    )
    prompt = build_chat_prompt(user_prompt)

    try:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, _generate_local_sync, prompt)
        if text is None:
            return None
        return parse_llm_response(text)
    except Exception as e:
        logger.error(f"Local LLM error: {e}")
        return None


async def generate_observation_anthropic(
    tool_name: str,
    tool_input: dict | None,
    tool_response_preview: str | None,
    cwd: str | None,
    last_user_message: str | None,
) -> dict | None:
    """Generate observation using Anthropic API (Claude Haiku)."""
    import anthropic

    user_prompt = build_user_prompt(
        tool_name, tool_input, tool_response_preview, cwd, last_user_message
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return parse_llm_response(message.content[0].text)
    except Exception as e:
        logger.error(f"Anthropic API error: {e}")
        return None


async def generate_observation(
    tool_name: str,
    tool_input: dict | None,
    tool_response_preview: str | None,
    cwd: str | None,
    last_user_message: str | None,
) -> dict | None:
    """Generate observation using best available LLM.

    Uses local GGUF model by default. Falls back to Anthropic if
    API key is set and local fails.
    """
    if tool_name in SKIP_TOOLS:
        return None

    # Primary: local GGUF model
    result = await generate_observation_local(
        tool_name, tool_input, tool_response_preview, cwd, last_user_message
    )
    if result is not None:
        return result

    # Fallback: Anthropic API (if configured)
    if settings.anthropic_api_key:
        return await generate_observation_anthropic(
            tool_name, tool_input, tool_response_preview, cwd, last_user_message
        )

    return None
