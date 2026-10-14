"""Delivery gates reject unrelated CI, tampering, unsafe archives and roll back."""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts import deploy_release as delivery


def artifact(tmp_path, name="app/file.py"):
    source = tmp_path / "artifact"
    source.mkdir()
    with tarfile.open(source / "source.tar", "w") as bundle:
        member = tarfile.TarInfo(name)
        member.size = 1
        bundle.addfile(member, io.BytesIO(b"x"))
    (source / "release.json").write_text(
        json.dumps(
            {
                "sha": "abc",
                "requirements_sha256": "runtime",
                "archive_sha256": hashlib.sha256(
                    (source / "source.tar").read_bytes()
                ).hexdigest(),
            }
        )
    )
    return source


def test_only_exact_successful_main_push_can_deploy():
    base = {
        "headSha": "abc",
        "headBranch": "main",
        "event": "push",
        "conclusion": "success",
    }
    assert delivery.eligible_run([base], "abc") == base
    for change in [
        {"headSha": "other"},
        {"headBranch": "dev"},
        {"event": "pull_request"},
        {"conclusion": "failure"},
    ]:
        assert delivery.eligible_run([{**base, **change}], "abc") is None


@pytest.mark.parametrize("problem", ["sha", "runtime", "checksum", "traversal"])
def test_rejects_invalid_release(tmp_path, problem):
    source = artifact(
        tmp_path, "../escape" if problem == "traversal" else "app/file.py"
    )
    if problem == "checksum":
        (source / "source.tar").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        delivery.unpack(
            source,
            tmp_path / "release",
            "wrong" if problem == "sha" else "abc",
            "wrong" if problem == "runtime" else "runtime",
        )
    assert not (tmp_path / "escape").exists()


def test_failed_candidate_restores_previous_release(tmp_path, monkeypatch):
    source = artifact(tmp_path)
    previous = tmp_path / "previous"
    previous.mkdir()
    calls = []

    def activate(path, python, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("not ready")

    monkeypatch.setattr(delivery, "activate", activate)
    state = {
        "active": str(previous),
        "python": "/tmp/venv/bin/python",
        "requirements_sha256": "runtime",
    }
    with pytest.raises(RuntimeError, match="not ready"):
        delivery.deploy(source, "abc", state, tmp_path)
    assert len(calls) == 2
    assert calls[1] == Path(state["active"])


def migration_release(tmp_path, monkeypatch, approvals=None, edit_existing=False):
    """Active release has 001; candidate keeps 001 and adds 002 (or edits 001)."""
    files = {"001-old.sql": b"changed" if edit_existing else b"old", "002-new.sql": b"x"}
    source = tmp_path / "artifact"
    source.mkdir()
    with tarfile.open(source / "source.tar", "w") as bundle:
        for name, body in files.items():
            member = tarfile.TarInfo(f"scripts/migrations/{name}")
            member.size = len(body)
            bundle.addfile(member, io.BytesIO(body))
    (source / "release.json").write_text(json.dumps({
        "sha": "abc", "requirements_sha256": "runtime",
        "archive_sha256": hashlib.sha256((source / "source.tar").read_bytes()).hexdigest(),
    }))
    previous = tmp_path / "previous"
    (previous / "scripts/migrations").mkdir(parents=True)
    (previous / "scripts/migrations/001-old.sql").write_bytes(b"old")
    if approvals is not None:
        (tmp_path / "approved-migrations.json").write_text(json.dumps(approvals))
    monkeypatch.setattr(delivery, "activate", lambda *a, **k: None)
    state = {"active": str(previous), "python": str(tmp_path / "venv/bin/python"),
             "requirements_sha256": "runtime"}
    return source, state


def test_unapproved_new_migration_is_refused(tmp_path, monkeypatch):
    source, state = migration_release(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="002-new.sql"):
        delivery.deploy(source, "abc", state, tmp_path)


def test_approved_new_migration_deploys(tmp_path, monkeypatch):
    approval = {"002-new.sql": hashlib.sha256(b"x").hexdigest()}
    source, state = migration_release(tmp_path, monkeypatch, approval)
    assert delivery.deploy(source, "abc", state, tmp_path)["sha"] == "abc"


def test_approval_does_not_cover_different_content(tmp_path, monkeypatch):
    source, state = migration_release(tmp_path, monkeypatch, {"002-new.sql": "stale"})
    with pytest.raises(ValueError, match="002-new.sql"):
        delivery.deploy(source, "abc", state, tmp_path)


def test_edited_or_removed_applied_migration_is_always_refused(tmp_path, monkeypatch):
    approval = {"002-new.sql": hashlib.sha256(b"x").hexdigest()}
    source, state = migration_release(tmp_path, monkeypatch, approval, edit_existing=True)
    with pytest.raises(ValueError, match="001-old.sql"):
        delivery.deploy(source, "abc", state, tmp_path)
