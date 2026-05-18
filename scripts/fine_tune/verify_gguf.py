#!/usr/bin/env python3
"""Cross-platform GGUF verification companion.

For collaborators (e.g. Mark) who receive a shipped GGUF and want to sanity
check it without the full training pipeline. Mac/Linux/Windows.

Subcommands:
    info  <path.gguf>         print metadata (arch, params, ctx, quant)
    smoke <path.gguf>         boot llama-server, send 3 chat-completions
                              prompts, verify tool_calls emit, shut down
    eval  <path.gguf>         run Class B + E from real_world/ against the
                              GGUF (requires the harness scripts in-tree)

No MPS/CUDA assumptions — boots llama-server on whatever the local
llama.cpp build supports.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).absolute().parents[2]
LLAMA_BIN_DIRS = [
    REPO_ROOT / "models" / "llama.cpp" / "build" / "bin",      # Mac/Linux
    REPO_ROOT / "models" / "llama.cpp" / "build" / "Release",  # Windows
]


# ---------------------------------------------------------------------------
# GGUF metadata reader — pure stdlib, GGUF v2/v3
# ---------------------------------------------------------------------------

# https://github.com/ggerganov/ggml/blob/master/docs/gguf.md
GGUF_MAGIC = b"GGUF"
GGUF_TYPES: dict[int, str] = {
    0: "uint8", 1: "int8", 2: "uint16", 3: "int16",
    4: "uint32", 5: "int32", 6: "float32", 7: "bool",
    8: "string", 9: "array", 10: "uint64", 11: "int64", 12: "float64",
}


def _read_u32(fp) -> int:
    return struct.unpack("<I", fp.read(4))[0]


def _read_u64(fp) -> int:
    return struct.unpack("<Q", fp.read(8))[0]


def _read_string(fp) -> str:
    n = _read_u64(fp)
    return fp.read(n).decode("utf-8", errors="replace")


_FIXED_TYPE_SIZES = {
    0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1,
    10: 8, 11: 8, 12: 8,
}


def _read_value(fp, t: int):
    if t == 0: return struct.unpack("<B", fp.read(1))[0]
    if t == 1: return struct.unpack("<b", fp.read(1))[0]
    if t == 2: return struct.unpack("<H", fp.read(2))[0]
    if t == 3: return struct.unpack("<h", fp.read(2))[0]
    if t == 4: return struct.unpack("<I", fp.read(4))[0]
    if t == 5: return struct.unpack("<i", fp.read(4))[0]
    if t == 6: return struct.unpack("<f", fp.read(4))[0]
    if t == 7: return bool(fp.read(1)[0])
    if t == 8: return _read_string(fp)
    if t == 9:
        sub = _read_u32(fp)
        n = _read_u64(fp)
        # Materialise short numeric arrays; skip huge token arrays.
        if sub in _FIXED_TYPE_SIZES and n <= 64:
            return [_read_value(fp, sub) for _ in range(n)]
        if sub == 8 and n <= 16:
            return [_read_string(fp) for _ in range(n)]
        # Skip the body but record a placeholder so the caller knows it existed.
        if sub in _FIXED_TYPE_SIZES:
            fp.read(_FIXED_TYPE_SIZES[sub] * n)
        elif sub == 8:
            for _ in range(n):
                slen = _read_u64(fp)
                fp.read(slen)
        else:
            raise ValueError(f"unsupported array sub-type {sub}")
        return f"<array[{GGUF_TYPES.get(sub, sub)} x {n}]>"
    if t == 10: return _read_u64(fp)
    if t == 11: return struct.unpack("<q", fp.read(8))[0]
    if t == 12: return struct.unpack("<d", fp.read(8))[0]
    raise ValueError(f"unknown gguf value type {t}")


def read_gguf_metadata(path: Path, *, max_kv: int = 200) -> dict:
    """Light-weight GGUF metadata read. We deliberately bail early on arrays
    we don't need so the function runs fast even on multi-GB files."""
    with path.open("rb") as fp:
        magic = fp.read(4)
        if magic != GGUF_MAGIC:
            raise ValueError(f"not a GGUF file (magic={magic!r})")
        version = _read_u32(fp)
        _ = _read_u64(fp)  # tensor count
        kv_count = _read_u64(fp)
        out: dict = {
            "_path": str(path),
            "_size_bytes": path.stat().st_size,
            "_gguf_version": version,
            "_kv_count": kv_count,
        }
        for _ in range(min(kv_count, max_kv)):
            try:
                key = _read_string(fp)
                t = _read_u32(fp)
                val = _read_value(fp, t)
                out[key] = val
                # We don't actually need to consume the rest after a string
                # array, since _read_value handles it.
            except (struct.error, UnicodeDecodeError, ValueError):
                break
        return out


def cmd_info(args: argparse.Namespace) -> int:
    p = Path(args.path)
    if not p.exists():
        print(f"FAIL: {p} not found", file=sys.stderr)
        return 1
    meta = read_gguf_metadata(p)
    print(f"path:        {p}")
    print(f"size:        {meta['_size_bytes'] / (1024**3):.2f} GB")
    print(f"gguf version: {meta['_gguf_version']}")
    print(f"kv count:    {meta['_kv_count']}")
    print("-- selected metadata --")
    arch = meta.get("general.architecture", "?")
    print(f"architecture:    {arch}")
    print(f"name:            {meta.get('general.name', '?')}")
    print(f"file_type code:  {meta.get('general.file_type', '?')}")
    # Find architecture-specific context / params
    if arch and arch != "?":
        ctx_key = f"{arch}.context_length"
        emb_key = f"{arch}.embedding_length"
        layers_key = f"{arch}.block_count"
        head_key = f"{arch}.attention.head_count"
        print(f"context_length:  {meta.get(ctx_key, '?')}")
        print(f"embedding_length:{meta.get(emb_key, '?')}")
        print(f"block_count:     {meta.get(layers_key, '?')}")
        print(f"head_count:      {meta.get(head_key, '?')}")
    tpl = meta.get("tokenizer.chat_template", "")
    print(f"chat_template:   {'present (' + str(len(tpl)) + ' chars)' if tpl else 'MISSING'}")
    # tool calling heuristic: chat template mentions <tool_call>
    if tpl:
        has_tools = "<tool_call>" in tpl or "tool_calls" in tpl
        print(f"tool_call markers in template: {'yes' if has_tools else 'no'}")
    return 0


# ---------------------------------------------------------------------------
# Server runner — used by smoke + eval
# ---------------------------------------------------------------------------


def find_llama_server() -> Path | None:
    for d in LLAMA_BIN_DIRS:
        for name in ("llama-server", "llama-server.exe"):
            cand = d / name
            if cand.exists():
                return cand
    p = shutil.which("llama-server")
    return Path(p) if p else None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    def __init__(self, gguf: Path, port: int, ctx: int = 8192):
        self.gguf = gguf
        self.port = port
        self.ctx = ctx
        self.proc: subprocess.Popen | None = None

    def __enter__(self):
        bin_ = find_llama_server()
        if bin_ is None:
            raise RuntimeError("llama-server not found; build llama.cpp first")
        log_path = REPO_ROOT / "logs" / f"verify-gguf-server-{int(time.time())}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(bin_),
            "-m", str(self.gguf),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-c", str(self.ctx),
            "--jinja",
        ]
        log_fp = log_path.open("w")
        self.proc = subprocess.Popen(cmd, stdout=log_fp, stderr=subprocess.STDOUT)
        # Wait for server to be ready (up to 60s)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    if r.status == 200:
                        return self
            except (urllib.error.URLError, ConnectionResetError):
                pass
            time.sleep(0.5)
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited early; see {log_path}")
        raise RuntimeError(f"llama-server never became ready on :{self.port}; see {log_path}")

    def __exit__(self, *_exc):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            import psutil  # noqa: PLC0415
            for c in psutil.net_connections():
                if c.laddr and c.laddr.port == self.port and c.status == "LISTEN":
                    try:
                        psutil.Process(c.pid).terminate()
                    except psutil.NoSuchProcess:
                        pass
        except ImportError:
            pass

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


SMOKE_PROMPTS = [
    "List the files in /tmp",
    "Read the first 20 lines of README.md",
    "Search for 'def main' in the current directory",
]

SMOKE_TOOLS = [
    {"type": "function", "function": {
        "name": "Bash",
        "description": "Execute a bash command and return stdout/stderr.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command to run"},
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "Read",
        "description": "Read the contents of a file.",
        "parameters": {"type": "object", "properties": {
            "file_path": {"type": "string"},
            "limit": {"type": "integer"},
        }, "required": ["file_path"]},
    }},
    {"type": "function", "function": {
        "name": "Grep",
        "description": "Search for a regex pattern in files.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        }, "required": ["pattern"]},
    }},
]


def chat_once(base_url: str, prompt: str, model: str = "local") -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant with access to tools."},
            {"role": "user", "content": prompt},
        ],
        "tools": SMOKE_TOOLS,
        "tool_choice": "auto",
        "temperature": 0.0,
        "max_tokens": 256,
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def cmd_smoke(args: argparse.Namespace) -> int:
    p = Path(args.path)
    if not p.exists():
        print(f"FAIL: {p} not found", file=sys.stderr)
        return 1
    port = args.port or free_port()
    passes = 0
    print(f"booting llama-server on :{port} ...")
    try:
        with LlamaServer(p, port) as srv:
            for i, prompt in enumerate(SMOKE_PROMPTS, 1):
                try:
                    resp = chat_once(srv.base_url(), prompt)
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                    print(f"  {i}. ERROR {prompt!r}: {e}")
                    continue
                choice = resp.get("choices", [{}])[0].get("message", {})
                tcs = choice.get("tool_calls") or []
                if tcs:
                    tc = tcs[0]
                    name = tc.get("function", {}).get("name", "?")
                    print(f"  {i}. OK   {prompt!r} -> tool_call name={name}")
                    passes += 1
                else:
                    txt = (choice.get("content") or "").strip()[:200]
                    print(f"  {i}. WARN {prompt!r} -> text only: {txt!r}")
    except RuntimeError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"\nsmoke result: {passes}/{len(SMOKE_PROMPTS)} emitted tool_calls")
    return 0 if passes >= 2 else 1


def cmd_eval(args: argparse.Namespace) -> int:
    p = Path(args.path)
    if not p.exists():
        print(f"FAIL: {p} not found", file=sys.stderr)
        return 1
    rw = REPO_ROOT / "tests" / "fine_tune" / "real_world"
    if not (rw / "harness_class_b.py").exists() or not (rw / "harness_class_e.py").exists():
        print("FAIL: real_world harness scripts missing; check repo layout", file=sys.stderr)
        return 1
    port = args.port or free_port()
    out_b = REPO_ROOT / "tests" / "fine_tune" / "real_world" / "baselines" / "verify-class-b.results.json"
    out_e = REPO_ROOT / "tests" / "fine_tune" / "real_world" / "baselines" / "verify-class-e.results.json"
    out_b.parent.mkdir(parents=True, exist_ok=True)
    rc = 0
    try:
        with LlamaServer(p, port):
            base = f"http://127.0.0.1:{port}/v1"
            for harness, out_path, label in (
                (rw / "harness_class_b.py", out_b, "Class B"),
                (rw / "harness_class_e.py", out_e, "Class E"),
            ):
                print(f"running {label} ...")
                cmd = [
                    sys.executable, str(harness),
                    base, "local", p.stem, "q?", str(out_path),
                ]
                r = subprocess.run(cmd)
                if r.returncode != 0:
                    rc = r.returncode
                    print(f"  {label} returned {r.returncode}")
                else:
                    print(f"  {label} -> {out_path}")
    except RuntimeError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return rc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="Print GGUF metadata")
    p_info.add_argument("path")
    p_info.set_defaults(func=cmd_info)

    p_smoke = sub.add_parser("smoke", help="Boot llama-server, send 3 prompts, verify tool_calls")
    p_smoke.add_argument("path")
    p_smoke.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    p_smoke.set_defaults(func=cmd_smoke)

    p_eval = sub.add_parser("eval", help="Run Class B + E harness against the GGUF")
    p_eval.add_argument("path")
    p_eval.add_argument("--port", type=int, default=0)
    p_eval.set_defaults(func=cmd_eval)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
