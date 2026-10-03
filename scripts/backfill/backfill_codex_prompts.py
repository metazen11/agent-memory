#!/usr/bin/env python3
"""Restore human prompts from Codex rollout logs; dry-run unless --commit.

Uses psql_wrapper.sh for all DB access. Per-session atomic, paced imports keep
original timestamps, native session IDs and canonical project paths. Repeated
human turns survive; injected context and delegated-agent conversations do not.
"""

from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(ROOT))
from app.git_context import resolve_git_context  # noqa: E402
from app.path_normalize import normalize_text  # noqa: E402
from app.redact import redact_text  # noqa: E402

CONTEXT_PREFIXES = (
    "# AGENTS.md instructions for ",
    "<environment_context>",
    "<recommended_plugins>",
    "<external_codex_apps_open_page>",
    "<task-notification>",
    "<subagent_notification>",
    "<turn_aborted>",
    "<codex_internal_context",
    "The following is the Codex agent history",
)


def parse_transcript(file: Path) -> tuple[list[dict], Counter]:
    rows = []
    counts = Counter()
    sid = cwd = None
    delegated = False
    with file.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                counts["malformed"] += 1
                continue
            if not isinstance(record, dict) or not isinstance(
                record.get("payload"), dict
            ):
                counts["malformed"] += 1
                continue
            payload = record["payload"]
            kind = record.get("type")
            if kind == "session_meta":
                sid, cwd = payload.get("id"), payload.get("cwd")
                source = payload.get("source")
                delegated = isinstance(source, dict) and "subagent" in source
            elif kind == "turn_context" and payload.get("cwd"):
                cwd = payload["cwd"]
            elif (
                kind == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                if delegated:
                    counts["delegated"] += 1
                    continue
                content = payload.get("content") or []
                text = "\n".join(
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") in ("input_text", "text")
                ).strip()
                if not text or text.startswith(CONTEXT_PREFIXES):
                    counts["context_or_empty"] += 1
                    continue
                timestamp = record.get("timestamp")
                try:
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError("naive timestamp")
                except (AttributeError, ValueError):
                    counts["missing_timestamp"] += 1
                    continue
                if not sid or not cwd or not Path(cwd).is_absolute():
                    counts["missing_identity"] += 1
                    continue
                text = normalize_text(redact_text(text.replace("\x00", ""))) or ""
                if not text:
                    continue
                rows.append(
                    {
                        "sid": sid,
                        "cwd": normalize_text(cwd.rstrip("/") + "/").rstrip("/") or "/",
                        "text": text,
                        "timestamp": parsed.astimezone(timezone.utc).isoformat(),
                        "hash": hashlib.sha256(text.encode()).hexdigest(),
                    }
                )
    return rows, counts


def psql(sql: str) -> str:
    proc = subprocess.run(
        [str(ROOT / "scripts/psql_wrapper.sh"), "-qAt", "-v", "ON_ERROR_STOP=1"],
        input=sql,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def literal(value) -> str:
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def prompt_identity(row: dict) -> tuple[str, str, str, str]:
    """An occurrence belongs to one session, project and source timestamp."""
    timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    return (
        row["sid"],
        row["cwd"],
        row["hash"],
        timestamp.astimezone(timezone.utc).isoformat(),
    )


def existing_occurrences() -> set[tuple[str, str, str, str]]:
    raw = psql(
        "SELECT COALESCE(json_agg(x),'[]'::json) FROM ("
        "SELECT s.session_id, p.prompt_text, p.created_at, pr.full_path "
        "FROM mem_user_prompts p JOIN mem_sessions s ON s.id=p.session_id "
        "JOIN mem_projects pr ON pr.id=p.project_id "
        "WHERE p.agent_name LIKE 'codex%') x;"
    )
    occurrences = set()
    for row in json.loads(raw):
        text = normalize_text(redact_text(row["prompt_text"])) or ""
        occurrences.add(
            prompt_identity(
                {
                    "sid": row["session_id"],
                    "cwd": normalize_text(row["full_path"]),
                    "hash": hashlib.sha256(text.encode()).hexdigest(),
                    "timestamp": row["created_at"],
                }
            )
        )
    return occurrences


def plan(
    rows: list[dict], existing: set[tuple[str, str, str, str]]
) -> tuple[dict[str, list[dict]], int]:
    sessions = defaultdict(list)
    skipped = 0
    seen = set()
    ordinals = Counter()
    projects = {}
    for row in sorted(rows, key=lambda r: (r["sid"], r["timestamp"])):
        cwd = row["cwd"]
        if cwd not in projects:
            projects[cwd] = (
                normalize_text(resolve_git_context(cwd).canonical_root_path) or cwd
            )
        row = row | {"cwd": projects[cwd]}
        key = prompt_identity(row)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        ordinals[row["sid"]] += 1
        if key in existing:
            skipped += 1
            continue
        sessions[row["sid"]].append(row | {"ordinal": ordinals[row["sid"]]})
    return dict(sessions), skipped


def write_session(sid: str, rows: list[dict], run_id: str, projects: dict) -> int:
    statements = [
        "BEGIN;",
        "SET LOCAL standard_conforming_strings=on;",
        "SET LOCAL lock_timeout='5s';",
    ]
    for cwd in dict.fromkeys(r["cwd"] for r in rows):
        if cwd not in projects:
            ctx = resolve_git_context(cwd)
            canonical = normalize_text(ctx.canonical_root_path) or cwd
            projects[cwd] = (canonical, ctx.git_remote, ctx.source_kind)
        canonical, remote, source = projects[cwd]
        statements.append(
            f"INSERT INTO mem_projects(name,full_path,canonical_root_path,git_remote,source_kind) VALUES ({literal(Path(canonical).name or canonical)},{literal(canonical)},{literal(canonical if source == 'git' else None)},{literal(remote)},{literal(source)}) ON CONFLICT(full_path) DO NOTHING;"
        )
    first_project = projects[rows[0]["cwd"]][0]
    statements.append(
        f"INSERT INTO mem_sessions(session_id,project_id,agent_type,started_at) SELECT {literal(sid)},id,'codex-cli',{literal(rows[0]['timestamp'])}::timestamptz FROM mem_projects WHERE full_path={literal(first_project)} ON CONFLICT(session_id) DO NOTHING;"
    )
    # Lock the existing session row while allocating sequential prompt numbers.
    statements.append(
        f"SELECT id FROM mem_sessions WHERE session_id={literal(sid)} FOR UPDATE;"
    )
    values = ",\n".join(
        f"({literal(r['text'])},{literal(r['hash'])},{literal(r['timestamp'])}::timestamptz,{r['ordinal']},{literal(projects[r['cwd']][0])})"
        for r in rows
    )
    statements.append(f"""WITH candidates(text,hash,ts,ordinal,project_path) AS (VALUES {values}),
    session AS (SELECT id FROM mem_sessions WHERE session_id={literal(sid)}),
    base AS (SELECT COALESCE(MAX(prompt_number),0) n FROM mem_user_prompts WHERE session_id=(SELECT id FROM session)),
    missing AS (SELECT c.* FROM candidates c WHERE NOT EXISTS (SELECT 1 FROM mem_user_prompts p WHERE p.session_id=(SELECT id FROM session) AND p.content_hash=c.hash AND p.created_at=c.ts AND p.project_id=(SELECT id FROM mem_projects WHERE full_path=c.project_path))),
    inserted AS (INSERT INTO mem_user_prompts(session_id,project_id,prompt_number,prompt_text,agent_name,turn_index,content_hash,retention_class,backfill_run_id,created_at)
    SELECT (SELECT id FROM session),p.id,(base.n+row_number() OVER(ORDER BY m.ts,m.ordinal))::integer,m.text,'codex-cli',m.ordinal,m.hash,'backfill_codex',{literal(run_id)},m.ts
    FROM missing m CROSS JOIN base JOIN mem_projects p ON p.full_path=m.project_path RETURNING id)
    SELECT count(*) FROM inserted;""")
    statements.append("COMMIT;")
    return int(psql("\n".join(statements)).splitlines()[-1])


def link_existing_tools() -> list[dict]:
    """Fill missing links only within the same native session and project.

    Existing links are preserved. Returned IDs are a rollback/audit manifest;
    no prompt text is written to logs.
    """
    sql = """BEGIN;
    SET LOCAL lock_timeout='5s';
    WITH candidates AS (
      SELECT t.id, p.id AS prompt_id
      FROM mem_tool_calls t
      CROSS JOIN LATERAL (
        SELECT p.id FROM mem_user_prompts p
        WHERE p.session_id=t.session_id AND p.project_id=t.project_id
          AND p.created_at <= t.created_at AND p.agent_name LIKE 'codex%'
        ORDER BY p.created_at DESC,p.id DESC LIMIT 1
      ) p
      WHERE t.source_system LIKE 'codex%' AND t.prev_user_prompt_id IS NULL
        AND EXISTS(SELECT 1 FROM mem_user_prompts b WHERE b.session_id=t.session_id AND b.retention_class='backfill_codex')
    ), updated AS (
      UPDATE mem_tool_calls t SET prev_user_prompt_id=c.prompt_id
      FROM candidates c WHERE t.id=c.id AND t.prev_user_prompt_id IS NULL
      RETURNING t.id,t.prev_user_prompt_id
    ) SELECT COALESCE(json_agg(updated),'[]'::json) FROM updated;
    COMMIT;"""
    return json.loads(psql(sql))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--link-tools",
        action="store_true",
        help="Link existing unlinked tool calls by native session, project and source time",
    )
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    run_id = "codex-prompts-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if args.report is None:
        args.report = ROOT / "logs/codex-backfill" / f"{run_id}.json"
    rows = []
    counts = Counter()
    files = sorted(
        set(args.codex_home.joinpath("sessions").rglob("*.jsonl"))
        | set(args.codex_home.joinpath("archived_sessions").rglob("*.jsonl"))
    )
    for file in files:
        parsed, stats = parse_transcript(file)
        rows.extend(parsed)
        counts.update(stats)
    sessions, skipped = plan(rows, existing_occurrences())
    summary = {
        "run_id": run_id,
        "commit": args.commit,
        "files": len(files),
        "human_prompts": len(rows),
        "excluded": dict(counts),
        "already_present_or_duplicate": skipped,
        "sessions_to_import": len(sessions),
        "prompts_to_import": sum(map(len, sessions.values())),
        "inserted": 0,
        "sessions_completed": 0,
        "projects": dict(
            Counter(r["cwd"] for entries in sessions.values() for r in entries)
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def report():
        args.report.write_text(json.dumps(summary, indent=2) + "\n")

    report()
    print(json.dumps(summary), flush=True)
    if args.commit:
        projects = {}
        for sid, entries in list(sessions.items())[: args.limit]:
            summary["inserted"] += write_session(sid, entries, run_id, projects)
            summary["sessions_completed"] += 1
            report()
            print(
                json.dumps(
                    {
                        "utc": datetime.now(timezone.utc).isoformat(),
                        "session": sid,
                        "completed": summary["sessions_completed"],
                        "inserted": summary["inserted"],
                    }
                ),
                flush=True,
            )
            time.sleep(0.65)
        if args.link_tools:
            summary["tool_links"] = link_existing_tools()
            summary["linked_tool_calls"] = len(summary["tool_links"])
    report()


if __name__ == "__main__":
    main()
