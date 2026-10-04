"""Audit retained CNINFO search evidence without network access."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .claims import build_reviewed_candidates, validate_reviewed_claims
from .common import DividendEvidenceError
from .search import audit_search_run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit-search", "validate-claims", "build-claims"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--claims", type=Path)
    parser.add_argument("--pdftotext", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.report and args.report.exists():
        parser.error("report already exists; preserve the previous audit")
    if args.command != "audit-search" and (args.claims is None or args.pdftotext is None):
        parser.error("claim commands require --claims and --pdftotext")
    if args.command == "build-claims" and args.output is None:
        parser.error("build-claims requires --output")
    try:
        if args.command == "audit-search":
            result = audit_search_run(args.plan, args.manifest, args.evidence_root)
            code = (
                0
                if result["selected_queries_complete"]
                else 1
                if result["status"] == "invalid_evidence"
                else 2
            )
        else:
            parameters = (args.claims, args.plan, args.manifest, args.evidence_root, args.pdftotext)
            result = (
                build_reviewed_candidates(*parameters, args.output)
                if args.command == "build-claims"
                else validate_reviewed_claims(*parameters)
            )
            code = 2 if args.command == "build-claims" else 0
    except (
        DividendEvidenceError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        result = {
            "schema_version": "cninfo-dividend-search-audit-v1",
            "status": "invalid_evidence",
            "selected_queries_complete": False,
            "research_eligible": False,
            "available_time": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "network_requests": 0,
        }
        code = 1
    result["interpreter"] = sys.executable
    encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
