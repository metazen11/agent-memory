"""Delivery gates reject unrelated CI, tampering, unsafe archives and roll back."""

import hashlib
import io
import json
from pathlib import Path
import tarfile

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
