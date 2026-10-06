"""Pull a successful main CI artifact into an isolated release, with rollback.

No public PR code executes on the host. This controller only accepts successful
push runs for the current main SHA from this repository's ci-quality workflow.
"""

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

REPO = "metazen11/agent-memory"
ROOT = Path(__file__).absolute().parents[1]
STORE = Path.home() / ".local/share/agent-memory"


def run(*args, **kwargs):
    return subprocess.check_output(list(args), text=True, timeout=120, **kwargs).strip()


def gh_json(*args):
    return json.loads(run("gh", *args))


def eligible_run(runs, sha):
    return next(
        (
            r
            for r in runs
            if r["headSha"] == sha
            and r["headBranch"] == "main"
            and r["event"] == "push"
            and r["conclusion"] == "success"
        ),
        None,
    )


def unpack(artifact, destination, sha, requirements_hash):
    manifest = json.loads((artifact / "release.json").read_text())
    archive = artifact / "source.tar"
    if manifest["sha"] != sha or manifest["requirements_sha256"] != requirements_hash:
        raise ValueError("Release identity or prepared runtime does not match")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Release checksum does not match")
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                raise ValueError("Unsafe release path")
            if not (member.isfile() or member.isdir()):
                raise ValueError("Release links and special files are forbidden")
        bundle.extractall(destination, filter="data")
    (destination / "release.json").write_text(json.dumps(manifest))


def activate(path, python, *, verify_identity=True):
    subprocess.run(
        [str(python), str(path / "scripts/install_recovery_service.py"), "install"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    manifest = path / "release.json"
    identity = (
        ["--sha", json.loads(manifest.read_text())["sha"]]
        if manifest.exists() and verify_identity
        else []
    )
    subprocess.run(
        [
            str(python),
            str(Path(__file__).absolute().parent / "wait_for_health.py"),
            "--timeout",
            "180",
            *identity,
        ],
        check=True,
    )


def deploy(artifact, sha, state, store=STORE):
    release = store / "releases" / f"{sha}-{time.time_ns()}"
    previous = Path(state["active"])
    python = Path(state["python"])
    unpack(artifact, release, sha, state["requirements_sha256"])
    baseline = Path(state["active"]) / "scripts/migrations"
    candidate = release / "scripts/migrations"
    old = {p.name: p.read_bytes() for p in baseline.glob("*.sql")}
    new = {p.name: p.read_bytes() for p in candidate.glob("*.sql")}
    if old != new:
        raise ValueError("Migration changes require explicit compatibility preparation")
    (release / ".venv").symlink_to(python.parent.parent, target_is_directory=True)
    (release / ".env").symlink_to(store / "runtime.env")
    try:
        activate(release, python)
    except Exception:
        activate(previous, python, verify_identity=False)
        raise
    return {**state, "active": str(release), "previous": str(previous), "sha": sha}


def save(state, store=STORE):
    temporary = store / "deployment.tmp"
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(store / "deployment.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    STORE.mkdir(parents=True, exist_ok=True)
    if args.prepare:
        # Explicit preparation pins the runtime and private configuration.
        if (STORE / "deployment.json").exists():
            raise SystemExit("Already prepared; update the runtime contract explicitly")
        (STORE / "runtime.env").write_bytes((ROOT / ".env").read_bytes())
        (STORE / "runtime.env").chmod(0o600)
        sha = run("git", "rev-parse", "HEAD", cwd=ROOT)
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            subprocess.run(
                [
                    str(ROOT / ".venv/bin/python"),
                    str(ROOT / "scripts/build_release.py"),
                    sha,
                    directory,
                ],
                check=True,
                cwd=ROOT,
            )
            manifest = json.loads((artifact / "release.json").read_text())
            baseline = STORE / "releases" / f"bootstrap-{sha}"
            unpack(artifact, baseline, sha, manifest["requirements_sha256"])
            (baseline / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
            (baseline / ".env").symlink_to(STORE / "runtime.env")
        save(
            {
                "active": str(baseline),
                "python": str(ROOT / ".venv/bin/python"),
                "requirements_sha256": hashlib.sha256(
                    (ROOT / "requirements.txt").read_bytes()
                ).hexdigest(),
                "sha": run("git", "rev-parse", "HEAD", cwd=ROOT),
            }
        )
        return
    import fcntl

    with (STORE / "deploy.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        state = json.loads((STORE / "deployment.json").read_text())
        sha = gh_json("api", f"repos/{REPO}/commits/main")["sha"]
        if state.get("sha") == sha:
            return
        runs = gh_json(
            "run",
            "list",
            "--repo",
            REPO,
            "--workflow",
            "ci-quality.yml",
            "--branch",
            "main",
            "--limit",
            "30",
            "--json",
            "databaseId,headSha,headBranch,event,conclusion",
        )
        candidate = eligible_run(runs, sha)
        if not candidate:
            print("Waiting for successful main CI", sha)
            return
        # Recheck the moving branch before stopping any production process.
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory)
            run(
                "gh",
                "run",
                "download",
                str(candidate["databaseId"]),
                "--repo",
                REPO,
                "--name",
                f"release-{sha}",
                "--dir",
                directory,
            )
            if gh_json("api", f"repos/{REPO}/commits/main")["sha"] != sha:
                return
            payload = json.dumps(
                {
                    "ref": sha,
                    "environment": "production",
                    "auto_merge": False,
                    "required_contexts": [],
                    "description": "Verified CI artifact local deployment",
                }
            )
            # Deployment metadata is recorded by the authenticated host, never by PR jobs.
            deployment = json.loads(
                run(
                    "gh",
                    "api",
                    f"repos/{REPO}/deployments",
                    "--input",
                    "-",
                    input=payload,
                )
            )
            endpoint = f"repos/{REPO}/deployments/{deployment['id']}/statuses"
            run("gh", "api", endpoint, "-f", "state=in_progress")
            try:
                updated = deploy(artifact, sha, state)
                save(updated)
            except Exception:
                run(
                    "gh",
                    "api",
                    endpoint,
                    "-f",
                    "state=failure",
                    "-f",
                    "description=Deployment failed; previous release restored or rollback needs attention",
                )
                raise
            run("gh", "api", endpoint, "-f", "state=success")
            print("Deployed", sha)


if __name__ == "__main__":
    main()
