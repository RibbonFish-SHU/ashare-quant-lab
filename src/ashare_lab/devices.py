"""Read-only device inventory and explicit selection; no task scheduler or claims."""

import csv
import io
import os
import subprocess


def inventory():
    def query(args):
        result = subprocess.run(
            ["nvidia-smi", *args, "--format=csv,noheader,nounits"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return list(csv.reader(io.StringIO(result.stdout), skipinitialspace=True))

    devices = query(
        ["--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu,driver_version"]
    )
    processes = query(["--query-compute-apps=pid,gpu_uuid,used_memory"])
    return {
        "devices": [
            dict(
                zip(
                    (
                        "index",
                        "uuid",
                        "name",
                        "memory_total_mib",
                        "memory_used_mib",
                        "utilization_pct",
                        "driver",
                    ),
                    row,
                )
            )
            for row in devices
        ],
        "compute_processes": processes,
        "reservation_verified": False,
    }


def select_device(device: str, *, allocation_confirmed: bool = False):
    if device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        return {"selected": "cpu"}
    if not allocation_confirmed:
        raise RuntimeError("GPU allocation/booking must be confirmed before running smoke")
    state = inventory()
    candidates = [item for item in state["devices"] if item["uuid"] == device]
    if len(candidates) != 1:
        raise RuntimeError(f"requested GPU not found: {device}")
    chosen = candidates[0]
    if (
        float(chosen["utilization_pct"]) != 0
        or float(chosen["memory_used_mib"]) > 64
        or any(len(row) >= 2 and row[1] == device for row in state["compute_processes"])
    ):
        raise RuntimeError(f"requested GPU is busy: {device}")
    os.environ["CUDA_VISIBLE_DEVICES"] = device
    state.update(selected=device, reservation_verified=True)
    return state
