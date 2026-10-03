"""Record one explicitly allocated GPU smoke and its bounded resource observations."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from ashare_lab.devices import inventory, select_device


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--gpu", required=True, help="full GPU UUID; no automatic selection")
    parser.add_argument("--allocation-confirmed", action="store_true", required=True)
    parser.add_argument("--authorization", required=True, help="resource owner's authorization")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if head != args.commit or subprocess.check_output(["git", "status", "--porcelain"], cwd=root):
        raise RuntimeError("GPU validation requires the requested clean commit")
    if not args.gpu.startswith("GPU-") or len(args.gpu) != 40:
        raise ValueError("use a full GPU UUID")
    # select_device checks fresh utilization, memory and compute processes.
    before = select_device(args.gpu, allocation_confirmed=args.allocation_confirmed)
    logs = root / ".cache" / "phase0-linux" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = logs / ("gpu-" + stamp + ".json")
    log_path = report_path.with_suffix(".log")
    env = dict(os.environ)
    env.update(
        CUDA_VISIBLE_DEVICES=args.gpu,
        CUBLAS_WORKSPACE_CONFIG=":4096:8",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
    )
    command = [
        sys.executable,
        "-m",
        "ashare_lab.cli",
        "smoke",
        "--gpu",
        args.gpu,
        "--allocation-confirmed",
    ]
    report = {
        "source_commit": head,
        "authorization": args.authorization,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "selected_uuid": args.gpu,
        "before": before,
        "command": command,
        "environment": {
            key: env[key]
            for key in (
                "CUDA_VISIBLE_DEVICES",
                "CUBLAS_WORKSPACE_CONFIG",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
            )
        },
        "samples": [],
        "log_path": str(log_path),
    }

    def save():
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    save()
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            with subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log) as process:
                report["pid"] = process.pid
                save()
                try:
                    while process.poll() is None:
                        if time.monotonic() - started >= 300:
                            raise TimeoutError("GPU smoke exceeded 300 seconds")
                        report["samples"].append(
                            {
                                "elapsed_seconds": round(time.monotonic() - started, 3),
                                **inventory(),
                            }
                        )
                        save()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=15)
                    report["exit_code"] = process.returncode
    finally:
        report["after"] = inventory()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({"report": str(report_path), "exit_code": report["exit_code"]}))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
