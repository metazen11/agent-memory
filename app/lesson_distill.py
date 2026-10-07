"""Automatic lesson distillation: mine recurring failures into lessons.

Why this exists
---------------
Lessons were only ever created by hand. In 772 sessions that produced
exactly one. Meanwhile mem_tool_calls accumulated ~6.7k failed calls whose
errors repeat verbatim hundreds of times — `fatal: 'public_html/.git' not
recognized as a git repository` alone fired 1,997 times in one project.
Every one of those is a mistake a lesson could have prevented.

Design decisions grounded in the data
-------------------------------------
1. Cluster on a NORMALIZED error, not raw text. `old_string matches 138
   times` and `matches 192 times` are one lesson; digits are masked to N.
   Doing this merged 578 + 296 into a single 892-count cluster.

2. Threshold on REPETITION, not distinct sessions. 5,511 of 5,640 recent
   failures came from a single long-running session. A "seen in >= 3
   sessions" rule — the intuitive one — would have mined almost nothing.
   Repeating a mistake 200 times inside one session is precisely the
   failure a lesson prevents.

3. Scope to the project where it recurs. `public_html/.git` is an
   artofmetazen.com fact, not a global one. Global lessons need a much
   higher bar because they are injected everywhere.

4. NEVER emit a broad-match input lesson. trigger_on='input' with neither
   trigger_tool nor trigger_pattern matches every Edit/Write/Bash call and
   dominates the systemMessage budget. Migration 016 adds a CHECK that
   refuses such rows; an LLM asked to write lessons will produce them by
   default, so the synthesizer constrains the trigger itself rather than
   trusting the model.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Errors that are environmental noise, not a preventable agent mistake.
# A lesson telling the agent "don't let the network fail" helps nobody.
_NOISE_PATTERNS = [
    re.compile(r"^Command failed \(exit code N, no output\)$", re.I),
    re.compile(r"broken pipe", re.I),
    re.compile(r"connection reset by peer", re.I),
    re.compile(r"^\s*$"),
]

# Digits, hex ids, and timestamps vary per occurrence and would fragment
# otherwise-identical clusters.
_NORMALIZERS = [
    (re.compile(r"\b[0-9a-f]{7,40}\b", re.I), "HASH"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "TS"),
    (re.compile(r"\d+"), "N"),
    (re.compile(r"\s+"), " "),
]


def normalize_error(text: str | None) -> str:
    """Collapse an error into a clusterable signature."""
    if not text:
        return ""
    out = text.strip()
    for pattern, repl in _NORMALIZERS:
        out = pattern.sub(repl, out)
    return out.strip()[:300]


def is_noise(normalized: str) -> bool:
    """True when an error cluster is not worth a lesson."""
    if len(normalized) < 12:
        return True
    return any(p.search(normalized) for p in _NOISE_PATTERNS)


# ── Candidate mining ──────────────────────────────────────────

CANDIDATE_SQL = """
    SELECT
        p.name                              AS project_name,
        p.full_path                         AS project_path,
        tc.project_id                       AS project_id,
        tc.tool_name                        AS tool_name,
        count(*)                            AS occurrences,
        count(DISTINCT tc.session_id)       AS sessions,
        max(tc.created_at)                  AS last_seen,
        (array_agg(tc.tool_error ORDER BY tc.created_at DESC))[1:3]  AS sample_errors,
        (array_agg(tc.tool_input::text ORDER BY tc.created_at DESC))[1:3] AS sample_inputs,
        (array_agg(tc.id ORDER BY tc.created_at DESC))[1]            AS latest_call_id
    FROM mem_tool_calls tc
    JOIN mem_projects p ON p.id = tc.project_id
    WHERE tc.tool_success = false
      AND tc.tool_error IS NOT NULL
      AND tc.created_at > now() - ($1 || ' days')::interval
    GROUP BY 1, 2, 3, 4,
             regexp_replace(regexp_replace(tc.tool_error, '[0-9]+', 'N', 'g'), '\\s+', ' ', 'g')
    HAVING count(*) >= $2
    ORDER BY count(*) DESC
    LIMIT $3
"""


async def find_candidates(
    conn,
    *,
    lookback_days: int = 180,
    min_occurrences: int = 10,
    limit: int = 25,
) -> list[dict]:
    """Return recurring failure clusters worth considering for a lesson."""
    rows = await conn.fetch(CANDIDATE_SQL, str(lookback_days), min_occurrences, limit)

    candidates: list[dict] = []
    for row in rows:
        samples = [e for e in (row["sample_errors"] or []) if e]
        if not samples:
            continue
        normalized = normalize_error(samples[0])
        if is_noise(normalized):
            continue
        candidates.append({
            "project_id": row["project_id"],
            "project_name": row["project_name"],
            "project_path": row["project_path"],
            "tool_name": row["tool_name"],
            "occurrences": row["occurrences"],
            "sessions": row["sessions"],
            "last_seen": row["last_seen"],
            "normalized_error": normalized,
            "sample_errors": samples[:3],
            "sample_inputs": [i for i in (row["sample_inputs"] or []) if i][:2],
            "latest_call_id": row["latest_call_id"],
        })
    return candidates


# ── Deduplication against existing lessons ────────────────────

async def filter_already_covered(conn, candidates: list[dict], *, threshold: float = 0.80) -> list[dict]:
    """Drop candidates already covered by an active lesson.

    Uses pgvector cosine distance against mem_lessons.embedding so a
    reworded duplicate is caught, not just an exact string repeat. Falls
    back to keeping the candidate when no embedding is available — a
    duplicate lesson is a smaller failure than a missing one, and the
    review gate catches it.
    """
    from app.embeddings import embed_text

    kept: list[dict] = []
    for cand in candidates:
        probe = f"{cand['tool_name']} {cand['normalized_error']}"
        try:
            vec = await embed_text(probe)
            vec_str = "[" + ",".join(str(v) for v in vec) + "]"
            row = await conn.fetchrow(
                """
                SELECT id, title, 1 - (embedding <=> $1::vector) AS similarity
                FROM mem_lessons
                WHERE active = true AND embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT 1
                """,
                vec_str,
            )
            if row and row["similarity"] is not None and row["similarity"] >= threshold:
                logger.info(
                    "distill: skipping candidate covered by lesson #%s (sim=%.2f): %s",
                    row["id"], row["similarity"], cand["normalized_error"][:60],
                )
                continue
        except Exception as e:
            logger.warning("distill: dedup check failed, keeping candidate: %s", e)
        kept.append(cand)
    return kept



async def find_duplicate_lesson(conn, title: str, rule: str, *, threshold: float = 0.90) -> dict | None:
    """Return an existing lesson that already says this, if any.

    This runs AFTER synthesis and compares like with like: stored lessons
    embed `title\nrule`, so probing with the same shape is far more
    accurate than probing with raw error text.

    The pre-synthesis probe in filter_already_covered() compares an ERROR
    against a RULE — different vocabularies — and scored an exact
    duplicate at only 0.710, under any threshold that would not also
    swallow unrelated lessons (an unrelated one scored 0.644). That probe
    is kept as a cheap first pass that avoids wasted LLM calls; this is
    the accurate gate.
    """
    from app.embeddings import embed_text

    try:
        vec = await embed_text(f"{title}\n{rule}")
        vec_str = "[" + ",".join(str(v) for v in vec) + "]"
        row = await conn.fetchrow(
            """
            SELECT id, title, 1 - (embedding <=> $1::vector) AS similarity
            FROM mem_lessons
            WHERE active = true AND embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT 1
            """,
            vec_str,
        )
    except Exception as e:
        logger.warning("distill: duplicate check failed: %s", e)
        return None

    if row and row["similarity"] is not None and row["similarity"] >= threshold:
        return {"id": row["id"], "title": row["title"], "similarity": row["similarity"]}
    return None

# ── Synthesis ─────────────────────────────────────────────────

SYNTH_SYSTEM_PROMPT = """You write preventive rules for a coding agent's memory system.

You are given a failure that recurred many times. Write ONE lesson that would
have prevented it.

Rules for the output:
- "rule" is an instruction to a future agent, in the imperative. State what to
  do INSTEAD, not merely what went wrong. Name the concrete command, flag, path
  or API that fixes it.
- "rule" must be self-contained: an agent reads it with no other context.
- "title" is under 60 characters.
- Be specific. "Be careful with git" is useless. "Run git -C public_html rev-parse
  before treating it as a repo; it is a plain directory in this project" is useful.
- severity: "critical" only if the failure destroys work, corrupts state, or
  blocks all progress. Otherwise "warning".
- If the failure is environmental (network flake, disk full, an upstream outage)
  and no agent behavior would prevent it, respond {"skip": true}.

- "rule" must be at least two sentences and 20+ words. One-clause
  restatements of the error are rejected by the validator.

GOOD rule:
  "Run `git -C public_html rev-parse --git-dir` before any git command that
   targets public_html. It is a plain directory in this project, not a
   repository, so `git -C public_html status` fails with 'not recognized as
   a git repository'. Use the repo root instead."

BAD rules (all rejected):
  "Set replace_all=True in edit_file calls."   <- wrong fix, and too short
  "Be careful with file paths."                <- vague, no action
  "Use relative paths."                        <- names nothing concrete

Respond with JSON only, no markdown fences:
{"skip": false, "title": "...", "rule": "...", "severity": "warning"}"""


def build_synth_prompt(candidate: dict) -> str:
    """Render a candidate cluster into a synthesis prompt."""
    parts = [
        f"Project: {candidate['project_name']}",
        f"Tool: {candidate['tool_name']}",
        f"This exact failure occurred {candidate['occurrences']} times"
        f" across {candidate['sessions']} session(s).",
        "",
        "Error samples:",
    ]
    for err in candidate["sample_errors"][:3]:
        parts.append(f"  - {str(err)[:300]}")
    if candidate.get("sample_inputs"):
        parts.append("")
        parts.append("Tool inputs that produced it:")
        for inp in candidate["sample_inputs"][:2]:
            parts.append(f"  - {str(inp)[:300]}")
    return "\n".join(parts)


#: Identifiers recorded in mem_lessons.synthesized_by. Stable strings, not
#: model version ids: the point is to find locally-synthesized lessons later
#: and re-run them against a better model, which only needs the tier.
PROVIDER_ANTHROPIC = "anthropic:claude-haiku-4-5"
PROVIDER_LOCAL = "local:gguf"


async def synthesize_lesson(candidate: dict) -> dict | None:
    """Ask the LLM for a lesson. Returns None to skip.

    Reuses app.observation_llm's provider selection and Anthropic rate
    limiter rather than opening a second, unthrottled path to the API.

    The returned dict carries a ``_provider`` key naming which model
    actually wrote the rule. Locally-synthesized lessons are markedly
    weaker (the local 7B produced "Set replace_all=True in edit_file
    calls" — too thin AND wrong), so the provider is persisted on the
    lesson row to make them findable for re-synthesis later.
    """
    from app import llm_provider_status as provider_status
    from app.config import settings
    from app.observation_llm import (
        _generate_local_sync,
        _get_anthropic_client,
        parse_llm_response,
        wait_for_anthropic_slot,
    )

    user_prompt = build_synth_prompt(candidate)

    # Provider order is DELIBERATELY the reverse of observation capture.
    # Capture runs per tool call and needs the local model's throughput.
    # Distillation runs on a schedule over a handful of candidates, so the
    # 13s Anthropic throttle costs nothing and buys markedly better rules —
    # the local 7B emitted "Set replace_all=True in edit_file calls", which
    # is actively harmful advice (blind replace_all makes wrong edits; the
    # real fix is more surrounding context).
    # anthropic_available() is checked BEFORE the throttle sleep below.
    # synthesize_lesson() runs once per candidate, and the original code
    # paid the full 13s _ANTHROPIC_MIN_INTERVAL wait on every one of them
    # even after the account had already returned "credit balance is too
    # low" — ~130 seconds of sleeping per 10-candidate run for calls that
    # could not succeed. The breaker collapses that to a single attempt.
    if provider_status.anthropic_available(settings.anthropic_api_key):
        await wait_for_anthropic_slot()
        try:
            client = _get_anthropic_client()
            msg = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system=SYNTH_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as e:
            # Trips the breaker on billing/auth so the remaining candidates
            # in this run go straight to local. Transient errors (timeout,
            # 429, 5xx) leave it closed and retry normally next candidate.
            status = provider_status.record_failure(e)
            logger.warning(
                "distill: anthropic synthesis failed (%s), trying local: %s", status, e
            )
        else:
            provider_status.record_success()
            parsed = parse_llm_response(msg.content[0].text)
            if parsed and (parsed.get("rule") or parsed.get("skip")):
                parsed["_provider"] = PROVIDER_ANTHROPIC
                return parsed

    if settings.anvil_fallback_enabled:
        from app.anvil_enrichment import EnrichmentUnavailable, generate_json
        try:
            result = await generate_json(SYNTH_SYSTEM_PROMPT, user_prompt, "lesson")
            return result
        except EnrichmentUnavailable:
            logger.warning("distill: Anvil unavailable; trying local GGUF")

    # Fallback: local GGUF.
    if settings.observation_llm_model:
        try:
            import asyncio as _asyncio
            prompt = (
                f"<|im_start|>system\n{SYNTH_SYSTEM_PROMPT}<|im_end|>\n"
                f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
                f"<|im_start|>assistant\n"
            )
            loop = _asyncio.get_event_loop()
            text = await loop.run_in_executor(None, _generate_local_sync, prompt)
            parsed = parse_llm_response(text) if text else None
            if parsed and parsed.get("rule"):
                parsed["_provider"] = PROVIDER_LOCAL
                return parsed
        except Exception as e:
            logger.warning("distill: local synthesis failed: %s", e)

    return None


# ── Trigger construction ──────────────────────────────────────

def build_trigger(candidate: dict) -> dict:
    """Derive a NARROW trigger for a candidate.

    The trigger is computed here, never taken from the LLM. A model asked
    for a trigger reliably returns trigger_on='input' with no tool and no
    pattern — the broad-match shape that migration 016's CHECK constraint
    refuses and that would fire on every tool call.

    Preference order:
      1. file_scope, when the failing inputs name specific files. Most
         precise and cannot flood unrelated calls.
      2. input + trigger_tool + trigger_pattern, keyed off a literal token
         lifted from the error.
      3. input + trigger_tool alone, which is still narrow because it is
         bounded to one tool.
    """
    tool = candidate.get("tool_name") or None
    files = _extract_files(candidate)
    if files:
        return {"trigger_on": "file_scope", "trigger_files": files}

    pattern = _extract_pattern(candidate)
    trigger = {"trigger_on": "input", "trigger_tool": tool}
    if pattern:
        trigger["trigger_pattern"] = pattern

    # Last resort: with neither a tool nor a pattern this would be a
    # broad-match lesson firing on every tool call — refused by migration
    # 016's CHECK and by the API. Signal "unusable" so the caller drops the
    # candidate rather than attempting a doomed insert.
    if not trigger.get("trigger_tool") and not trigger.get("trigger_pattern"):
        return {"trigger_on": "input", "trigger_tool": None, "_unusable": True}
    return trigger


_PATH_RE = re.compile(r"[\w./~-]*/[\w./-]+|\b[\w-]+\.[A-Za-z0-9]{1,8}\b")


def _extract_files(candidate: dict) -> list[str]:
    """Pull file paths out of the failing inputs — only when the ERROR names them.

    A file trigger is only correct when the failure is ABOUT that file.
    Deriving it from whatever file happened to be open produces nonsense:
    the generic "old_string matches N times" cluster would otherwise be
    scoped to .mcp.json, when the real lesson ("make old_string unique")
    applies to every file. So a path must appear in the error text itself,
    and must recur across samples, before it earns a file_scope trigger.
    """
    error_blob = " ".join(str(e) for e in candidate.get("sample_errors", []))

    counts: dict[str, int] = {}
    for raw in candidate.get("sample_inputs", []):
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ("file_path", "path", "notebook_path"):
            val = data.get(key)
            if not isinstance(val, str) or not val:
                continue
            base = val.rsplit("/", 1)[-1]
            if len(base) <= 2:
                continue
            # The error must actually reference this file.
            if base not in error_blob:
                continue
            counts[base] = counts.get(base, 0) + 1

    # Require recurrence: a path seen once is an incidental detail.
    recurring = sorted(f for f, n in counts.items() if n >= 2)
    return recurring[:5]


# Literal, stable fragments worth keying a regex on.
_TOKEN_RE = re.compile(r"[\w][\w./-]{3,40}")
# Tokens so common that keying a regex on them matches nearly every call.
_GENERIC_TOKENS = {
    "bin/sh", "bin/bash", "/bin/sh", "/bin/bash", "usr/bin", "/usr/bin",
    "opt/homebrew", "site-packages", "node_modules", ".venv", "python.app",
}

_TOKEN_STOPWORDS = {
    "error", "fatal", "failed", "running", "command", "provide", "times",
    "please", "cannot", "unable", "exceeds", "missing", "required", "positional",
    "argument", "arguments", "context", "unique", "found", "file", "lines",
    "the", "not", "use", "and", "for", "with", "more",
}


def _extract_pattern(candidate: dict) -> str | None:
    """Build a conservative regex from distinctive error tokens."""
    normalized = candidate.get("normalized_error", "")
    tokens = []
    for tok in _TOKEN_RE.findall(normalized):
        low = tok.lower()
        if low in _TOKEN_STOPWORDS or low.isdigit():
            continue
        # Drop tokens that are mostly normalization placeholders — `N.N/N.N.N`
        # carries no signal and would only match our own masking artifact.
        if re.fullmatch(r"[N./-]+", tok):
            continue
        # Also reject tokens that merely CONTAIN masking placeholders —
        # `N.N/N.N.N/Frameworks/...` is an artifact of our own
        # normalization and matches nothing real at trigger time.
        if re.search(r"(^|[/.])N([/.]|$)", tok):
            continue
        # Reject generic tokens that appear in nearly any shell command or
        # path. `bin/sh` as a trigger_pattern fires on almost everything.
        if low in _GENERIC_TOKENS:
            continue
        if ("/" in tok or "." in tok or "-" in tok) and len(tok) >= 5:
            tokens.append(tok)
    if not tokens:
        return None
    # Cap the alternation so the pattern stays well under MAX_PATTERN_LEN
    # and stays readable in a review diff.
    chosen = []
    for tok in tokens:
        if tok not in chosen:
            chosen.append(tok)
        if len(chosen) == 3:
            break
    pattern = "|".join(re.escape(t) for t in chosen)
    return pattern if len(pattern) <= 200 else None


# ── Orchestration ─────────────────────────────────────────────

# Global lessons inject into every project, so they need a much higher bar
# than a project-scoped one.
GLOBAL_PROMOTION_MIN_PROJECTS = 3


def validate_lesson_payload(payload: dict) -> tuple[bool, str | None]:
    """Reject a synthesized lesson that would be unusable or unsafe.

    The DB CHECK from migration 016 is the last line of defence; this keeps
    a bad row from ever being attempted and gives a readable reason.
    """
    rule = (payload.get("rule") or "").strip()
    title = (payload.get("title") or "").strip()

    if not rule or not title:
        return False, "missing title or rule"
    # A rule must be long enough to say what to do INSTEAD. The local 7B
    # reliably emits one-clause restatements ("Set replace_all=True in
    # edit_file calls") that pass a naive length check yet teach nothing —
    # and in that example actively mislead.
    if len(rule) < 60:
        return False, "rule too short to be actionable"
    if len(rule.split()) < 10:
        return False, "rule too terse to be actionable"

    # Reject vague filler with no operational content.
    _VAGUE = ("be careful", "make sure to be", "pay attention", "remember to try")
    low_rule = rule.lower()
    if any(v in low_rule for v in _VAGUE):
        return False, "rule is vague guidance, not an actionable instruction"

    # An actionable rule names something concrete: a command, flag, path,
    # function, or API. Require at least one such token.
    if not re.search(r"[\w-]+\.[A-Za-z0-9]{1,8}|--?[a-z][\w-]{2,}|/[\w.-]+|\w+\(\)|`[^`]+`", rule):
        return False, "rule names no concrete command, flag, path, or API"
    if len(title) > 120:
        return False, "title too long"
    if payload.get("severity") not in ("critical", "warning", "info"):
        return False, f"invalid severity {payload.get('severity')!r}"

    # Broad-match guard, mirroring the API and the DB constraint.
    if payload.get("trigger_on") == "input":
        if not payload.get("trigger_tool") and not payload.get("trigger_pattern"):
            return False, "broad-match input trigger (no tool, no pattern)"
    if payload.get("trigger_on") == "file_scope" and not payload.get("trigger_files"):
        return False, "file_scope trigger without trigger_files"

    # A regex that does not compile would 400 at the API anyway.
    pattern = payload.get("trigger_pattern")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as e:
            return False, f"invalid regex: {e}"

    return True, None


async def distill_once(
    conn,
    *,
    lookback_days: int = 180,
    min_occurrences: int = 10,
    limit: int = 10,
    dry_run: bool = True,
) -> dict:
    """Run one distillation pass.

    Returns a report describing what was proposed and (unless dry_run)
    what was created. dry_run defaults to True: creating lessons writes
    into every future session's context, so the caller opts in explicitly.
    """
    candidates = await find_candidates(
        conn,
        lookback_days=lookback_days,
        min_occurrences=min_occurrences,
        limit=limit,
    )
    candidates = await filter_already_covered(conn, candidates)

    proposed: list[dict] = []
    rejected: list[dict] = []
    # Which model actually wrote each rule, counted per provider. Surfaced
    # in the report so a run that silently degraded to the local 7B is
    # visible in the output instead of only in a log line.
    providers_used: dict[str, int] = {}

    for cand in candidates:
        synth = await synthesize_lesson(cand)
        if not synth or synth.get("skip"):
            rejected.append({
                "error": cand["normalized_error"][:80],
                "reason": "llm skipped (environmental or not preventable)",
            })
            continue

        trigger = build_trigger(cand)
        if trigger.pop("_unusable", False):
            rejected.append({
                "error": cand["normalized_error"][:80],
                "reason": "no narrow trigger derivable (would be broad-match)",
            })
            continue

        provider = synth.get("_provider")
        if provider:
            providers_used[provider] = providers_used.get(provider, 0) + 1

        payload = {
            "title": (synth.get("title") or "").strip()[:120],
            "rule": (synth.get("rule") or "").strip(),
            "severity": synth.get("severity", "warning"),
            # Project-scoped by default. Promotion to global is a separate,
            # higher-bar decision — see GLOBAL_PROMOTION_MIN_PROJECTS.
            "project": cand.get("project_path") or cand.get("project_name"),
            # Which model wrote this rule. Persisted so locally-synthesized
            # (lower-quality) lessons can be found and re-synthesized once a
            # better provider is available again.
            "synthesized_by": provider,
            **trigger,
        }

        # Validate the full rule, condense it, THEN dedup: the duplicate
        # check must compare the rule that will actually be stored.
        ok, reason = validate_lesson_payload(payload)
        if ok:
            reason = await condense_payload(payload)
            ok = reason is None
        if not ok:
            rejected.append({"error": cand["normalized_error"][:80], "reason": reason})
            continue

        duplicate = await find_duplicate_lesson(conn, payload["title"], payload["rule"])
        if duplicate:
            rejected.append({
                "error": cand["normalized_error"][:80],
                "reason": (
                    f"duplicate of lesson #{duplicate['id']} "
                    f"({duplicate['similarity']:.2f} similar)"
                ),
            })
            continue

        payload["_evidence"] = {
            "occurrences": cand["occurrences"],
            "sessions": cand["sessions"],
            "tool_name": cand["tool_name"],
            "normalized_error": cand["normalized_error"][:200],
            "last_seen": cand["last_seen"].isoformat() if cand.get("last_seen") else None,
        }
        proposed.append(payload)

    created: list[dict] = []
    if not dry_run:
        for payload in proposed:
            evidence = payload.pop("_evidence", None)
            try:
                lesson_id = await _insert_lesson(conn, payload)
                created.append({"id": lesson_id, "title": payload["title"]})
                logger.info(
                    "distill: created lesson #%s from %sx failure: %s",
                    lesson_id,
                    (evidence or {}).get("occurrences"),
                    payload["title"],
                )
            except Exception as e:
                logger.error("distill: insert failed for %r: %s", payload["title"], e)
                rejected.append({"error": payload["title"], "reason": f"insert failed: {e}"})

    from app import llm_provider_status as provider_status
    from app.config import settings as _settings

    return {
        "candidates_examined": len(candidates),
        "proposed": proposed,
        "created": created,
        "rejected": rejected,
        "dry_run": dry_run,
        # Provider accounting. `providers_used` says who actually wrote the
        # rules; `llm` reports last-known provider health so a run that fell
        # back because the account is out of credit says so, rather than
        # looking identical to a run with no key configured.
        "providers_used": providers_used,
        "llm": provider_status.snapshot(_settings.anthropic_api_key),
    }


async def condense_payload(payload: dict) -> str | None:
    """Route a synthesized rule through the same condenser as the API/MCP.

    Mutates ``payload`` (rule -> <= 280 chars, original -> ``detail``) and
    returns None, or returns a rejection reason. Runs after
    validate_lesson_payload so actionability is judged on the full rule.
    """
    from app.lesson_condense import CondenseRejected, prepare_rule

    try:
        prepared = await prepare_rule(payload["rule"], payload.get("detail"))
    except CondenseRejected as error:
        return f"rule too long and not condensable: {error}"
    payload["rule"] = prepared.rule
    payload["detail"] = prepared.detail
    return None


async def _insert_lesson(conn, payload: dict) -> int:
    """Insert a validated lesson, mirroring POST /api/lessons."""
    from app.embeddings import embed_text
    from app.project import ensure_project

    project_id = None
    if payload.get("project"):
        project_id = await ensure_project(conn, payload["project"])

    from app.lesson_condense import lesson_raw_text

    raw_text = lesson_raw_text(payload["title"], payload["rule"], payload.get("detail"))
    embedding_str = None
    try:
        embedding = await embed_text(raw_text)
        embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
    except Exception as e:
        logger.warning("distill: embedding failed: %s", e)

    row = await conn.fetchrow(
        """
        INSERT INTO mem_lessons (
            project_id, title, rule, severity,
            trigger_tool, trigger_pattern,
            embedding, raw_text,
            trigger_on, trigger_output_pattern, trigger_phase, trigger_files,
            synthesized_by, detail
        ) VALUES ($1,$2,$3,$4,$5,$6,$7::vector,$8,$9,$10,$11,$12,$13,$14)
        RETURNING id
        """,
        project_id,
        payload["title"],
        payload["rule"],
        payload["severity"],
        payload.get("trigger_tool"),
        payload.get("trigger_pattern"),
        embedding_str,
        raw_text,
        payload.get("trigger_on", "input"),
        payload.get("trigger_output_pattern"),
        payload.get("trigger_phase"),
        payload.get("trigger_files"),
        payload.get("synthesized_by"),
        payload.get("detail"),
    )
    return row["id"]
