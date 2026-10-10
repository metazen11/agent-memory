"""Isolated, bounded inference fallback with strict output validation."""

import asyncio
import json
import logging
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.redact import redact_text

ROOT = Path(__file__).absolute().parent.parent
_gate = asyncio.Semaphore(1)
_status = {"status": "not_used", "last_success": None, "provider": None}


logger = logging.getLogger(__name__)


class EnrichmentUnavailable(RuntimeError):
    pass


class EnrichmentOutage(EnrichmentUnavailable):
    """The bridge itself failed (engine crash, timeout), not one item's output.

    Callers should pause before retrying. ``charge`` is set for timeouts: an
    oversized item times out on its own, so it must still use up its retries.
    """

    def __init__(self, message: str, *, charge: bool = False):
        super().__init__(message)
        self.charge = charge


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


_MAX_OUTPUT = 65536


def _request_line(system: str, user: str) -> str:
    return json.dumps(
        {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": redact_text(user)},
            ]
        }
    )


def _bridge_command(*, serve: bool = False) -> list[str]:
    command = [
        str(Path(settings.anvil_root) / ".venv/bin/python"),
        str(ROOT / "scripts/anvil_enrich.py"),
    ]
    return command + ["--serve"] if serve else command


def _bridge_env(env_overrides: dict[str, str] | None) -> dict[str, str]:
    return {
        **os.environ,
        "AGENT_MEMORY_ANVIL_ROOT": settings.anvil_root,
        **(env_overrides or {}),
    }


def _parse_envelope(output: bytes) -> tuple[str, str]:
    if len(output) > _MAX_OUTPUT:
        raise ValueError("Anvil output exceeds limit")
    envelope = json.loads(output)
    if not isinstance(envelope, dict):
        raise ValueError("Invalid Anvil envelope")
    if "error" in envelope:
        raise ValueError(f"Anvil bridge error: {envelope['error']}")
    if not isinstance(envelope.get("content"), str):
        raise ValueError("Invalid Anvil envelope")
    provider = envelope.get("provider")
    if not isinstance(provider, str) or not provider.startswith("anvil:"):
        raise ValueError("Missing provider identity")
    return envelope["content"], provider


def _log_stderr_tail(stderr, returncode) -> None:
    stderr.seek(max(0, stderr.seek(0, os.SEEK_END) - 2048))
    tail = stderr.read().decode(errors="replace").strip()
    logger.error("Anvil bridge exited %s: %s", returncode, redact_text(tail))


def _kill(process) -> None:
    if process is not None and process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def _bridge(
    system: str,
    user: str,
    *,
    env_overrides: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[str, str]:
    """Run one tools-free completion through scripts/anvil_enrich.py.

    Returns ``(content, provider)``. Raises EnrichmentOutage when the bridge
    process fails (its stderr tail is logged) and ValueError for bad output.
    ``env_overrides`` lets a caller pin ANVIL_MODEL_* for its own use without
    touching Anvil's global configuration.
    """
    async with _gate:
        process = None
        # A file, not a pipe: an unread stderr pipe can fill and wedge the child.
        with tempfile.TemporaryFile() as stderr:
            try:
                process = await asyncio.create_subprocess_exec(
                    *_bridge_command(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr,
                    start_new_session=True,
                    env=_bridge_env(env_overrides),
                )
                output = await asyncio.wait_for(
                    exchange(process, _request_line(system, user).encode()),
                    timeout or settings.anvil_timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                raise EnrichmentOutage("Anvil bridge timed out", charge=True) from error
            except OSError as error:
                raise EnrichmentOutage("Anvil bridge did not start") from error
            finally:
                if process is not None and process.returncode is None:
                    _kill(process)
                    await process.wait()
            if process.returncode:
                _log_stderr_tail(stderr, process.returncode)
                raise EnrichmentOutage(f"Anvil bridge exited {process.returncode}")
        return _parse_envelope(output)


_reapers: set[asyncio.Task] = set()


class WarmBridge:
    """One long-lived ``anvil_enrich.py --serve`` process for a pinned model.

    The one-shot bridge reloads the model on every call, and that load is
    most of a cold call's ~10s. This keeps one process (and its loaded
    model) per distinct ``env_overrides``, and kills it after
    ``idle_seconds`` without a request so the model's memory is returned.
    The process also exits by itself when its stdin closes, so it cannot
    outlive this one.

    Anvil's own LLM daemon (:3399) is NOT used: it is keyed by backend only
    and serves whatever model Anvil's global config loaded, which would
    defeat the pinned model.

    Any failure (timeout, EOF, malformed line) kills the process. The next
    request starts a fresh one, so a wedged model never stays cached.
    Failures map like the one-shot bridge: process death or spawn failure is
    an EnrichmentOutage, a timeout a charged one, an error line a ValueError.
    """

    def __init__(self, env_overrides: dict[str, str], idle_seconds: float):
        self.env_overrides = dict(env_overrides)
        self.idle_seconds = idle_seconds
        self.process = None
        self._stderr = None
        self._lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None

    @property
    def pid(self) -> int | None:
        return None if self.process is None else self.process.pid

    async def _ensure_process(self):
        if self.process is not None and self.process.returncode is None:
            return self.process
        self._stderr = tempfile.TemporaryFile()
        self.process = await asyncio.create_subprocess_exec(
            *_bridge_command(serve=True),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr,
            start_new_session=True,
            env=_bridge_env(self.env_overrides),
            limit=_MAX_OUTPUT * 2,
        )
        return self.process

    def close(self) -> None:
        """Kill the process (idle timeout, failure, shutdown) and reap it."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        process, self.process = self.process, None
        stderr, self._stderr = self._stderr, None
        if stderr is not None:
            stderr.close()
        if process is None:
            return
        _kill(process)
        try:
            task = asyncio.get_running_loop().create_task(process.wait())
        except RuntimeError:  # no running loop: nothing left to reap with
            return
        _reapers.add(task)
        task.add_done_callback(_reapers.discard)

    def _arm_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = asyncio.get_running_loop().call_later(
            self.idle_seconds, self.close
        )

    async def request(self, system: str, user: str, timeout: float) -> tuple[str, str]:
        async with self._lock:
            if self._idle_handle is not None:
                self._idle_handle.cancel()
                self._idle_handle = None
            try:
                try:
                    process = await self._ensure_process()
                    process.stdin.write((_request_line(system, user) + "\n").encode())
                    await process.stdin.drain()
                    line = await asyncio.wait_for(process.stdout.readline(), timeout)
                except asyncio.TimeoutError as error:
                    raise EnrichmentOutage("Anvil bridge timed out", charge=True) from error
                except OSError as error:  # spawn failure or broken stdin pipe
                    raise EnrichmentOutage("Anvil bridge did not start") from error
                if not line:
                    returncode = await process.wait()
                    _log_stderr_tail(self._stderr, returncode)
                    raise EnrichmentOutage(f"Anvil bridge exited {returncode}")
                result = _parse_envelope(line)
            except BaseException:
                self.close()
                raise
            self._arm_idle_timer()
            return result


_warm_bridges: dict[tuple, WarmBridge] = {}


def warm_bridge(env_overrides: dict[str, str], idle_seconds: float) -> WarmBridge:
    key = tuple(sorted(env_overrides.items()))
    bridge = _warm_bridges.get(key)
    if bridge is None:
        bridge = _warm_bridges[key] = WarmBridge(env_overrides, idle_seconds)
    bridge.idle_seconds = idle_seconds
    return bridge


async def close_warm_bridges() -> None:
    """Kill every warm bridge and wait until each process is reaped."""
    for bridge in _warm_bridges.values():
        bridge.close()
    _warm_bridges.clear()
    if _reapers:
        await asyncio.gather(*list(_reapers), return_exceptions=True)


_BRIDGE_ERRORS = (OSError, ValueError, KeyError, ValidationError, asyncio.TimeoutError)


def _record_failure(error: BaseException) -> None:
    timed_out = isinstance(error, asyncio.TimeoutError) or isinstance(
        error.__cause__, asyncio.TimeoutError
    )
    _status["status"] = "timeout" if timed_out else "failed"


async def _complete(
    system: str,
    user: str,
    env_overrides: dict[str, str] | None,
    timeout: float | None,
    keep_warm_seconds: float,
) -> tuple[str, str]:
    if keep_warm_seconds > 0:
        return await warm_bridge(env_overrides or {}, keep_warm_seconds).request(
            system, user, timeout or settings.anvil_timeout_seconds
        )
    return await _bridge(system, user, env_overrides=env_overrides, timeout=timeout)


async def generate_text(
    system: str,
    user: str,
    *,
    env_overrides: dict[str, str] | None = None,
    timeout: float | None = None,
    keep_warm_seconds: float = 0,
) -> tuple[str, str]:
    """Plain-text completion: returns ``(content, provider)`` unparsed.

    ``keep_warm_seconds > 0`` routes through a :class:`WarmBridge` that keeps
    the model loaded between calls for that long; 0 uses the one-shot bridge.

    For callers whose output is prose, not an object. Asking the local model
    for JSON around free text (commands with quoted flags) produced invalid
    JSON with unescaped quotes, so text callers must not round-trip JSON.
    """
    try:
        content, provider = await _complete(
            system, user, env_overrides, timeout, keep_warm_seconds
        )
    except EnrichmentOutage as error:
        _record_failure(error)
        raise
    except _BRIDGE_ERRORS as error:
        _record_failure(error)
        raise EnrichmentUnavailable("Anvil enrichment unavailable") from error
    _status.update(status="ok", last_success=time.time(), provider=provider)
    return content, provider


async def generate_json(system: str, user: str, kind: str) -> dict:
    try:
        content, provider = await _complete(
            system, user, None, None, settings.anvil_keep_warm_seconds
        )
        text = content.strip()
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
    except EnrichmentOutage as error:
        _record_failure(error)
        raise
    except _BRIDGE_ERRORS as error:
        _record_failure(error)
        raise EnrichmentUnavailable("Anvil enrichment unavailable") from error
    data["_provider"] = provider
    _status.update(status="ok", last_success=time.time(), provider=provider)
    return data
