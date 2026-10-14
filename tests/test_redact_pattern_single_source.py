"""#57 regression guards: redaction patterns must have exactly ONE definition.

The v5 pilot model memorized absolute home paths and emitted them as
tool-call arguments. Root cause was not a missing pattern — PATH_PATTERNS
existed in app.redact — but a hand-copied fallback stub in the dataset
builder that carried only SECRET_PATTERNS. Every build that fell back to it
(no .env, e.g. CI) silently wrote unscrubbed paths into the training set.

These tests make that drift structurally impossible rather than remembered.
"""
import builtins
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tracked_py():
    """Tracked .py files only: rglob also walks .venv*, nested agent worktrees
    under .claude/worktrees/, and other untracked copies that are not this repo."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [rel for rel in out.split("\0") if rel]


def test_patterns_import_without_config():
    """The pattern module must not depend on app.config (no .env in CI)."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.modules['app.config']=None;"
         "from app.redact_patterns import SECRET_PATTERNS, PATH_PATTERNS, PII_PATTERNS;"
         "print(len(SECRET_PATTERNS), len(PATH_PATTERNS), len(PII_PATTERNS))"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert out.returncode == 0, f"pattern module needs config: {out.stderr}"


def test_exactly_one_definition_of_each_pattern_list():
    """A second definition anywhere is the drift that caused #57."""
    tracked = _tracked_py()
    for name in ("SECRET_PATTERNS", "PATH_PATTERNS", "PII_PATTERNS"):
        hits = []
        for rel in tracked:
            path = REPO_ROOT / rel
            for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                if re.match(rf"\s*{name}\s*(?::[^=]+)?=\s*\[", line):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{i}")
        assert len(hits) == 1, f"{name} defined {len(hits)}x (must be 1):\n" + "\n".join(hits)


def test_no_stray_home_path_regex_outside_pattern_module():
    """A renamed or reshaped copy evades the name check; the regex literal can't.

    Only string-literal patterns are flagged. Patterns built with rf-strings from
    app.redact_patterns or Path.home() carry no hardcoded username and are fine.
    """
    offenders = [
        rel for rel in _tracked_py()
        if rel != "app/redact_patterns.py"
        and re.search(r"re\.compile\(\s*r?[\"']/\(?\??:?Users", (REPO_ROOT / rel).read_text(errors="ignore"))
    ]
    assert not offenders, "home-path regex defined outside app/redact_patterns.py:\n" + "\n".join(offenders)


def test_dataset_builder_fallback_scrubs_home_paths(monkeypatch):
    """With app.redact unimportable, the builder must STILL scrub paths."""
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "app.redact":
            raise RuntimeError("simulated: no .env in CI")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    modname = "scripts.fine_tune.build_v5_pilot_dataset"
    for m in [m for m in list(sys.modules) if m.startswith(modname)]:
        monkeypatch.delitem(sys.modules, m, raising=False)
    mod = importlib.import_module(modname)
    try:
        assert "/Users/mz" not in mod.redact_text("/Users/mz/_CODING/x.py")
        nested = mod.redact_json({"file_path": "/Users/mz/.claude/projects/a/.env"})
        assert "/Users/mz" not in str(nested)
    finally:
        # the fallback-flavoured module must not leak into later tests
        sys.modules.pop(modname, None)


@pytest.mark.parametrize("raw,must_not_contain", [
    ("/Users/mz/_CODING/agentMemory/x.py", "/Users/mz"),
    ("/home/someone/proj/y.py", "/home/someone"),
    ("cat /Users/mz/.env", ".env"),
])
def test_identifying_paths_are_scrubbed(raw, must_not_contain):
    from app.redact import redact_text
    assert must_not_contain not in redact_text(raw)


@pytest.mark.parametrize("keep", [
    "config/settings.py",      # relative — the useful signal
    "/usr/local/bin/python",   # system — carries no identity
    "docs/environment.md",     # must NOT trip the .env pattern
])
def test_non_identifying_paths_are_preserved(keep):
    """Over-scrubbing would gut the training data to no benefit (#57 scope)."""
    from app.redact import redact_text
    assert redact_text(keep) == keep
