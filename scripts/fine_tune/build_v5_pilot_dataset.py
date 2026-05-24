#!/usr/bin/env python3
"""v5 pilot dataset builder.

Pipeline:
  1. Read config (yaml from v5_pilot_wizard.py).
  2. Pull source_window_recent_rows recent tool_calls from DB via psql_wrapper.sh
     (CSV stream parsed into rows).
  3. Apply filters in order, counting drops at each stage:
       a. Drop rows in exclude_projects / matching exclude_project_patterns.
       b. Drop rows where required fields are NULL/empty.
       c. Strip injected reminder blocks from prompt_text.
       d. Drop continuation-style prompts (short + matches pattern).
       e. Dedup by (prompt_text, tool_input, cwd) — keep first occurrence.
  4. Normalize paths via v5_schema (cwd + tool_input + tool_response).
  5. Build V5Row + render to chat-format JSONL.
  6. Stop when target_clean_rows reached.
  7. Write train.jsonl + AUDIT.md (per-project counts, drop reasons, 3 samples).

Usage:
    python3 scripts/fine_tune/build_v5_pilot_dataset.py \
        --config configs/v5_pilot.yaml
    python3 scripts/fine_tune/build_v5_pilot_dataset.py \
        --config configs/v5_pilot.yaml --dry-run   # print stats only
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# Repo paths
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "fine_tune"))

from v5_schema import (  # noqa: E402
    V5Row, ToolTurn, render_jsonl_line,
    normalize_path_string, normalize_json_paths,
    UNKNOWN_PROJECT, TRUSTED_ROOT_TOKEN, LOCAL_WORKSPACE_PREFIXES,
)

# Defense-in-depth redaction. The ingest path is supposed to redact, but
# webhook URLs (Slack/Discord) historically slipped through, so we re-run
# the pattern set on every text leaf before it lands in the training set.
# If app.config can't load (no .env in CI), fall back to a stripped-down
# inline redactor with the same patterns.
try:
    from app.redact import redact_text, redact_json, SECRET_PATTERNS  # noqa: E402
except Exception:  # pragma: no cover — defensive fallback
    import re as _re

    SECRET_PATTERNS = [
        (_re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_-]{80,}", _re.I), "[REDACTED:anthropic_key]"),
        (_re.compile(r"sk-[A-Za-z0-9]{48,}"), "[REDACTED:openai_key]"),
        (_re.compile(r"hf_[A-Za-z0-9]{30,}"), "[REDACTED:hf_token]"),
        (_re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws_key]"),
        (_re.compile(r"ghp_[A-Za-z0-9]{36,}"), "[REDACTED:github_token]"),
        (_re.compile(r"gho_[A-Za-z0-9]{36,}"), "[REDACTED:github_oauth]"),
        (_re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "[REDACTED:gitlab_token]"),
        (_re.compile(r"xox[bpors]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),
        (_re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"), "[REDACTED:slack_webhook]"),
        (_re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"), "[REDACTED:discord_webhook]"),
        (_re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", _re.I), "[REDACTED:bearer_token]"),
        (_re.compile(r"(?:password|passwd|pwd)\s*[=:]\s*\S+", _re.I), "[REDACTED:password]"),
        (_re.compile(r"(?:postgresql|mysql|mongodb)://[^\s]+@[^\s]+", _re.I), "[REDACTED:connection_string]"),
    ]

    def redact_text(text):
        if not text:
            return text
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def redact_json(obj):
        if isinstance(obj, dict):
            return {k: redact_json(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [redact_json(v) for v in obj]
        if isinstance(obj, str):
            return redact_text(obj)
        return obj

PSQL_WRAPPER = REPO_ROOT / "scripts" / "psql_wrapper.sh"


# --- Config loader -----------------------------------------------------------
# We don't want a pyyaml dep — the wizard writes a flat key:value yaml subset.

def load_yaml_flat(path: Path) -> dict[str, Any]:
    """Minimal yaml loader for the flat key:value / key:[list] format the
    wizard emits. Not a general yaml parser — only handles what we write."""
    out: dict[str, Any] = {}
    current_list_key: Optional[str] = None
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - "):
            if current_list_key is None:
                raise ValueError(f"orphan list item: {line!r}")
            val = line[4:].strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1].replace('\\"', '"')
            out[current_list_key].append(val)
            continue
        # New key
        if ":" not in line:
            raise ValueError(f"bad line: {line!r}")
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val == "":  # opens a list
            out[key] = []
            current_list_key = key
            continue
        current_list_key = None
        if val == "[]":
            out[key] = []
        elif val in {"true", "false"}:
            out[key] = (val == "true")
        elif val.startswith('"') and val.endswith('"'):
            out[key] = val[1:-1].replace('\\"', '"')
        else:
            try:
                out[key] = int(val)
            except ValueError:
                out[key] = val
    return out


# --- DB query ----------------------------------------------------------------

SELECT_COLS = [
    "tc.id",
    "tc.created_at",
    "p.name AS project_name",
    "tc.tool_name",
    "tc.tool_input",
    "tc.tool_response_preview",
    "tc.tool_error",
    "tc.prompt_text",
    "tc.cwd",
    "tc.session_id",
]


def query_source_rows(limit: int) -> list[dict[str, Any]]:
    """Pull recent rows via psql_wrapper.sh in CSV mode. Returns dicts."""
    cols = ", ".join(SELECT_COLS)
    sql = f"""
COPY (
  SELECT {cols}
  FROM mem_tool_calls tc
  LEFT JOIN mem_projects p ON p.id = tc.project_id
  WHERE tc.tool_response_preview IS NOT NULL
    AND tc.tool_error IS NULL
    AND tc.prompt_text IS NOT NULL
    AND tc.prompt_text != ''
  ORDER BY tc.created_at DESC
  LIMIT {limit}
) TO STDOUT WITH (FORMAT csv, HEADER true, FORCE_QUOTE *);
""".strip()

    print(f"[db] querying top {limit} recent rows via psql_wrapper.sh...")
    result = subprocess.run(
        [str(PSQL_WRAPPER), "-c", sql],
        capture_output=True, text=True, check=True,
    )
    # Parse CSV
    import csv as _csv
    rows: list[dict[str, Any]] = []
    reader = _csv.DictReader(result.stdout.splitlines())
    for r in reader:
        # Parse tool_input as JSON (it's a jsonb column → comes back as JSON text)
        if r.get("tool_input"):
            try:
                r["tool_input"] = json.loads(r["tool_input"])
            except (json.JSONDecodeError, TypeError):
                r["tool_input"] = {}
        else:
            r["tool_input"] = {}
        rows.append(r)
    print(f"[db] got {len(rows)} rows")
    return rows


# --- Filters -----------------------------------------------------------------

# Match injected reminder blocks: <agent-memory>...</agent-memory>,
# <system-reminder>...</system-reminder>, and a few related wrappers.
_INJECTED_BLOCKS_RE = re.compile(
    r"<(agent-memory|system-reminder|user-prompt-submit-hook|local-command-(?:stdout|stderr|caveat))>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def strip_injected_blocks(text: str) -> str:
    if not text:
        return text
    cleaned = _INJECTED_BLOCKS_RE.sub("", text)
    return cleaned.strip()


def is_excluded_project(project_name: str, exact: list[str], patterns: list[str]) -> bool:
    if not project_name:
        return False
    pn = project_name.strip()
    if pn in exact:
        return True
    pn_lower = pn.lower()
    for pat in patterns:
        if fnmatch.fnmatch(pn_lower, pat.lower()):
            return True
    return False


def is_continuation_prompt(prompt: str, min_len: int, pattern: re.Pattern) -> bool:
    if not prompt:
        return True  # empty = drop
    s = prompt.strip()
    if len(s) >= min_len:
        return False
    return bool(pattern.match(s.lower()))


def cwd_in_workspace(cwd: str) -> bool:
    """True if cwd starts with any known workspace prefix (case-insensitive)."""
    if not cwd:
        return False
    cwd_lower = cwd.lower()
    return any(cwd_lower.startswith(p.lower()) for p in LOCAL_WORKSPACE_PREFIXES)


def cwd_starts_with_excluded(cwd: str, excluded_prefixes: list[str]) -> bool:
    if not cwd:
        return False
    return any(cwd.startswith(p) for p in excluded_prefixes)


def derive_subfolder(cwd_normalized: str, project_name: str) -> Optional[str]:
    """Given normalized cwd '<TRUSTED_ROOT>/proj/a/b/c' and project_name 'proj',
    return 'a' (the first folder under project_root), or None if cwd IS project_root.

    Why first folder only: it's the meaningful sub-concern boundary (wfca-app vs
    aws-infrastructure vs wfca-etl). Deeper paths are just file-level locations.
    """
    expected_root = f"{TRUSTED_ROOT_TOKEN}/{project_name}"
    if not cwd_normalized.startswith(expected_root):
        return None
    rest = cwd_normalized[len(expected_root):].lstrip("/")
    if not rest:
        return None
    return rest.split("/", 1)[0] or None


# --- Builder -----------------------------------------------------------------

@dataclass
class BuildStats:
    db_rows_pulled: int = 0
    dropped_excluded_project: int = 0
    dropped_unknown_project: int = 0
    dropped_empty_prompt_after_strip: int = 0
    dropped_continuation: int = 0
    dropped_duplicate: int = 0
    dropped_bad_tool_input: int = 0
    dropped_cwd_outside_workspace: int = 0
    dropped_cwd_excluded_prefix: int = 0
    dropped_too_long: int = 0
    kept: int = 0
    per_project_kept: dict[str, int] = field(default_factory=dict)
    per_project_dropped: dict[str, int] = field(default_factory=dict)
    per_subfolder_kept: dict[str, int] = field(default_factory=dict)


def build(cfg: dict[str, Any], dry_run: bool) -> tuple[list[str], BuildStats, list[V5Row]]:
    """Returns (jsonl_lines, stats, sample_rows_for_audit)."""
    stats = BuildStats()
    cont_pattern = re.compile(cfg["continuation_prompt_pattern"])
    exclude_exact = cfg["exclude_projects"]
    exclude_patterns = cfg["exclude_project_patterns"]
    exclude_cwd_prefixes = cfg.get("exclude_cwd_prefixes", [])
    require_workspace_cwd = bool(cfg.get("require_cwd_in_workspace", True))
    target = int(cfg["target_clean_rows"])
    min_prompt = int(cfg["min_prompt_len_chars"])
    drop_cont = bool(cfg["drop_continuation_prompts"])
    strip_blocks = bool(cfg["strip_injected_blocks"])

    raw_rows = query_source_rows(int(cfg["source_window_recent_rows"]))
    stats.db_rows_pulled = len(raw_rows)

    # Minimal tools envelope. The trainer's collator may wrap/expand this;
    # using minimal {name} shape keeps the row valid + small.
    tools_envelope: list[dict[str, Any]] = []
    seen_tools: set[str] = set()

    jsonl_lines: list[str] = []
    sample_rows: list[V5Row] = []
    dedup_keys: OrderedDict[tuple[str, str, str], int] = OrderedDict()

    for raw in raw_rows:
        project_name = (raw.get("project_name") or "").strip() or UNKNOWN_PROJECT

        # 1. Project exclusion
        if is_excluded_project(project_name, exclude_exact, exclude_patterns):
            stats.dropped_excluded_project += 1
            stats.per_project_dropped[project_name] = stats.per_project_dropped.get(project_name, 0) + 1
            continue

        # 2. Drop unknown-project rows for the pilot (no project signal to learn from)
        if project_name == UNKNOWN_PROJECT:
            stats.dropped_unknown_project += 1
            continue

        # 2b. Path-sanity gates on raw cwd (before normalization)
        cwd_raw = raw.get("cwd") or ""
        if cwd_starts_with_excluded(cwd_raw, exclude_cwd_prefixes):
            stats.dropped_cwd_excluded_prefix += 1
            stats.per_project_dropped[project_name] = stats.per_project_dropped.get(project_name, 0) + 1
            continue
        if require_workspace_cwd and not cwd_in_workspace(cwd_raw):
            stats.dropped_cwd_outside_workspace += 1
            stats.per_project_dropped[project_name] = stats.per_project_dropped.get(project_name, 0) + 1
            continue

        # 3. Strip injected blocks from prompt + redact secrets
        prompt = raw.get("prompt_text") or ""
        if strip_blocks:
            prompt = strip_injected_blocks(prompt)
        prompt = redact_text(prompt) or ""
        if not prompt.strip():
            stats.dropped_empty_prompt_after_strip += 1
            continue

        # 4. Continuation filter
        if drop_cont and is_continuation_prompt(prompt, min_prompt, cont_pattern):
            stats.dropped_continuation += 1
            continue

        # 5. tool_input must be a dict — redact every string leaf before use
        tool_input = raw.get("tool_input")
        if not isinstance(tool_input, dict):
            stats.dropped_bad_tool_input += 1
            continue
        tool_input = redact_json(tool_input)

        # 5b. Length cap — coarse char-based proxy for tokens.
        # Trainer crashes if rendered row exceeds MAX_LENGTH (assistant span
        # gets truncated → zero predicted tokens → masking gate fails).
        # MAX_LENGTH=2048 tokens ~= 6000 chars of content (sys prompt + tools
        # envelope eats ~880 tokens / ~2200 chars). Cap content at 4000 chars
        # to leave margin.
        content_chars = (
            len(prompt or "")
            + len(json.dumps(tool_input, ensure_ascii=False))
            + len(raw.get("tool_response_preview") or "")
        )
        if content_chars > 2000:
            stats.dropped_too_long += 1
            continue

        # 6. Dedup key (use compact JSON for stable hashing) — cwd_raw set above
        dk = (
            prompt.strip(),
            json.dumps(tool_input, sort_keys=True, ensure_ascii=False),
            cwd_raw,
        )
        if dk in dedup_keys:
            stats.dropped_duplicate += 1
            continue
        dedup_keys[dk] = 1

        # 7. Normalize + build row (tool_response redacted before path normalization)
        cwd_norm = normalize_path_string(cwd_raw) if cwd_raw else f"<TRUSTED_ROOT>/{project_name}"
        tool_args_norm = normalize_json_paths(tool_input)
        tool_response = redact_text(raw.get("tool_response_preview") or "") or ""
        tool_response_norm = normalize_path_string(tool_response)
        tool_name = raw.get("tool_name") or "Unknown"

        if tool_name not in seen_tools:
            tools_envelope.append({"type": "function", "function": {"name": tool_name}})
            seen_tools.add(tool_name)

        # Derive subfolder from normalized cwd (e.g., "wfca-app" under "fire-map.wfca.com")
        sub = derive_subfolder(cwd_norm, project_name)
        if sub:
            stats.per_subfolder_kept[f"{project_name}/{sub}"] = (
                stats.per_subfolder_kept.get(f"{project_name}/{sub}", 0) + 1
            )

        row = V5Row(
            project_name=project_name,
            cwd_relative=cwd_norm,
            user_prompt=prompt.strip(),
            tool_turns=[ToolTurn(
                tool_name=tool_name,
                tool_arguments=tool_args_norm,
                tool_response=tool_response_norm,
            )],
            final_reply=None,
            subfolder=sub,
            source_ids=[int(raw["id"])],
            created_at=raw.get("created_at"),
        )

        jsonl_lines.append(render_jsonl_line(row, tools_envelope))
        stats.kept += 1
        stats.per_project_kept[project_name] = stats.per_project_kept.get(project_name, 0) + 1

        if len(sample_rows) < 3:
            sample_rows.append(row)

        if stats.kept >= target:
            break

    return jsonl_lines, stats, sample_rows


# --- Audit writer ------------------------------------------------------------

def write_audit(stats: BuildStats, sample_rows: list[V5Row], cfg: dict, out_path: Path) -> None:
    lines = [
        "# v5 pilot dataset audit",
        "",
        f"- config: `{cfg.get('_config_path', '(unknown)')}`",
        f"- target_clean_rows: {cfg['target_clean_rows']}",
        f"- source_window: {cfg['source_window_recent_rows']}",
        "",
        "## Funnel",
        "",
        f"| stage | count |",
        f"|---|---|",
        f"| db rows pulled | {stats.db_rows_pulled} |",
        f"| dropped: excluded project | {stats.dropped_excluded_project} |",
        f"| dropped: unknown project | {stats.dropped_unknown_project} |",
        f"| dropped: cwd excluded prefix (/tmp etc) | {stats.dropped_cwd_excluded_prefix} |",
        f"| dropped: cwd outside workspace | {stats.dropped_cwd_outside_workspace} |",
        f"| dropped: empty after strip | {stats.dropped_empty_prompt_after_strip} |",
        f"| dropped: continuation prompt | {stats.dropped_continuation} |",
        f"| dropped: bad tool_input | {stats.dropped_bad_tool_input} |",
        f"| dropped: too long (>4k chars) | {stats.dropped_too_long} |",
        f"| dropped: duplicate | {stats.dropped_duplicate} |",
        f"| **kept** | **{stats.kept}** |",
        "",
        "## Per-project kept",
        "",
        "| project | rows |",
        "|---|---|",
    ]
    for proj, n in sorted(stats.per_project_kept.items(), key=lambda x: -x[1]):
        lines.append(f"| {proj} | {n} |")
    lines.append("")
    if stats.per_subfolder_kept:
        lines.append("## Per-subfolder kept (top 30)")
        lines.append("")
        lines.append("| project/subfolder | rows |")
        lines.append("|---|---|")
        for key, n in sorted(stats.per_subfolder_kept.items(), key=lambda x: -x[1])[:30]:
            lines.append(f"| {key} | {n} |")
        lines.append("")
    if stats.per_project_dropped:
        lines.append("## Per-project dropped (excluded)")
        lines.append("")
        lines.append("| project | rows |")
        lines.append("|---|---|")
        for proj, n in sorted(stats.per_project_dropped.items(), key=lambda x: -x[1]):
            lines.append(f"| {proj} | {n} |")
        lines.append("")
    lines.append("## Sample rendered rows (first 3)")
    lines.append("")
    for i, row in enumerate(sample_rows, 1):
        lines.append(f"### Sample {i} — {row.project_name} (id={row.source_ids[0]})")
        lines.append("")
        lines.append("```json")
        # Re-render with empty tools envelope for compactness
        out = json.loads(render_jsonl_line(row, []))
        lines.append(json.dumps(out, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    print(f"[audit] wrote {out_path}")


# --- Main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="Path to v5_pilot.yaml")
    ap.add_argument("--dry-run", action="store_true", help="Print stats, don't write jsonl")
    args = ap.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path
    cfg = load_yaml_flat(config_path)
    cfg["_config_path"] = str(config_path)
    print(f"[config] loaded {config_path}")
    for k, v in cfg.items():
        if not k.startswith("_"):
            print(f"  {k}: {v}")

    jsonl_lines, stats, sample_rows = build(cfg, dry_run=args.dry_run)

    print()
    print("=" * 60)
    print("BUILD STATS")
    print("=" * 60)
    for k, v in asdict(stats).items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for pk, pv in sorted(v.items(), key=lambda x: -x[1])[:15]:
                print(f"    {pk}: {pv}")
        else:
            print(f"  {k}: {v}")

    if args.dry_run:
        print("\n[dry-run] no files written")
        return 0

    out_jsonl = REPO_ROOT / cfg["output_jsonl"]
    out_audit = REPO_ROOT / cfg["output_audit_md"]
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    out_jsonl.write_text("\n".join(jsonl_lines) + "\n")
    print(f"\n[write] {out_jsonl} ({len(jsonl_lines)} rows)")
    write_audit(stats, sample_rows, cfg, out_audit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
