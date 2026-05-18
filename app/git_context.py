"""Resolve a cwd to its canonical git context for project consolidation.

Returns the git root, remote, branch, and SHA at the moment of call.
Results are cached per cwd within a single process for the lifetime of the
session — branch/sha can change mid-session, so the TTL is intentionally
short. The cache exists only to avoid running git 4× per tool call when
many calls hit the same cwd in a tight loop.

See issue #36 and docs/fine_tune/V2_DATA_PIPELINE_PLAN.md for design notes.
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.redact import redact_text

GitSourceKind = Literal["git", "non-git", "ephemeral"]

# Match the specific shapes that come from test runners and short-lived
# scratch space, not anything that merely contains "pytest" or "tmp" — a
# user might legitimately work in a repo at /home/user/projects/pytest-foo
# and we should not silently drop their tool calls.
_EPHEMERAL_PATH_RE = re.compile(
    r"^(?:/private)?(?:"
    r"/var/folders/[^/]+/[^/]+/T/pytest-of-"   # macOS pytest-of-USER tmp
    r"|/tmp/pytest-of-"                        # Linux pytest-of-USER tmp
    r"|/tmp/tmp\.[A-Za-z0-9]+/"                # mktemp-style scratch
    r")",
    re.IGNORECASE,
)

# Strip userinfo (`user:token@`) from URL remotes before storage. The
# connection_string pattern in app.redact catches postgresql://user:pw@host
# but not https://x-token:secret@github.com — handle that here explicitly.
_URL_USERINFO_RE = re.compile(r"(https?://)([^/@]+@)(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class GitContext:
    """Resolved git context for a cwd.

    Attributes
    ----------
    canonical_root_path:
        Absolute path to the git toplevel for the cwd. NULL when the cwd
        isn't under a git repo. For worktrees, this is the parent repo's
        root (git rev-parse --show-toplevel handles this natively).
    git_remote:
        URL of the origin remote with embedded credentials stripped, or
        None if the cwd has no remote / no origin / isn't a git repo.
    branch:
        Branch name at the moment of resolution, or None.
    sha:
        Commit SHA at HEAD, or None.
    source_kind:
        'git' for repos, 'non-git' for real cwds without a repo,
        'ephemeral' for pytest/tmp paths that should be excluded from
        training-data export.
    """
    canonical_root_path: str | None
    git_remote: str | None
    branch: str | None
    sha: str | None
    source_kind: GitSourceKind


# Process-local cache: cwd -> (GitContext, monotonic_at)
_CACHE: dict[str, tuple[GitContext, float]] = {}
_CACHE_TTL_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 1024


def _git(cwd: str, *args: str) -> str | None:
    """Run `git <args>` in cwd. Return stripped stdout, or None on failure.

    Failures are silent by design: a missing repo, a removed cwd, or any
    other git error degrades to None on the relevant context field.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _strip_url_userinfo(remote: str) -> str:
    """Remove `user:token@` from an HTTPS remote URL."""
    m = _URL_USERINFO_RE.match(remote)
    if not m:
        return remote
    return f"{m.group(1)}{m.group(3)}"


def _is_ephemeral(cwd: str) -> bool:
    return bool(_EPHEMERAL_PATH_RE.match(cwd))


def _resolve_remote(cwd: str) -> str | None:
    """Return the canonical origin remote URL, redacted of any creds.

    Fallback chain: origin → upstream → first remote → None.
    """
    for name in ("origin", "upstream"):
        url = _git(cwd, "remote", "get-url", name)
        if url:
            return redact_text(_strip_url_userinfo(url))
    remotes = _git(cwd, "remote")
    if not remotes:
        return None
    first = remotes.splitlines()[0].strip()
    if not first:
        return None
    url = _git(cwd, "remote", "get-url", first)
    if not url:
        return None
    return redact_text(_strip_url_userinfo(url))


def _resolve_uncached(cwd: str) -> GitContext:
    """Resolve cwd to a GitContext without consulting the cache."""
    if not cwd:
        return GitContext(None, None, None, None, "non-git")

    if _is_ephemeral(cwd):
        return GitContext(None, None, None, None, "ephemeral")

    # Existence check — a removed cwd shouldn't crash the writer.
    if not Path(cwd).exists():
        return GitContext(None, None, None, None, "non-git")

    root = _git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return GitContext(None, None, None, None, "non-git")

    remote = _resolve_remote(root)
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        # Detached HEAD — no branch name.
        branch = None
    sha = _git(root, "rev-parse", "HEAD")

    return GitContext(
        canonical_root_path=root,
        git_remote=remote,
        branch=branch,
        sha=sha,
        source_kind="git",
    )


def resolve_git_context(cwd: str | None) -> GitContext:
    """Resolve a cwd to its git context, with a short-lived per-process cache.

    Cache TTL is 30s — long enough to absorb tight loops of tool calls in
    the same cwd, short enough that a branch switch is reflected within
    half a minute. The cache is bounded to 1024 entries; entries past
    the bound are evicted by oldest insert.
    """
    if cwd is None:
        return GitContext(None, None, None, None, "non-git")

    now = time.monotonic()
    cached = _CACHE.get(cwd)
    if cached is not None:
        ctx, at = cached
        if now - at < _CACHE_TTL_SECONDS:
            return ctx

    ctx = _resolve_uncached(cwd)

    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        # Drop the oldest entry to bound memory.
        oldest_key = min(_CACHE, key=lambda k: _CACHE[k][1])
        _CACHE.pop(oldest_key, None)
    _CACHE[cwd] = (ctx, now)
    return ctx


def clear_cache() -> None:
    """Test helper: drop all cached entries."""
    _CACHE.clear()
