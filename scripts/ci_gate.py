"""Required portable review gate: syntax plus lint on changed Python files."""

import os
import subprocess
from pathlib import Path

base = os.environ.get("BASE")
before = os.environ.get("BEFORE")
ref = f"origin/{base}" if base else before
if not ref or set(ref) == {"0"}:
    ref = "HEAD^"
changed = subprocess.check_output(
    ["git", "diff", "--name-only", ref, "HEAD"], text=True
).splitlines()
python = [p for p in changed if p.endswith(".py") and Path(p).is_file()]
if python:
    subprocess.run(["ruff", "check", *python], check=True)
subprocess.run(["python", "-m", "compileall", "-q", "app", "scripts"], check=True)
for directory in ["integrations/codex", "hooks", "scripts/lib"]:
    for file in Path(directory).glob("*.js"):
        subprocess.run(["node", "--check", str(file)], check=True)
