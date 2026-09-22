#!/usr/bin/env python3
"""Validate an aa_quality_gate output document against the canonical schema.

Referenced by CLAUDE.md: "invoke the quality-gate specialist and validate output
with scripts/validate_quality_gate.py before implementation." A non-trivial plan
is not cleared for implementation until its gate output passes this validator.

Schema: schemas/quality-gate-output.schema.json (source contract:
autonomous_agents_mds/agents/quality-gate.md).

Beyond raw schema conformance this also enforces one consistency invariant the
schema alone can't express cheaply: a verdict of "approved" is inconsistent with
any open critical/major gap.

Usage:
    scripts/validate_quality_gate.py <gate-output.json> [...]
    scripts/validate_quality_gate.py            # validates all plans/*quality-gate*.json

Exit codes:
    0  all inputs valid
    1  one or more inputs invalid
    2  usage / IO error
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "quality-gate-output.schema.json"


def _load_json(path: Path) -> dict:
    with path.open() as fh:
        return json.load(fh)


def _consistency_errors(doc: dict) -> list[str]:
    """Invariants the JSON Schema can't express ergonomically."""
    errors: list[str] = []
    verdict = doc.get("verdict")
    gaps = doc.get("gaps_found") or []
    open_major = [g for g in gaps if isinstance(g, dict) and g.get("severity") in ("critical", "major")]
    if verdict == "approved" and open_major:
        sev = ", ".join(sorted({g.get("severity", "?") for g in open_major}))
        errors.append(
            f"verdict 'approved' is inconsistent with {len(open_major)} open {sev} gap(s); "
            "resolve them or downgrade the verdict to 'needs_refinement'."
        )
    if verdict == "blocked" and not doc.get("block_reason"):
        errors.append("verdict 'blocked' requires a non-empty 'block_reason'.")
    return errors


def validate_file(path: Path, validator) -> tuple[bool, list[str]]:
    try:
        doc = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"could not read/parse: {exc}"]

    errors = [f"{e.json_path}: {e.message}" for e in sorted(validator.iter_errors(doc), key=str)]
    errors.extend(_consistency_errors(doc))
    return (not errors), errors


def main(argv: list[str]) -> int:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("FAIL: jsonschema not installed (pip install jsonschema)", file=sys.stderr)
        return 2

    if not SCHEMA_PATH.exists():
        print(f"FAIL: schema not found at {SCHEMA_PATH}", file=sys.stderr)
        return 2

    schema = _load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    targets = [Path(a) for a in argv[1:]]
    if not targets:
        targets = [Path(p) for p in sorted(glob.glob(str(REPO_ROOT / "plans" / "*quality-gate*.json")))]
    if not targets:
        print("FAIL: no gate-output files given and none found under plans/", file=sys.stderr)
        return 2

    all_ok = True
    for path in targets:
        ok, errors = validate_file(path, validator)
        if ok:
            print(f"PASS  {path}")
        else:
            all_ok = False
            print(f"FAIL  {path}")
            for err in errors:
                print(f"        - {err}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
