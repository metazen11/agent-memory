#!/usr/bin/env python3
"""Render a structured fine-tune eval results.json into a uniform markdown
(and optionally HTML) report.

Usage
-----
    python scripts/fine_tune/render_test_report.py <results.json> \\
        --out report.md [--html report.html] [--template path/to/template.md]

Behavior
--------
1. Validate results.json against schemas/eval-report.schema.json
   (JSON Schema draft 2020-12) using the `jsonschema` library.
   Fail with a clear error and exit code 2 on validation failure.

2. Load the markdown template (default:
   docs/fine_tune/templates/TEST_REPORT_TEMPLATE.md). Placeholders use
   `{{double_curly}}` syntax. We substitute via plain string replacement
   — no Jinja, no template-side loops. Tables are pre-rendered by helpers
   in this script and dropped in as whole blocks. The script asserts no
   `{{...}}` placeholder remains in the output before writing.

3. If --html is passed, also emit HTML. Strategy:
     - prefer `pandoc` (via shutil.which) if available
     - else try the `markdown` Python library
     - else write a warning HTML wrapping the .md verbatim and print a
       notice to stderr; exit code is still 0
   Document this in the script docstring.

4. Output is deterministic: rerunning with the same input produces
   byte-identical output (modulo file mtimes).

Exit codes
----------
    0   success
    1   missing template / generic error
    2   schema validation failure
    3   missing input file

Dependencies
------------
    jsonschema >= 4.18 (for draft 2020-12 support)
    markdown (optional, only used as HTML fallback if pandoc is missing)

This is a dev/eval tool. It belongs in requirements-dev.txt, NOT the
runtime requirements.txt.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "error: `jsonschema` is required. Install via "
        "`pip install 'jsonschema>=4.18'`\n"
    )
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = REPO_ROOT / "schemas" / "eval-report.schema.json"
DEFAULT_TEMPLATE = (
    REPO_ROOT / "docs" / "fine_tune" / "templates" / "TEST_REPORT_TEMPLATE.md"
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(data: dict, schema_path: Path) -> None:
    """Validate `data` against the schema at `schema_path`.

    On failure, print every error path + message and exit 2.
    """
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        sys.stderr.write(
            f"error: results JSON failed schema validation against {schema_path}\n"
        )
        for err in errors:
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            sys.stderr.write(f"  at {loc}: {err.message}\n")
        sys.exit(2)


# ---------------------------------------------------------------------------
# Helpers: markdown table builders
# ---------------------------------------------------------------------------


def _md_escape(text: str) -> str:
    """Escape characters that would break a markdown table cell."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def _truncate(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "..."


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Build a GitHub-flavored markdown table.

    Empty `rows` returns a table with a single "no data" row so the
    section still renders cleanly.
    """
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "|" + "|".join(["---"] * len(headers)) + "|"
    if not rows:
        empty = ["_(none)_"] + [""] * (len(headers) - 1)
        body = "| " + " | ".join(empty) + " |"
    else:
        body = "\n".join(
            "| " + " | ".join(_md_escape(c) for c in row) + " |" for row in rows
        )
    return f"{header_line}\n{sep_line}\n{body}"


def render_verdict_banner(verdict: dict) -> str:
    status = "PASS [OK]" if verdict["pass"] else "FAIL [X]"
    headline = verdict["headline"].strip()
    recommendation = verdict["recommendation"].strip()
    return (
        f"> **Verdict: {status}** — {headline}\n"
        f">\n"
        f"> **Recommendation:** {recommendation}"
    )


def render_model_harness_table(model: dict, harness: dict) -> str:
    rows = [
        ["Model ID", model.get("id", "")],
        ["Path", model.get("path", "")],
        ["Quant", model.get("quant", "")],
        ["Params", model.get("params", "")],
    ]
    if model.get("sha256"):
        rows.append(["SHA-256", model["sha256"]])
    rows += [
        ["Harness", f"{harness.get('name', '')} @ {harness.get('version', '')}"],
        ["Endpoint", harness.get("endpoint", "")],
        ["Server", harness.get("server", "")],
    ]
    optional_harness = [
        ("temperature", "Temperature"),
        ("max_tokens", "Max tokens"),
        ("max_turns", "Max turns"),
    ]
    for key, label in optional_harness:
        if key in harness:
            rows.append([label, str(harness[key])])
    return _md_table(["Field", "Value"], rows)


def _format_gate_value(value: Any, units: str) -> str:
    """Format a gate threshold/actual with units, only attaching units to a
    bare numeric value. String values (e.g. '>= 50', '<= 30') are returned
    as-is — the caller already encoded the comparison."""
    if not units:
        return str(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        sep = "" if units.startswith("%") else " "
        return f"{value}{sep}{units}"
    return str(value)


def render_gates_table(gates: list[dict]) -> str:
    rows = []
    for g in gates:
        units = g.get("units", "")
        threshold = _format_gate_value(g["threshold"], units)
        actual = _format_gate_value(g["actual"], units)
        pass_cell = "PASS" if g["pass"] else "FAIL"
        notes = g.get("notes", "")
        rows.append([g["name"], threshold, actual, pass_cell, notes])
    return _md_table(["Gate", "Threshold", "Actual", "Result", "Notes"], rows)


def _try_float(v: Any) -> float | None:
    """Try to coerce 'X/Y', 'XX%', or a plain number into a float for delta computation.

    Returns None if not coercible.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip()
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*%$", s)
    if m:
        return float(m.group(1))
    m = re.match(r"^(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)$", s)
    if m:
        denom = float(m.group(2))
        if denom == 0:
            return None
        return float(m.group(1)) / denom * 100.0  # render as percentage points
    try:
        return float(s)
    except ValueError:
        return None


def render_baseline_section(this_stats: dict, baselines: list[dict]) -> str:
    if not baselines:
        return "_No baselines linked._"

    # Union of metric keys, preserving insertion order from this_stats first.
    keys: list[str] = []
    for k in this_stats.keys():
        if k not in keys:
            keys.append(k)
    for b in baselines:
        for k in b.get("summary", {}).keys():
            if k not in keys:
                keys.append(k)

    headers = ["Metric", "This run"]
    for b in baselines:
        headers.append(b["model_id"])
        headers.append(f"Δ vs {b['model_id']}")

    rows: list[list[str]] = []
    for k in keys:
        this_v = this_stats.get(k, "")
        this_f = _try_float(this_v)
        row = [k, str(this_v) if this_v != "" else "_(n/a)_"]
        for b in baselines:
            base_v = b.get("summary", {}).get(k, "")
            base_f = _try_float(base_v)
            row.append(str(base_v) if base_v != "" else "_(n/a)_")
            if this_f is not None and base_f is not None:
                delta = this_f - base_f
                sign = "+" if delta >= 0 else ""
                row.append(f"{sign}{delta:.1f}pp")
            else:
                row.append("—")
        rows.append(row)

    # Add baseline source URLs as a list under the table.
    lines = [_md_table(headers, rows), ""]
    for b in baselines:
        if b.get("results_url"):
            lines.append(f"- `{b['model_id']}` results: {b['results_url']}")
    return "\n".join(lines).rstrip() + "\n"


def render_prompts_table(prompts: list[dict]) -> str:
    rows = []
    for p in prompts:
        regressions = ", ".join(p.get("regressions", [])) or "—"
        rows.append([
            str(p["id"]),
            _truncate(p["text"], 70),
            p["outcome"],
            str(p["n_turns"]),
            regressions,
        ])
    return _md_table(
        ["#", "Prompt (truncated)", "Outcome", "Turns", "Regressions"], rows
    )


def render_aggregate_stats_table(stats: dict) -> str:
    rows = [[k, str(v)] for k, v in stats.items()]
    return _md_table(["Metric", "Value"], rows)


def render_artifacts_list(artifacts: list[dict]) -> str:
    if not artifacts:
        return "_No artifacts recorded._"
    lines = []
    for a in artifacts:
        size = a.get("size_bytes")
        size_str = f" ({size:,} bytes)" if isinstance(size, int) else ""
        lines.append(f"- **{a['name']}** — `{a['path']}`{size_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def build_substitutions(data: dict) -> dict[str, str]:
    return {
        "report_id": data["report_id"],
        "run_date": data["run_date"],
        "eval_class": data["eval_class"],
        "verdict_banner": render_verdict_banner(data["verdict"]),
        "model_harness_table": render_model_harness_table(
            data["model"], data["harness"]
        ),
        "gates_table": render_gates_table(data["gates"]),
        "baseline_section": render_baseline_section(
            data.get("aggregate_stats", {}), data.get("baselines", [])
        ),
        "prompts_table": render_prompts_table(data["prompts"]),
        "aggregate_stats_table": render_aggregate_stats_table(
            data.get("aggregate_stats", {})
        ),
        "notable_findings": data.get("notable_findings", "").strip()
            or "_(no findings recorded)_",
        "artifacts_list": render_artifacts_list(data.get("artifacts", [])),
    }


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def substitute(template: str, subs: dict[str, str]) -> str:
    """Substitute {{placeholder}} occurrences. HTML comments are passed through
    untouched, so the template can document its own placeholder syntax inside
    <!-- ... --> without those literal examples being treated as real
    placeholders."""

    # Carve the template into (comment, non-comment) segments. Substitution
    # only applies to non-comment segments.
    out: list[str] = []
    last = 0
    for m in _HTML_COMMENT_RE.finditer(template):
        # Non-comment chunk before this comment: substitute.
        chunk = template[last:m.start()]
        out.append(_apply_subs(chunk, subs))
        # Comment itself: pass through verbatim.
        out.append(m.group(0))
        last = m.end()
    out.append(_apply_subs(template[last:], subs))

    rendered = "".join(out)
    # Verify no placeholder remains outside comments.
    for segment in _HTML_COMMENT_RE.split(rendered):
        leftover = _PLACEHOLDER_RE.search(segment)
        if leftover:
            raise RuntimeError(
                f"unsubstituted placeholder remains after rendering: {leftover.group(0)}"
            )
    return rendered


def _apply_subs(text: str, subs: dict[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in subs:
            raise KeyError(f"unknown placeholder in template: {{{{{key}}}}}")
        return subs[key]

    return _PLACEHOLDER_RE.sub(_replace, text)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def render_html(md_path: Path, html_path: Path) -> str:
    """Render markdown to HTML. Returns a short status string for logging."""
    if shutil.which("pandoc"):
        subprocess.run(
            [
                "pandoc",
                "--from=gfm",
                "--to=html5",
                "--standalone",
                "--metadata", f"title={md_path.stem}",
                "-o", str(html_path),
                str(md_path),
            ],
            check=True,
        )
        return f"pandoc -> {html_path}"

    try:
        import markdown as md_lib  # type: ignore
    except ImportError:
        # Fallback: wrap the raw markdown in a <pre> so something useful renders.
        html_path.write_text(
            "<!doctype html><html><body>"
            "<p><em>pandoc not available and `markdown` library not installed; "
            "this file contains the raw markdown verbatim.</em></p>"
            f"<pre>{md_path.read_text()}</pre>"
            "</body></html>"
        )
        sys.stderr.write(
            "warning: neither pandoc nor the `markdown` Python library is "
            "installed; wrote raw markdown wrapped in <pre>.\n"
        )
        return f"raw-fallback -> {html_path}"

    html_body = md_lib.markdown(
        md_path.read_text(),
        extensions=["tables", "fenced_code"],
    )
    html_path.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{md_path.stem}</title></head><body>{html_body}</body></html>"
    )
    return f"markdown-lib -> {html_path}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results_json", type=Path, help="Path to a results.json conforming to schemas/eval-report.schema.json")
    ap.add_argument("--out", type=Path, required=True, help="Destination markdown file")
    ap.add_argument("--html", type=Path, help="Optional HTML destination (pandoc preferred, markdown lib fallback)")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help=f"Template path (default: {DEFAULT_TEMPLATE.relative_to(REPO_ROOT)})")
    ap.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA, help=f"Schema path (default: {DEFAULT_SCHEMA.relative_to(REPO_ROOT)})")
    args = ap.parse_args()

    if not args.results_json.exists():
        sys.stderr.write(f"error: results file not found: {args.results_json}\n")
        return 3
    if not args.template.exists():
        sys.stderr.write(f"error: template not found: {args.template}\n")
        return 1
    if not args.schema.exists():
        sys.stderr.write(f"error: schema not found: {args.schema}\n")
        return 1

    data = json.loads(args.results_json.read_text())
    validate(data, args.schema)

    template = args.template.read_text()
    subs = build_substitutions(data)
    rendered = substitute(template, subs)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered)
    sys.stdout.write(f"wrote {args.out}\n")

    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        status = render_html(args.out, args.html)
        sys.stdout.write(f"wrote {args.html} ({status})\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
