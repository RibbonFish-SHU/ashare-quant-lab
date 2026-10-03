"""Bind run coverage to preserved original plan bytes, not a mutable embedded list."""

import hashlib
import json
from pathlib import Path

from .queries import SDK_VERSION, query_id, validate_query
from .raw import digest


def validate_plan(plan):
    if plan.get("source", "baostock") != "baostock" or plan.get("sdk", SDK_VERSION) != SDK_VERSION:
        raise ValueError("plan source/SDK differs from the pinned source")
    if plan.get("schema_version", "source-plan-v1") != "source-plan-v1":
        raise ValueError("unsupported source plan")
    queries = [validate_query(q) for q in plan["queries"]]
    if not queries or len({query_id(q) for q in queries}) != len(queries):
        raise ValueError("plan must contain nonempty, unique provider requests")
    return queries


def snapshot_plan(plan_path, run_directory):
    content = Path(plan_path).read_bytes()
    path = Path(run_directory) / "plan.original.json"
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValueError("existing original plan snapshot differs")
    return {"file": path.name, "sha256": hashlib.sha256(content).hexdigest()}


def verify_plan(run, run_path, original_plans=()):
    """v1 needs explicit original files; v2 carries an immutable sibling snapshot."""
    expected = run["plan_sha256"]
    if run.get("schema_version") == "source-run-v2":
        binding = run.get("plan_binding", {})
        filename = binding.get("file", "")
        if filename != "plan.original.json" or binding.get("sha256") != expected:
            raise ValueError("invalid original plan binding")
        path = Path(run_path).parent / filename
        method = "run_v2_original_snapshot"
    elif run.get("schema_version") == "source-run-v1":
        matches = [Path(p) for p in original_plans if digest(p) == expected]
        if not matches:
            raise ValueError("v1 run requires an explicit original plan with matching digest")
        path = matches[0]
        method = "v1_explicit_original_plan"
    else:
        raise ValueError("unsupported source run")
    if not path.is_file() or digest(path) != expected:
        raise ValueError("original plan snapshot digest mismatch")
    original = json.loads(path.read_text(encoding="utf-8-sig"))
    if original != run["plan"]:
        raise ValueError("embedded plan differs from verified original plan")
    validate_plan(original)
    return {"method": method, "path": str(path.resolve()), "sha256": expected}
