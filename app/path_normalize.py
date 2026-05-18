"""Path normalization at the write boundary.

Per CLAUDE.md, the source of truth is ``~/_CODING/``; ``~/Dropbox/_CODING/``
is a stale backup mirror. After migration 014, all existing data was
normalized to local paths. This module prevents new writes from
re-introducing Dropbox paths.

Apply to:
  * mem_projects.full_path / canonical_root_path  (via app.project.ensure_project)
  * mem_tool_calls.cwd / tool_input / tool_response_preview
                                                (via backfill + hooks ingest)
  * mem_user_prompts.prompt_text                (via /api/prompts)
  * backfill_log.jsonl_path

The rewrite is purely string-level — no resolution, no FS access — so it's
safe to call on hot ingest paths.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# The exact prefixes the v4 model memorized as project roots.
_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/Users/mz/Dropbox/_CODING/", "/Users/mz/_CODING/"),
    ("~/Dropbox/_CODING/",          "~/_CODING/"),
    # JSON-encoded variant (escaped slashes never appear in our jsonb, but
    # cover URL-escaped forms occasionally seen in text fields).
    ("%2FUsers%2Fmz%2FDropbox%2F_CODING%2F", "%2FUsers%2Fmz%2F_CODING%2F"),
)

# Pre-compiled regex for "did this string change?" probe (faster than
# running each replace blindly when 99% of strings don't match).
_PROBE_RE = re.compile(r"(?:/Users/mz|~)/Dropbox/_CODING/")


def normalize_text(s: str | None) -> str | None:
    """Rewrite any Dropbox-rooted path in s to the local _CODING equivalent.

    Returns the same object if no change (str-identity safe for callers
    that compare ``is``).
    """
    if not s or not _PROBE_RE.search(s):
        return s
    out = s
    for old, new in _PREFIXES:
        if old in out:
            out = out.replace(old, new)
    return out


def normalize_json(obj: Any) -> Any:
    """Recursively normalize all string values in a dict / list / scalar.

    Returns the same object if no change. Does NOT deep-copy when no
    normalization is needed.
    """
    if isinstance(obj, str):
        return normalize_text(obj)
    if isinstance(obj, Mapping):
        changed = False
        out: dict[str, Any] = {}
        for k, v in obj.items():
            new_v = normalize_json(v)
            if new_v is not v:
                changed = True
            out[k] = new_v
        return out if changed else obj
    if isinstance(obj, list):
        changed = False
        out_list = []
        for item in obj:
            new_item = normalize_json(item)
            if new_item is not item:
                changed = True
            out_list.append(new_item)
        return out_list if changed else obj
    return obj


def contains_dropbox_path(s: str | None) -> bool:
    """Probe — for asserts / tests / pre-insert sanity checks."""
    if not s:
        return False
    return bool(_PROBE_RE.search(s))


def iter_dropbox_paths_in(obj: Any) -> Iterable[str]:
    """Walk a value tree, yield each Dropbox-path string for diagnostics."""
    if isinstance(obj, str):
        if contains_dropbox_path(obj):
            yield obj
        return
    if isinstance(obj, Mapping):
        for v in obj.values():
            yield from iter_dropbox_paths_in(v)
        return
    if isinstance(obj, list):
        for v in obj:
            yield from iter_dropbox_paths_in(v)
