"""Condense long lesson rules to a <= 280-char imperative instruction.

Why this exists (issue #75)
---------------------------
user-prompt-submit injects each active CRITICAL lesson's ``rule`` capped at
280 chars. Most long rules lead with the incident story and put the actual
instruction last, so the cap cut off the "what to do". Every lesson writer
(POST/PATCH /api/lessons, MCP ``create_lesson``, the distiller) now routes a
long rule through :func:`prepare_rule`: the condensed instruction goes into
``rule``; the original text is kept verbatim in ``detail``. Migration 019's
CHECK constraint is the database-level backstop.

Provider order
--------------
1. Anvil bridge with the backend/model PINNED by agent-memory settings
   (``anvil_condense_backend`` / ``anvil_condense_model_path``), so the result
   never depends on Anvil's global model or the LM Studio engine.
2. Claude Haiku, when an Anthropic key is configured and its breaker is closed.

Each provider gets one retry that tells it how long its answer was. If every
provider fails, :class:`CondenseRejected` is raised. A rule is NEVER silently
truncated and never stored over-long: a truncated rule loses exactly the
instruction this module exists to preserve.

The model is asked for PLAIN TEXT, not JSON: asked for a JSON object, the
local model emitted invalid JSON (unescaped quotes inside ``--reason "..."``).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

MAX_RULE_CHARS = 280

SYSTEM_PROMPT = (
    "You condense engineering lessons. Output ONLY the rule text, nothing else. "
    f"One imperative instruction, max {MAX_RULE_CHARS} characters, stating WHEN it "
    "applies and WHAT to do. Keep exact commands, flags, file names. No backstory, "
    "incident numbers or dates."
)

HAIKU_MODEL = "claude-haiku-4-5-20251001"


class ProviderUnavailable(RuntimeError):
    """A provider could not produce any answer (not configured, down, error)."""


class CondenseRejected(ValueError):
    """No provider produced a valid <= 280-char rule. The write must be refused."""


@dataclass(frozen=True)
class PreparedRule:
    rule: str
    detail: str | None
    provider: str | None  # None = rule was already short enough


# ── Post-processing ───────────────────────────────────────────

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```[\w-]*\s*\n?(.*?)\n?```$", re.DOTALL)
_PREFIX = re.compile(
    r"^(?:(?:critical|warning|info)\s*(?:\[[^\]]*\])?|rule|lesson)\s*:\s*",
    re.IGNORECASE,
)


def clean_rule(text: str) -> str:
    """Strip model wrappers: think blocks, code fences, a leading
    ``CRITICAL [scope]:`` / ``Rule:`` label, wrapping quotes, and collapse
    whitespace to a single line. Inline backticks are kept — they mark
    commands the rule must preserve."""
    text = _THINK_BLOCK.sub("", text or "")
    text = _UNCLOSED_THINK.sub("", text).strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    text = _PREFIX.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return " ".join(text.split())


# ── Providers ─────────────────────────────────────────────────

def anvil_model_env() -> dict[str, str]:
    """Env overrides that pin the bridge's model. Process env beats Anvil's
    .env files in pydantic-settings, so these win over /opt/anvil/.env."""
    return {
        "ANVIL_MODEL_BACKEND": settings.anvil_condense_backend,
        "ANVIL_MODEL_PATH": os.path.expanduser(settings.anvil_condense_model_path),
        "ANVIL_MODEL_NAME": "",
    }


async def _call_anvil(system: str, user: str) -> tuple[str, str]:
    if not settings.anvil_condense_enabled:
        raise ProviderUnavailable("anvil condenser disabled (anvil_condense_enabled=false)")
    from app.anvil_enrichment import EnrichmentUnavailable, generate_text

    try:
        return await generate_text(
            system,
            user,
            env_overrides=anvil_model_env(),
            timeout=settings.anvil_condense_timeout_seconds,
            keep_warm_seconds=settings.anvil_condense_keep_warm_seconds,
        )
    except EnrichmentUnavailable as error:
        raise ProviderUnavailable(f"anvil bridge failed: {error.__cause__!r}") from error


async def _call_haiku(system: str, user: str) -> tuple[str, str]:
    from app import llm_provider_status as provider_status

    if not provider_status.anthropic_available(settings.anthropic_api_key):
        raise ProviderUnavailable("anthropic not configured or breaker open")
    from app.observation_llm import _get_anthropic_client, wait_for_anthropic_slot

    await wait_for_anthropic_slot()
    try:
        msg = await _get_anthropic_client().messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as error:  # classified + recorded, then surfaced
        status = provider_status.record_failure(error)
        raise ProviderUnavailable(f"anthropic failed ({status}): {error}") from error
    provider_status.record_success()
    return msg.content[0].text, f"anthropic:{HAIKU_MODEL}"


# ── Core ──────────────────────────────────────────────────────

def _user_prompt(rule: str) -> str:
    return f"Condense this lesson into one rule:\n\n{rule}"


# The retry asks for a TIGHTER target than the hard limit. Measured on the
# live backfill dry run: a retry that only restated "max 280" and quoted the
# previous answer got the SAME answer back verbatim in 16 of 18 cases
# (293 -> 293, 315 -> 315, ...). The model copies the answer it is shown.
RETRY_TARGET_CHARS = 200


def _retry_prompt(rule: str, previous: str) -> str:
    if not previous:
        problem = "Your previous answer was empty."
    else:
        problem = (
            f"Your previous answer was {len(previous)} characters, over the "
            f"{MAX_RULE_CHARS}-character limit."
        )
    return (
        f"{problem} Write a NEW, shorter rule of at most {RETRY_TARGET_CHARS} "
        "characters: keep only the single most important instruction and the "
        "exact command or flag it needs; drop secondary clauses and examples. "
        f"Output only the rule.\n\nOriginal lesson:\n{rule}"
    )


async def condense_rule(rule: str) -> tuple[str, str]:
    """Return ``(condensed_rule, provider)`` or raise CondenseRejected."""
    failures: list[str] = []
    providers = (("anvil", _call_anvil), ("haiku", _call_haiku))
    for name, call in providers:
        prompt = _user_prompt(rule)
        for attempt in (1, 2):
            try:
                raw, provider = await call(SYSTEM_PROMPT, prompt)
            except ProviderUnavailable as error:
                failures.append(f"{name}: {error}")
                break
            candidate = clean_rule(raw)
            if candidate and len(candidate) <= MAX_RULE_CHARS:
                return candidate, provider
            failures.append(
                f"{name} attempt {attempt}: "
                + (f"{len(candidate)} chars" if candidate else "empty output")
            )
            prompt = _retry_prompt(rule, candidate)
    raise CondenseRejected(
        f"rule is {len(rule)} chars (max {MAX_RULE_CHARS}) and could not be "
        f"condensed: {'; '.join(failures)}. Shorten the rule yourself (put the "
        "backstory in `detail`) and retry."
    )


async def prepare_rule(rule: str, detail: str | None = None) -> PreparedRule:
    """Normalize a lesson's (rule, detail) for storage.

    A rule within the limit passes through untouched. A longer rule is
    condensed; the original goes to ``detail`` (any caller-supplied detail is
    appended after it, so nothing is lost).
    """
    rule = rule.strip()
    if len(rule) <= MAX_RULE_CHARS:
        return PreparedRule(rule=rule, detail=detail, provider=None)
    condensed, provider = await condense_rule(rule)
    logger.info(
        "lesson_condense: %d -> %d chars via %s", len(rule), len(condensed), provider
    )
    full_detail = f"{rule}\n\n{detail}" if detail else rule
    return PreparedRule(rule=condensed, detail=full_detail, provider=provider)


def lesson_raw_text(title: str, rule: str, detail: str | None) -> str:
    """Text embedded/indexed for a lesson: the condensed rule plus the
    long-form detail, so search still finds a lesson by its incident story."""
    text = f"{title}\n{rule}"
    return f"{text}\n\n{detail}" if detail else text
