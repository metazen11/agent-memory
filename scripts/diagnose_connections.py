#!/usr/bin/env python3
"""Read-only connection diagnostics; never starts services or prints secrets."""

from __future__ import annotations

import argparse
import json
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def safe_endpoint(url: str) -> str:
    """Return only the non-sensitive shape of a diagnostic endpoint."""
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path if parsed.path == "/api/health" else "/[redacted]"
    return f"{parsed.scheme}://{parsed.hostname}:{port}{path}"


def http_probe(url: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        service_status = body.get("status") if isinstance(body, dict) else None
        if service_status not in {"ok", "degraded", "error"}:
            service_status = "unknown"
        return {"status": "ok", "http_status": response.status, "service_status": service_status}
    except urllib.error.HTTPError as error:
        return {"status": "error", "http_status": error.code, "error": "http_error"}
    except (OSError, ValueError) as error:
        return {"status": "error", "error": type(error).__name__}


def postgres_probe(host: str, port: int, timeout: float) -> dict:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"status": "ok", "host": host, "port": port}
    except OSError as error:
        return {"status": "error", "host": host, "port": port, "error": type(error).__name__}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", default="http://127.0.0.1:3377/api/health")
    parser.add_argument("--pg-host", default=os.getenv("POSTGRES_HOST", "localhost"))
    parser.add_argument("--pg-port", type=int, default=int(os.getenv("POSTGRES_PORT", "5432")))
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    parsed = urlsplit(args.http)
    default_port = 443 if parsed.scheme == "https" else 80
    print(json.dumps({
        "http": {"endpoint": safe_endpoint(args.http),
                 **http_probe(args.http, args.timeout)},
        "postgres_tcp": postgres_probe(args.pg_host, args.pg_port, args.timeout),
        "interpretation": "HTTP and PostgreSQL are independent transports; an HTTP refusal does not prove stdio MCP or PostgreSQL is down.",
    }, indent=2))


if __name__ == "__main__":
    main()
