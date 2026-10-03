"""Reuse imports for <=20 sequential one-connection queries; never run in parallel."""

import argparse
import json
from pathlib import Path

from .raw import save_json
from .worker import main as one_query


def run(tasks, receipt, common_args, execute=one_query):
    state = {"finished": [], "running_index": None}
    save_json(receipt, state)
    for index, task in enumerate(tasks):
        state["running_index"] = index
        save_json(receipt, state)
        status = execute(
            ["--request", task["request"], "--progress", task["progress"], *common_args]
        )
        state["finished"].append({"index": index, "exit_code": status})
        state["running_index"] = None
        save_json(receipt, state)
        if status:
            return status
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args, common = parser.parse_known_args()
    tasks = json.loads(args.tasks.read_text(encoding="utf-8"))
    if not 1 <= len(tasks) <= 20:
        raise ValueError("bounded batch size must be 1..20")
    return run(tasks, args.receipt, common)


if __name__ == "__main__":
    raise SystemExit(main())
