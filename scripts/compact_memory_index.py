#!/usr/bin/env python3
"""Safely compact a Markdown memory index without deleting its source.

The compacted file is an index only: headings and one short summary line per
section remain, while the complete original is copied to an adjacent archive
directory before ``--apply`` writes anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path


def _fit_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    safe = encoded[:max_bytes].decode("utf-8", "ignore")
    if "\n" in safe:
        safe = safe.rsplit("\n", 1)[0]
    safe = safe.rstrip()
    return safe + ("\n" if len((safe + "\n").encode("utf-8")) <= max_bytes else "")


def compact_markdown(text: str, max_chars: int = 4000) -> str:
    """Return a UTF-8-byte-bounded index while retaining headings."""
    lines = text.splitlines()
    out: list[str] = []
    pending_summary: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            out.append(line)
            pending_summary = None
        elif stripped and pending_summary is None:
            pending_summary = " ".join(stripped.split())
            out.append(f"> {pending_summary[:240]}")
        if sum(len(item.encode("utf-8")) + 1 for item in out) >= max_chars:
            break

    compacted = "\n".join(out).rstrip() + "\n"
    return _fit_utf8(compacted, max_chars)


def archive_and_compact(path: Path, max_chars: int = 4000) -> Path:
    """Archive ``path`` then replace it; return the archive path."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive_dir = path.parent / f"{path.name}.archive"
    archive_dir.mkdir(exist_ok=True)
    original = path.read_bytes()
    for suffix in range(100):
        candidate = archive_dir / f"{stamp}-{suffix:02d}-{path.name}"
        try:
            # Start owner-only even if interrupted before source-mode
            # preservation. ``open('xb')`` can expose 0644 briefly under a
            # permissive umask.
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(original)
            os.chmod(candidate, path.stat().st_mode & 0o7777)
            archive = candidate
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"could not allocate unique archive name under {archive_dir}")
    current = path.read_bytes()
    if current != original:
        raise RuntimeError(f"source changed while archiving: {path}")
    compacted = compact_markdown(original.decode("utf-8"), max_chars)
    path.write_text(compacted, encoding="utf-8")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-chars", type=int, default=4000)
    parser.add_argument("--apply", action="store_true", help="archive and replace the file")
    args = parser.parse_args()
    if args.max_chars < 256:
        parser.error("--max-chars must be at least 256")

    source = args.path.read_text(encoding="utf-8")
    compacted = compact_markdown(source, args.max_chars)
    if not args.apply:
        print(compacted, end="")
        return
    archive = archive_and_compact(args.path, args.max_chars)
    print(f"archived original to {archive}")


if __name__ == "__main__":
    main()
