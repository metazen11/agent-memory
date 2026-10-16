#!/usr/bin/env python3
"""Safely compact a Markdown memory index without deleting its source.

The compacted file is an index only: headings and one short summary line per
section remain, while the complete original is copied to an adjacent archive
directory before ``--apply --coordinated`` writes anything. Without that
explicit opt-in, the CLI only emits a candidate and never replaces the active
index.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import tempfile
from contextlib import contextmanager
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
            pending_summary = " ".join(stripped[2:].split()) if stripped.startswith("> ") else " ".join(stripped.split())
            out.append(f"> {pending_summary[:240]}")
        if sum(len(item.encode("utf-8")) + 1 for item in out) >= max_chars:
            break

    compacted = "\n".join(out).rstrip() + "\n"
    return _fit_utf8(compacted, max_chars)


@contextmanager
def _source_lock(path: Path):
    """Coordinate compaction with writers using the same adjacent lock."""
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def archive_and_compact(path: Path, max_chars: int = 4000) -> Path:
    """Archive then atomically replace ``path`` under the writer lock."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    with _source_lock(path):
        archive_dir = path.parent / f"{path.name}.archive"
        archive_dir.mkdir(exist_ok=True)
        original = path.read_bytes()
        source_mode = path.stat().st_mode & 0o7777
        for suffix in range(100):
            candidate = archive_dir / f"{stamp}-{suffix:02d}-{path.name}"
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(candidate, source_mode)
                archive = candidate
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(f"could not allocate unique archive name under {archive_dir}")
        if path.read_bytes() != original:
            raise RuntimeError(f"source changed while archiving: {path}")
        compacted = compact_markdown(original.decode("utf-8"), max_chars).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), source_mode)
                handle.write(compacted)
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != original:
                raise RuntimeError(f"source changed before publication: {path}")
            os.replace(temporary, path)
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-chars", type=int, default=4000)
    parser.add_argument("--apply", action="store_true", help="replace only with explicit --coordinated")
    parser.add_argument("--coordinated", action="store_true", help="confirm all writers honor the adjacent lock and the index is quiescent")
    args = parser.parse_args()
    if args.max_chars < 256:
        parser.error("--max-chars must be at least 256")
    if args.apply and not args.coordinated:
        parser.error("--apply requires --coordinated; default candidate output is safer")

    source = args.path.read_text(encoding="utf-8")
    compacted = compact_markdown(source, args.max_chars)
    if not args.apply:
        print(compacted, end="")
        return
    archive = archive_and_compact(args.path, args.max_chars)
    print(f"archived original to {archive}")


if __name__ == "__main__":
    main()
