"""Wait for a candidate API's database and embedding readiness."""

import argparse
import json
import time
import urllib.request

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://127.0.0.1:3377/api/health")
p.add_argument("--timeout", type=float, default=120)
p.add_argument("--sha")
a = p.parse_args()
deadline = time.monotonic() + a.timeout
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(a.url, timeout=5) as response:
            health = json.load(response)
        if (
            health.get("db", {}).get("status") == "ok"
            and health.get("embeddings", {}).get("status") == "ok"
            and (not a.sha or health.get("release_sha") == a.sha)
        ):
            print("API database and embeddings ready")
            break
    except Exception:
        pass
    time.sleep(2)
else:
    raise SystemExit("API readiness deadline exceeded")
