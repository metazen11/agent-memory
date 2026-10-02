"""Tests for scripts/validate_quality_gate.py — schema conformance + the
consistency invariant CLAUDE.md's quality gate leans on.

Run with:
    python -m pytest tests/test_validate_quality_gate.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_quality_gate as vqg  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402


def _validator() -> Draft202012Validator:
    schema = json.loads(vqg.SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _minimal_doc(**overrides) -> dict:
    """A schema-complete gate doc; override single fields per test."""
    doc = {
        "verdict": "approved",
        "source_reference": {"system": "github", "id": "GH-1"},
        "summary": "does a thing",
        "gaps_found": [],
        "refined_plan": {"objective": "o", "scope": "s", "non_goals": [], "steps": []},
        "acceptance_criteria": [{"criterion": "c", "testable": True, "verification_method": "m"}],
        "testing_plan": {"unit": [], "integration": [], "e2e": [], "negative": [], "regression": []},
        "security_review": {"risks": [], "controls": [], "secrets_handling": "n/a", "auth_requirements": "n/a"},
        "compliance_review": {"applicable_frameworks": ["N/A"], "requirements": []},
        "edge_cases": [],
        "failure_modes": [],
        "observability": {"logs": [], "metrics": [], "alerts": [], "error_messages": []},
        "deployment": {"steps": [], "rollback": []},
        "definition_of_done": ["done"],
        "documentation": {"updates_needed": []},
        "improvements": {"docs_to_update": [], "proposed_ci_checks": [], "agents_md_additions": []},
    }
    doc.update(overrides)
    return doc


def _write(tmp_path: Path, doc: dict) -> Path:
    p = tmp_path / "gate.json"
    p.write_text(json.dumps(doc))
    return p


def test_approved_clean_doc_passes(tmp_path):
    ok, errors = vqg.validate_file(_write(tmp_path, _minimal_doc()), _validator())
    assert ok is True, errors


def test_approved_with_open_critical_gap_fails(tmp_path):
    doc = _minimal_doc(
        gaps_found=[{"gap": "boom", "severity": "critical", "recommendation": "fix"}]
    )
    ok, errors = vqg.validate_file(_write(tmp_path, doc), _validator())
    assert ok is False
    assert any("approved" in e and "critical" in e for e in errors)


def test_needs_refinement_with_gaps_is_fine(tmp_path):
    doc = _minimal_doc(
        verdict="needs_refinement",
        gaps_found=[{"gap": "boom", "severity": "major", "recommendation": "fix"}],
    )
    ok, errors = vqg.validate_file(_write(tmp_path, doc), _validator())
    assert ok is True, errors


def test_blocked_without_reason_fails(tmp_path):
    doc = _minimal_doc(verdict="blocked")  # no block_reason
    ok, errors = vqg.validate_file(_write(tmp_path, doc), _validator())
    assert ok is False
    # caught by schema allOf and/or the consistency check
    assert any("block_reason" in e for e in errors)


def test_blocked_with_reason_passes(tmp_path):
    doc = _minimal_doc(verdict="blocked", block_reason="dangerous")
    ok, errors = vqg.validate_file(_write(tmp_path, doc), _validator())
    assert ok is True, errors


def test_missing_required_field_fails(tmp_path):
    doc = _minimal_doc()
    del doc["acceptance_criteria"]
    ok, errors = vqg.validate_file(_write(tmp_path, doc), _validator())
    assert ok is False
    assert any("acceptance_criteria" in e for e in errors)


def test_bad_verdict_enum_fails(tmp_path):
    doc = _minimal_doc(verdict="approve_with_changes")  # legacy value, not in enum
    ok, errors = vqg.validate_file(_write(tmp_path, doc), _validator())
    assert ok is False


def test_repo_dataset_factory_output_passes():
    """The shipped, contract-conforming gate output must validate."""
    target = REPO_ROOT / "plans" / "dataset-factory-quality-gate.json"
    if not target.exists():
        pytest.skip("dataset-factory-quality-gate.json not present (plans/ is gitignored)")
    ok, errors = vqg.validate_file(target, _validator())
    assert ok is True, errors
