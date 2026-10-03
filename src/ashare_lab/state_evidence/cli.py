"""Local-only state evidence validation/build; no source fetching or model hooks."""

import argparse
from pathlib import Path

from .common import json_text, save_json
from .pipeline import build, preparation_summary, prepare


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "build"))
    for name in ("source", "input-root", "pdftotext", "project-root"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate":
        result = preparation_summary(prepare(args.source, args.input_root, args.pdftotext))
        if args.report:
            # Read-only validation has its own immutable diagnostic; never a candidate namespace.
            if args.report.exists():
                raise FileExistsError("validation report already exists")
            args.report.parent.mkdir(parents=True, exist_ok=True)
            save_json(args.report, result)
        print(json_text(result))
        return 0
    if args.output is None:
        parser.error("build requires --output")
    result = build(
        description=args.source,
        input_root=args.input_root,
        pdftotext=args.pdftotext,
        output=args.output,
        project_root=args.project_root,
    )
    print(
        json_text(
            {
                k: result[k]
                for k in (
                    "status",
                    "research_eligible",
                    "quality_status",
                    "rows",
                    "elapsed_seconds",
                )
            }
        )
    )
    return 2  # Candidate built, formal research still blocked.


if __name__ == "__main__":
    raise SystemExit(main())
