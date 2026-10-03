"""Explicit offline import; no download, installation or upstream executable hooks."""

import argparse
import json
from pathlib import Path

from .archive import iso_day
from .pipeline import build


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "source", "input-root", "plan", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--start", required=True, type=iso_day)
    parser.add_argument("--end", required=True, type=iso_day)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = build(
        archive_path=args.archive,
        description=args.source,
        root=args.input_root,
        plan=args.plan,
        start=args.start,
        end=args.end,
        output=args.output,
        project_root=args.project_root,
    )
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "status",
                    "research_eligible",
                    "quality_status",
                    "rows",
                    "elapsed_seconds",
                )
            },
            ensure_ascii=False,
        )
    )
    return 2  # A successful candidate conversion still cannot pass the formal research gate.


if __name__ == "__main__":
    raise SystemExit(main())
