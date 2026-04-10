"""Unit tests for dataset export builders (no live DB required)."""

from datetime import datetime, timezone

from app.dataset_exports import build_dataset_records


def _row(*, idx: int, ok: bool, prompt: str, project: str = "proj") -> dict:
    return {
        "id": idx,
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi" if ok else "badcmd"},
        "tool_response_preview": "ok" if ok else "Error: command not found",
        "tool_success": ok,
        "tool_error": None if ok else "Error: command not found",
        "prompt_text": prompt,
        "source_system": "tests",
        "source_mode": "unit",
        "source_agent": "pytest",
        "observation_id": 100 + idx if ok else None,
        "created_at": datetime.now(timezone.utc),
        "project_name": project,
        "project_path": f"/tmp/{project}",
        "session_external_id": "s-1",
        "session_status": "completed" if ok else "failed",
        "last_user_message": prompt,
        "obs_title": "Observation title" if ok else None,
        "obs_type": "feature" if ok else None,
        "obs_narrative": "Did the thing" if ok else None,
        "obs_facts": ["f1"] if ok else None,
        "obs_concepts": ["c1"] if ok else None,
    }


def test_sft_excludes_problematic_when_disabled():
    rows = [
        _row(idx=1, ok=True, prompt="run command"),
        _row(idx=2, ok=False, prompt="run command"),
    ]
    out = build_dataset_records(
        rows,
        dataset_type="sft",
        include_errors=False,
        include_observations=True,
    )
    assert len(out) == 1
    assert out[0]["meta"]["is_problematic"] is False


def test_trajectory_includes_observation_payload():
    rows = [_row(idx=1, ok=True, prompt="run command")]
    out = build_dataset_records(
        rows,
        dataset_type="trajectory",
        include_errors=True,
        include_observations=True,
    )
    assert len(out) == 1
    step = out[0]["trajectory"][0]
    assert step["observation"]["title"] == "Observation title"


def test_preference_pairs_success_and_failure():
    rows = [
        _row(idx=1, ok=True, prompt="same prompt"),
        _row(idx=2, ok=False, prompt="same prompt"),
    ]
    out = build_dataset_records(
        rows,
        dataset_type="preference",
        include_errors=True,
        include_observations=False,
    )
    assert len(out) == 1
    pair = out[0]
    assert pair["chosen"]["reward"] >= pair["rejected"]["reward"]

