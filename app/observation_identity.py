"""Canonical identity for idempotent observation writes."""

import json


def observation_dedupe_key(
    project_id: int,
    title: str,
    subtitle: str | None,
    obs_type: str,
    narrative: str | None,
    facts: object,
    concepts: object,
    files_read: object,
    files_modified: object,
    raw_text: str,
) -> str:
    """Serialize semantic fields; provenance embedded in raw_text is excluded."""
    return json.dumps(
        [project_id, title, subtitle, obs_type, narrative, facts, concepts,
         files_read, files_modified],
        sort_keys=True,
        separators=(",", ":"),
    )
