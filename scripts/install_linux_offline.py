"""Install the measured offline bundle in this worktree; Python 3.8+ bootstrap.

Every package is checked against the transfer manifest before executing it.
The environment and temporary files are confined to this worktree. No GPU work.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import threading
import time
import traceback


def sha256(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def allocated(path):
    return int(subprocess.check_output(["du", "-s", "-B1", str(path)], text=True).split()[0])


def safe_extract(archive_path, destination):
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if destination not in target.parents and destination != target:
                raise ValueError("interpreter archive path escapes destination")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("unsupported interpreter archive member")
            if member.issym() or member.islnk():
                base = target.parent if member.issym() else destination
                link = (base / member.linkname).resolve()
                if destination not in link.parents and destination != link:
                    raise ValueError("interpreter archive link escapes destination")
        archive.extractall(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--reserve-gib", type=int, default=5)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if os.name != "posix":
        raise RuntimeError("Linux installer only")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if head != args.commit or subprocess.check_output(["git", "status", "--porcelain"], cwd=root):
        raise RuntimeError("server must execute the requested clean commit")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest["source_commit"] != head or manifest["lock_sha256"] != sha256(root / "uv.lock"):
        raise RuntimeError("offline bundle differs from source commit/lock")
    cache = root / ".cache" / "phase0-linux"
    logs = cache / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    distributions = args.manifest.resolve().parent / "distributions"
    for item in manifest["artifacts"]:
        path = distributions / item["filename"]
        if path.parent != distributions or sha256(path) != item["verified_sha256"]:
            raise RuntimeError("artifact hash/path mismatch: " + item["filename"])
    state = {
        "source_commit": head,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "steps": [],
        "initial_free_bytes": shutil.disk_usage(root).free,
        "initial_allocated_bytes": allocated(root),
    }
    python_artifact = manifest["python"]
    runtime = manifest["runtime"] + [manifest["project"]]
    unpacked = sum(item["unpacked_bytes"] for item in runtime)
    largest = max(item["unpacked_bytes"] for item in runtime)
    # Same-filesystem hardlinks share cache/venv payload. Allow one additional
    # largest wheel during serialized extraction, per-member block overhead,
    # and 1 GiB for scripts, metadata, tests and import bytecode.
    required = (
        python_artifact["unpacked_bytes"]
        + unpacked
        + largest
        + 1024**3
        + 8192 * sum(item["archive_members"] for item in manifest["artifacts"])
    )
    state["additional_peak_bound_bytes"] = required
    state["reserve_bytes"] = args.reserve_gib * 1024**3
    save(logs / "installation.json", state)
    if state["initial_free_bytes"] < required + state["reserve_bytes"]:
        raise RuntimeError("insufficient space for measured installation bound plus reserve")
    stopping = threading.Event()
    samples = [state["initial_free_bytes"]]

    def sample():
        while not stopping.wait(0.25):
            samples.append(shutil.disk_usage(root).free)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    env = dict(os.environ)
    temporary = cache / "tmp"
    temporary.mkdir(exist_ok=True)
    env.update(
        CUDA_VISIBLE_DEVICES="",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        UV_CONCURRENT_INSTALLS="1",
        UV_CONCURRENT_DOWNLOADS="1",
        UV_COMPILE_BYTECODE="false",
        TMPDIR=str(temporary),
    )

    def run(name, command):
        started = time.monotonic()
        with (logs / (name + ".log")).open("w", encoding="utf-8") as log:
            result = subprocess.run(
                command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900
            )
        step = {
            "name": name,
            "command": command,
            "exit_code": result.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "free_bytes": shutil.disk_usage(root).free,
            "allocated_bytes": allocated(root),
        }
        state["steps"].append(step)
        save(logs / "installation.json", state)
        print(json.dumps(step), flush=True)
        if result.returncode:
            raise RuntimeError(name + " failed; see persistent log")

    try:
        interpreter_root = cache / "interpreter"
        safe_extract(distributions / python_artifact["filename"], interpreter_root.resolve())
        python = str(interpreter_root / "python" / "bin" / "python3.11")
        uv_cache = str(cache / "uv-cache")
        venv_python = str(root / ".venv" / "bin" / "python")
        run("python", [python, "--version"])
        run(
            "venv",
            [
                "uv",
                "venv",
                "--python",
                python,
                str(root / ".venv"),
                "--offline",
                "--no-python-downloads",
                "--cache-dir",
                uv_cache,
            ],
        )
        run(
            "sync",
            [
                "uv",
                "pip",
                "sync",
                "configs/requirements-linux-cu118.lock",
                "--python",
                venv_python,
                "--offline",
                "--no-index",
                "--require-hashes",
                "--find-links",
                str(distributions),
                "--cache-dir",
                uv_cache,
                "--link-mode",
                "hardlink",
            ],
        )
        run(
            "project",
            [
                "uv",
                "pip",
                "install",
                "--python",
                venv_python,
                "--offline",
                "--no-deps",
                "--no-index",
                "--cache-dir",
                uv_cache,
                "--link-mode",
                "hardlink",
                str(distributions / manifest["project"]["filename"]),
            ],
        )
        run(
            "dependencies",
            ["uv", "pip", "check", "--python", venv_python, "--offline", "--cache-dir", uv_cache],
        )
        run("tests", [venv_python, "-m", "pytest", "-q"])
        run("ruff", [str(root / ".venv/bin/ruff"), "check", "src", "tests", "scripts"])
        run("cpu-smoke", [venv_python, "-m", "ashare_lab.cli", "smoke"])
        state["status"] = "passed"
    except Exception:
        state["status"] = "failed"
        state["traceback"] = traceback.format_exc()
        raise
    finally:
        stopping.set()
        thread.join()
        state.update(
            finished_at=datetime.now(timezone.utc).isoformat(),
            minimum_sampled_free_bytes=min(samples),
            sample_interval_seconds=0.25,
            samples=len(samples),
            final_free_bytes=shutil.disk_usage(root).free,
            final_allocated_bytes=allocated(root),
        )
        save(logs / "installation.json", state)


if __name__ == "__main__":
    main()
