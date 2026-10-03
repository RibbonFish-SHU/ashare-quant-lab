"""Bounded fallback collection; every candidate build exits 2 for blocked research."""

import argparse
import json
from pathlib import Path

from ashare_lab.data.raw import digest, save_json
from .collect import collect, load_collection, plan_missing
from .pipeline import build, load_probes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("plan")
    p.add_argument("--baostock-run", type=Path, required=True)
    p.add_argument("--original-plan", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--limit", type=int, default=10)
    p = commands.add_parser("probe")
    p.add_argument("--source-root", type=Path, required=True)
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("collect")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("build")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "plan":
        if args.output.exists():
            raise FileExistsError("plan is immutable")
        result = plan_missing(args.baostock_run, args.original_plan, args.limit)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        save_json(args.output, result)
        print(
            json.dumps(
                {
                    "missing_baostock_bar_queries": result["missing_baostock_bar_queries"],
                    "selected": len(result["requests"]),
                    "plan_sha256": digest(args.output),
                }
            )
        )
        return 0
    if args.command == "collect":
        run = collect(args.plan, args.output, project_root=Path.cwd())
        print(json.dumps({"status": run["status"], "attempts": len(run["attempts"])}))
        return 0 if run["status"] == "complete" else 2
    if args.command == "probe":
        records, refs = load_probes(args.source_root)
        acquisition = {
            "scope": "seven explicitly archived probe captures",
            "online_requests": 0,
            "successful_captures": len(records),
        }
    else:
        records, run = load_collection(args.run)
        refs = [{"path": str(args.run.resolve()), "sha256": digest(args.run)}]
        acquisition = {
            "planned": len(run["plan"]["requests"]),
            "successful": len(records),
            "missing": len(run["plan"]["requests"]) - len(records),
            "status": run["status"],
            "scope": "Eastmoney pilot only; original BaoStock 401 remains unchanged",
        }
    _, report = build(
        records,
        args.output,
        project_root=Path.cwd(),
        reference=args.reference,
        input_refs=refs,
        acquisition=acquisition,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "raw_rows": report["raw_rows"],
                "interval_rows": report["interval_rows"],
                "research_eligible": False,
            }
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
