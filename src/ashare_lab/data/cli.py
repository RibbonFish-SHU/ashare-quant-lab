"""Run with python -m ashare_lab.data.cli; quality exit 2 means research is blocked."""

import argparse
import json
from pathlib import Path
import subprocess

from .collect import collect
from .pipeline import build
from .plans import membership_2023, reuse_plan, securities_2023, write_plan


def access_directory(root):
    # All worktrees of this project share one lock/ledger, including subprocesses.
    common = Path(
        subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            timeout=15,
            encoding="utf-8",
        ).strip()
    )
    return common.parent / ".cache" / "phase1" / "source-access"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("plan-reuse")
    p.add_argument("--raw-root", type=Path, action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("plan-2023")
    p.add_argument("stage", choices=("membership", "securities"))
    p.add_argument("--run", type=Path, action="append", default=[])
    p.add_argument("--events", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("collect")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--reuse-root", type=Path, action="append", default=[])
    p.add_argument("--sdk-wheel", type=Path)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--delay", type=float, default=0.25)
    p.add_argument("--daily-budget", type=int, default=10000)
    p.add_argument("--batch-size", type=int, default=1, choices=range(1, 21))
    p = commands.add_parser("build")
    p.add_argument("--run", type=Path, action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--events", type=Path)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    if args.command.startswith("plan-"):
        if args.command == "plan-reuse":
            value = reuse_plan(args.raw_root)
        elif args.stage == "membership":
            value = membership_2023()
        else:
            value = securities_2023(args.run, args.events)
        write_plan(args.output, value)
        print(json.dumps({"plan": str(args.output), "queries": len(value["queries"])}))
        return 0
    if args.command == "collect":
        result = collect(
            args.plan,
            args.output,
            args.reuse_root,
            project_root=root,
            access_state=access_directory(root),
            sdk_wheel=args.sdk_wheel,
            offline=args.offline,
            timeout=args.timeout,
            delay=args.delay,
            budget=args.daily_budget,
            batch_size=args.batch_size,
        )
        print(
            json.dumps(
                {
                    "run": str(args.output / "run.json"),
                    "status": result["status"],
                    "attempt_entries": len(result["queries"]),
                }
            )
        )
        return 0 if result["status"] == "complete" else 2
    _, quality = build(args.run, args.output, project_root=root, event_path=args.events)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "quality_status": quality["status"],
                "research_eligible": quality["research_eligible"],
                "rows_by_kind": quality["rows_by_kind"],
            }
        )
    )
    return 0 if quality["research_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
