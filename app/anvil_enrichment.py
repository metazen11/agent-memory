"""Isolated, bounded inference fallback with strict output validation."""

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.redact import redact_text

ROOT = Path(__file__).absolute().parent.parent
_gate = asyncio.Semaphore(1)
_status = {"status": "not_used", "last_success": None, "provider": None}


class EnrichmentUnavailable(RuntimeError):
    pass


class Observation(BaseModel):
    skip: bool = False
    title: str = Field(min_length=3, max_length=250)
    narrative: str = Field(min_length=10)
    type: str = "discovery"
    subtitle: str | None = None
    facts: list[str] = []
    concepts: list[str] = []
    files_read: list[str] = []
    files_modified: list[str] = []


class Lesson(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    rule: str = Field(min_length=20)
    severity: Literal["critical", "warning", "info"] = "warning"


def snapshot():
    return {"enabled": settings.anvil_fallback_enabled, **_status}


async def exchange(process, request: bytes) -> bytes:
    process.stdin.write(request)
    await process.stdin.drain()
    process.stdin.close()
    output = bytearray()
    while True:
        chunk = await process.stdout.read(min(4096, 65537 - len(output)))
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > 65536:
            raise ValueError("Anvil output exceeds limit")
    await process.wait()
    return bytes(output)


async def generate_json(system: str, user: str, kind: str) -> dict:
    request = json.dumps(
        {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": redact_text(user)},
            ]
        }
    )
    command = [
        str(Path(settings.anvil_root) / ".venv/bin/python"),
        str(ROOT / "scripts/anvil_enrich.py"),
    ]
    async with _gate:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "AGENT_MEMORY_ANVIL_ROOT": settings.anvil_root},
            )
            output = await asyncio.wait_for(
                exchange(process, request.encode()), settings.anvil_timeout_seconds
            )
            if process.returncode or len(output) > 65536:
                raise ValueError("Anvil command failed")
            envelope = json.loads(output)
            if not isinstance(envelope, dict) or not isinstance(
                envelope.get("content"), str
            ):
                raise ValueError("Invalid Anvil envelope")
            text = envelope["content"].strip()
            if text.startswith("```"):
                if "\n" not in text:
                    raise ValueError("Invalid JSON fence")
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            if data.get("skip") is not True:
                if kind == "observation":
                    data = Observation.model_validate(data).model_dump()
                elif kind == "lesson":
                    data = Lesson.model_validate(data).model_dump()
                else:
                    raise ValueError("Unknown enrichment kind")
            provider = envelope.get("provider")
            if not isinstance(provider, str) or not provider.startswith("anvil:"):
                raise ValueError("Missing provider identity")
            data["_provider"] = provider
            _status.update(status="ok", last_success=time.time(), provider=provider)
            return data
        except (
            OSError,
            ValueError,
            KeyError,
            ValidationError,
            asyncio.TimeoutError,
        ) as error:
            _status["status"] = (
                "timeout" if isinstance(error, asyncio.TimeoutError) else "failed"
            )
            raise EnrichmentUnavailable("Anvil enrichment unavailable") from error
        finally:
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
