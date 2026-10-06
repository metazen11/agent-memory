"""Package only committed, tested source and a checksum manifest."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

sha = sys.argv[1]
output = Path(sys.argv[2])
output.mkdir(parents=True, exist_ok=True)
resolved = subprocess.check_output(["git", "rev-parse", sha], text=True).strip()
archive = subprocess.check_output(["git", "archive", "--format=tar", resolved])
(output / "source.tar").write_bytes(archive)
requirements = subprocess.check_output(["git", "show", f"{resolved}:requirements.txt"])
(output / "release.json").write_text(
    json.dumps(
        {
            "sha": resolved,
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
        },
        indent=2,
    )
    + "\n"
)
