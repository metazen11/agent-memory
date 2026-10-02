"""Path scrubbing for training data (#57).

The v5 pilot model emits absolute paths memorized from its training corpus —
e.g. a `read_file` call targeting a dotenv file deep under
`/Users/<name>/.claude/projects/...` in response to a prompt about an unrelated
source file. A model that reflexively reaches for secret files is a live risk
once it is wired into an agent that executes tool calls.

Measured on `datasets/v5_pilot/train.jsonl`: 545 of 5000 rows (10.9%) carry at
least one absolute home path, dotenv reference, or unexpanded shell
substitution. The earlier redaction pass covered tokens and PII, not paths.

These tests pin the scrubbing behaviour. They must FAIL against a build of
`redact.py` that has no path patterns.
"""

from app.redact import redact_text


def _r(s: str) -> str:
    return redact_text(s) or ""


class TestAbsoluteHomePaths:
    def test_macos_home_is_scrubbed(self):
        out = _r("read /Users/alice/_CODING/proj/config.py")
        assert "/Users/alice" not in out
        assert "config.py" in out, "the meaningful tail must survive"

    def test_linux_home_is_scrubbed(self):
        out = _r("open /home/bob/src/main.rs")
        assert "/home/bob" not in out
        assert "main.rs" in out

    def test_username_does_not_leak(self):
        assert "alice" not in _r("/Users/alice/notes.txt")

    def test_relative_paths_are_untouched(self):
        """Relative paths carry no identity and are the useful signal."""
        s = "edit config/settings.py and src/main.rs"
        assert _r(s) == s

    def test_system_paths_are_untouched(self):
        """/usr, /opt, /tmp are not user-identifying."""
        for s in ("/usr/bin/python3", "/opt/homebrew/bin/git", "/tmp/scratch"):
            assert _r(s) == s, f"{s} should pass through"


class TestSecretFileReferences:
    def test_dotenv_reference_is_flagged(self):
        out = _r("read_file .env")
        assert ".env" not in out or "REDACTED" in out

    def test_dotenv_with_path_is_flagged(self):
        out = _r('{"path": "/Users/carol/app/.env"}')
        assert "/Users/carol" not in out
        assert ".env" not in out or "REDACTED" in out

    def test_env_substring_is_not_a_false_positive(self):
        """'environment' and '.environment' must not be mangled."""
        for s in ("set the environment variable", "docs/environment.md"):
            assert _r(s) == s, f"{s} should pass through"


class TestShellSubstitutions:
    def test_unexpanded_hostname_is_scrubbed(self):
        out = _r("path-$(HOSTNAME)-suffix")
        assert "$(HOSTNAME)" not in out

    def test_unexpanded_date_is_scrubbed(self):
        assert "$(date" not in _r("backup-$(date -u +%Y%m%d).tar")


class TestRealWorldLeak:
    def test_the_exact_leak_observed_from_the_model(self):
        """Verbatim shape of the call the v5 model emitted (#57)."""
        leak = (
            '{"path":"/Users/mz/.claude/projects/'
            '-Users-mz-Dropbox--CODING-app-$(HOSTNAME)-CODING-wfca-app/.env"}'
        )
        out = _r(leak)
        assert "/Users/mz" not in out
        assert "$(HOSTNAME)" not in out
        assert ".env" not in out or "REDACTED" in out
