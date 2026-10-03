from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import traceback
import uuid

from .config import RunConfig


def write_json(path: Path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def code_identity(root: Path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], timeout=15)

    try:
        commit = git("rev-parse", "HEAD").decode().strip()
        status = git("status", "--porcelain=v1").decode("utf-8", errors="replace")
        tracked = git("ls-files", "-z").split(b"\0")
        untracked = git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        hashes = {}
        for item in sorted(set(tracked + untracked)):
            if not item:
                continue
            name = item.decode("utf-8")
            file = root / name
            if file.is_file():
                hashes[name] = hashlib.sha256(file.read_bytes()).hexdigest()
        return {
            "commit": commit,
            "dirty": bool(status),
            "status": status,
            "files_sha256": hashes,
            "source_sha256": hashlib.sha256(
                json.dumps(hashes, sort_keys=True).encode()
            ).hexdigest(),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot establish source identity: {exc}") from exc


class Run:
    def __init__(self, config: RunConfig, project_root: Path):
        self.config = config
        now = datetime.now(timezone.utc)
        self.path = (
            config.output_root
            / "synthetic"
            / (now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8])
        )
        self.path.mkdir(parents=True, exist_ok=False)
        self.manifest = {
            "status": "running",
            "started_at": now.isoformat(),
            "purpose": "synthetic engineering smoke; no strategy performance",
            "config": config.as_dict(),
            "command": sys.argv,
            "config_sha256": hashlib.sha256(
                json.dumps(config.as_dict(), sort_keys=True).encode()
            ).hexdigest(),
            "source": code_identity(project_root),
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "packages": dict(
                sorted(
                    (d.metadata["Name"], d.version)
                    for d in metadata.distributions()
                    if d.metadata["Name"]
                )
            ),
        }
        write_json(self.path / "manifest.json", self.manifest)

    def log(self, event, **fields):
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def finish(self, *, result=None, error=None):
        self.manifest.update(
            status="failed" if error else "passed",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        if error:
            self.manifest["error"] = f"{type(error).__name__}: {error}"
            (self.path / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
            self.log("failed", error=self.manifest["error"])
        else:
            write_json(self.path / "result.json", result)
            self.log("passed")
        write_json(self.path / "manifest.json", self.manifest)


def seed_process(seed):
    import numpy as np

    random.seed(seed)
    np.random.seed(seed)
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
