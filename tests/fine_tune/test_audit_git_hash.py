"""Unit tests for the C8a-D fabricated-commit-hash audit check.

Pure-logic + real-git tests — no model required. Run with:
    .venv-finetune/bin/python -m pytest tests/fine_tune/test_audit_git_hash.py -v

These lock in the behaviour of GIT_COMMIT_REF_RE + _git_hash_exists + the
c8a_fabricated_hashes hard gate, which previously existed only as an unused
regex (GIT_HASH_RE) with no invocation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "fine_tune"))

import audit_dataset  # noqa: E402
from audit_dataset import GIT_COMMIT_REF_RE, audit  # noqa: E402


def _real_commit() -> str:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _row(assistant_text: str) -> dict:
    return {"messages": [
        {"role": "user", "content": "what commit fixed it?"},
        {"role": "assistant", "content": assistant_text},
    ]}


def _write_jsonl(tmp_path: Path, rows: list[dict]) -> Path:
    f = tmp_path / "sample.jsonl"
    with f.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return f


@pytest.fixture(autouse=True)
def _clear_cache():
    audit_dataset._GIT_HASH_CACHE.clear()
    yield
    audit_dataset._GIT_HASH_CACHE.clear()


# --- regex scoping -------------------------------------------------------

def test_regex_matches_commit_context():
    m = GIT_COMMIT_REF_RE.search("fixed in commit 7051296 last night")
    assert m and m.group(1) == "7051296"


def test_regex_matches_sha_and_rev_keywords():
    assert GIT_COMMIT_REF_RE.search("sha abcdef1 landed it").group(1) == "abcdef1"
    assert GIT_COMMIT_REF_RE.search("at commit deadbeef").group(1) == "deadbeef"


def test_regex_ignores_bare_hex_without_commit_context():
    # A 7+ hex run that is NOT a commit reference must not be flagged.
    assert GIT_COMMIT_REF_RE.search("the checksum was 0badf00dcafe done") is None
    assert GIT_COMMIT_REF_RE.search("id abc1234 in the table") is None


# --- end-to-end audit behaviour -----------------------------------------

def test_real_commit_hash_passes(tmp_path):
    real = _real_commit()
    f = _write_jsonl(tmp_path, [_row(f"That was fixed in commit {real}.")])
    passed, report = audit(f, verify_paths=False, verify_hashes=True, repo_root=REPO_ROOT)
    assert report["counts"].get("c8a_fabricated_hashes", 0) == 0
    assert passed is True


def test_fabricated_commit_hash_is_flagged_and_fails_gate(tmp_path):
    # 'deadbeefcafe' is valid hex, 12 chars, but not a real object in this repo.
    rows = [_row("The bug was introduced in commit deadbeefcafe and never existed.")]
    f = _write_jsonl(tmp_path, rows)
    passed, report = audit(f, verify_paths=False, verify_hashes=True, repo_root=REPO_ROOT)
    assert report["counts"].get("c8a_fabricated_hashes", 0) == 1
    # single row, 1/1 = 100% > 5% hard gate → build must FAIL
    assert passed is False
    fails = {x["category"] for x in report["failures"]}
    assert "c8a_fabricated_hashes" in fails


def test_verify_hashes_false_skips_the_check(tmp_path):
    f = _write_jsonl(tmp_path, [_row("commit deadbeefcafe is fake")])
    passed, report = audit(f, verify_paths=False, verify_hashes=False, repo_root=REPO_ROOT)
    assert report["counts"].get("c8a_fabricated_hashes", 0) == 0
    assert passed is True


def test_bare_hex_not_flagged_even_when_verifying(tmp_path):
    # No commit-context word → not a commit citation → never checked/flagged.
    f = _write_jsonl(tmp_path, [_row("the file checksum 0badf00dcafe matched")])
    passed, report = audit(f, verify_paths=False, verify_hashes=True, repo_root=REPO_ROOT)
    assert report["counts"].get("c8a_fabricated_hashes", 0) == 0
    assert passed is True
