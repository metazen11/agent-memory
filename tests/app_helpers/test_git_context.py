"""Unit tests for app.git_context.resolve_git_context.

Real subprocess git calls against throwaway repos. Slower than mocking
but exercises the actual git CLI behaviour the production writer relies
on.

`tmp_path` paths are themselves under pytest's tmp tree, which the
production ephemeral-path regex catches by design. To keep these tests
deterministic, the git-repo fixtures override that check by patching
the regex during the test (it's still exercised end-to-end in the
ephemeral-path test class with hard-coded paths).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import git_context as gc
from app.git_context import (
    GitContext,
    _strip_url_userinfo,
    clear_cache,
    resolve_git_context,
)


# ---- Fixtures --------------------------------------------------------------

def _run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def disable_ephemeral_check(monkeypatch):
    """Make _is_ephemeral always return False for tmp_path-based tests.

    pytest's tmp_path lives under /private/var/folders/.../pytest-of-USER/
    which legitimately matches the production ephemeral regex. For tests
    that need to verify git resolution against a real tmp repo, we
    short-circuit that check.
    """
    monkeypatch.setattr(gc, "_is_ephemeral", lambda cwd: False)


@pytest.fixture
def git_repo(tmp_path: Path, disable_ephemeral_check) -> Path:
    """A tmp git repo with one commit, origin remote, HEAD on `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.email", "t@t")
    _run(repo, "git", "config", "user.name", "t")
    _run(repo, "git", "remote", "add", "origin", "https://github.com/me/r.git")
    (repo / "README").write_text("hi")
    _run(repo, "git", "add", "README")
    _run(repo, "git", "commit", "-q", "-m", "init")
    return repo


# ---- Tests -----------------------------------------------------------------

class TestResolveGitContext:

    def test_returns_canonical_root_for_repo(self, git_repo: Path):
        ctx = resolve_git_context(str(git_repo))
        assert ctx.canonical_root_path == str(git_repo.resolve())
        assert ctx.source_kind == "git"

    def test_subdir_resolves_to_repo_root(self, git_repo: Path):
        sub = git_repo / "src" / "deep"
        sub.mkdir(parents=True)
        ctx = resolve_git_context(str(sub))
        assert ctx.canonical_root_path == str(git_repo.resolve())

    def test_branch_and_sha_captured(self, git_repo: Path):
        ctx = resolve_git_context(str(git_repo))
        assert ctx.branch == "main"
        assert ctx.sha is not None
        assert len(ctx.sha) == 40

    def test_branch_is_none_when_head_is_detached(self, git_repo: Path):
        sha = subprocess.check_output(
            ["git", "-C", str(git_repo), "rev-parse", "HEAD"], text=True
        ).strip()
        _run(git_repo, "git", "checkout", "-q", sha)
        clear_cache()
        ctx = resolve_git_context(str(git_repo))
        assert ctx.branch is None
        assert ctx.sha == sha

    def test_remote_url_captured(self, git_repo: Path):
        ctx = resolve_git_context(str(git_repo))
        assert ctx.git_remote == "https://github.com/me/r.git"

    def test_remote_fallback_to_upstream(self, tmp_path: Path, disable_ephemeral_check):
        repo = tmp_path / "noorigin"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "main")
        _run(repo, "git", "config", "user.email", "t@t")
        _run(repo, "git", "config", "user.name", "t")
        _run(repo, "git", "remote", "add", "upstream", "https://github.com/u/r.git")
        (repo / "x").write_text("x")
        _run(repo, "git", "add", "x")
        _run(repo, "git", "commit", "-q", "-m", "i")
        ctx = resolve_git_context(str(repo))
        assert ctx.git_remote == "https://github.com/u/r.git"

    def test_remote_none_when_no_remotes(self, tmp_path: Path, disable_ephemeral_check):
        repo = tmp_path / "noremote"
        repo.mkdir()
        _run(repo, "git", "init", "-q", "-b", "main")
        _run(repo, "git", "config", "user.email", "t@t")
        _run(repo, "git", "config", "user.name", "t")
        (repo / "x").write_text("x")
        _run(repo, "git", "add", "x")
        _run(repo, "git", "commit", "-q", "-m", "i")
        ctx = resolve_git_context(str(repo))
        assert ctx.source_kind == "git"
        assert ctx.git_remote is None

    def test_worktree_remote_matches_parent_repo(
        self, git_repo: Path, tmp_path: Path, disable_ephemeral_check
    ):
        wt = tmp_path / "wt"
        _run(git_repo, "git", "worktree", "add", "-q", "-b", "feature", str(wt))
        ctx = resolve_git_context(str(wt))
        # The dedupe key across worktree + main repo is git_remote — both
        # report the same origin URL, so reads can collapse them.
        assert ctx.git_remote == "https://github.com/me/r.git"
        assert ctx.branch == "feature"
        assert ctx.source_kind == "git"


class TestNonGitAndEphemeral:
    """Hard-coded paths exercise the ephemeral regex end-to-end."""

    def test_pytest_of_user_macos_is_ephemeral(self):
        # Exactly the shape that polluted mem_projects today.
        ctx = resolve_git_context(
            "/private/var/folders/lg/abc/T/pytest-of-mz/pytest-398/test_x"
        )
        assert ctx.source_kind == "ephemeral"
        assert ctx.canonical_root_path is None

    def test_pytest_of_user_linux_is_ephemeral(self):
        ctx = resolve_git_context("/tmp/pytest-of-runner/pytest-1/test_y")
        assert ctx.source_kind == "ephemeral"

    def test_mktemp_dot_is_ephemeral(self):
        ctx = resolve_git_context("/tmp/tmp.AbCdEf123/scratch")
        assert ctx.source_kind == "ephemeral"

    def test_user_repo_named_pytest_plugin_is_not_ephemeral(self):
        # A real project that happens to contain the string "pytest" should
        # not be silently dropped.
        ctx = resolve_git_context("/Users/me/projects/pytest-plugin-foo")
        # Path doesn't exist; falls back to non-git.
        assert ctx.source_kind == "non-git"

    def test_missing_cwd_does_not_crash(self):
        ctx = resolve_git_context("/does/not/exist/anywhere/12345")
        assert ctx == GitContext(None, None, None, None, "non-git")

    def test_empty_or_none_cwd(self):
        assert resolve_git_context("").source_kind == "non-git"
        assert resolve_git_context(None).source_kind == "non-git"


class TestEphemeralRegex:
    """Direct regex unit tests so accidental loosening is caught."""

    @pytest.mark.parametrize("path", [
        "/private/var/folders/lg/abc/T/pytest-of-mz/pytest-1/test_a",
        "/tmp/pytest-of-runner/pytest-99/foo",
        "/tmp/tmp.XYZ123/scratch",
    ])
    def test_matches(self, path: str):
        assert gc._EPHEMERAL_PATH_RE.match(path)

    @pytest.mark.parametrize("path", [
        "/Users/me/_CODING/agentMemory",
        "/Users/me/projects/pytest-plugin-mock",
        "/tmp/my-real-work",
        "/private/var/folders/lg/abc/T/Something/else",
        "/home/runner/work/repo",
        "",
    ])
    def test_does_not_match(self, path: str):
        assert not gc._EPHEMERAL_PATH_RE.match(path)


class TestRedaction:

    def test_strip_url_userinfo_with_token(self):
        out = _strip_url_userinfo(
            "https://x-token-auth:secret123@github.com/me/r.git"
        )
        assert "secret123" not in out
        assert out == "https://github.com/me/r.git"

    def test_strip_url_userinfo_passthrough_for_ssh(self):
        out = _strip_url_userinfo("git@github.com:me/r.git")
        assert out == "git@github.com:me/r.git"

    def test_strip_url_userinfo_passthrough_for_plain_https(self):
        out = _strip_url_userinfo("https://github.com/me/r.git")
        assert out == "https://github.com/me/r.git"


class TestCache:

    def test_cache_returns_same_object(self, git_repo: Path):
        a = resolve_git_context(str(git_repo))
        b = resolve_git_context(str(git_repo))
        assert a is b

    def test_clear_cache_forces_re_resolution(self, git_repo: Path):
        a = resolve_git_context(str(git_repo))
        clear_cache()
        b = resolve_git_context(str(git_repo))
        assert a == b
        assert a is not b
