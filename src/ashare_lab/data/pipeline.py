"""Verified raw references -> immutable candidates -> machine-readable quality gate."""

from collections import Counter, defaultdict
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from ashare_lab.contracts import ContractError
from ashare_lab.runtime import code_identity
from ashare_lab.storage import read_dataset
from .candidates import VERSION, convert, table
from .quality import audit
from .queries import query_id
from .raw import digest, read_reference, save_json, utc_now


def load_run(path):
    run = json.loads(Path(path).read_text(encoding="utf-8"))
    if run.get("schema_version") != "source-run-v1":
        raise ValueError("unsupported source run")
    expected = {query_id(q) for q in run["plan"]["queries"]}
    successful = {}
    for entry in run["queries"]:
        if entry["query_id"] not in expected or query_id(entry) != entry["query_id"]:
            raise ValueError("unexpected query in run manifest")
        if entry["status"] not in {"reused", "complete"}:
            continue
        raw = read_reference(entry["raw_locator"])
        record = {**entry, "fields": raw["fields"], "rows": raw["rows"]}
        if query_id({**entry, "parameters": raw["parameters"]}) != entry["query_id"]:
            raise ValueError("raw reference does not match requested parameters")
        if raw.get("api", entry["api"]) != entry["api"]:
            raise ValueError("raw reference API differs")
        if len(record["rows"]) != entry["row_count"]:
            raise ValueError("raw reference row count differs")
        successful[entry["query_id"]] = record
    return list(successful.values()), expected == successful.keys(), run


def build(run_paths, output, *, project_root, event_path=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    records = {}
    planned = {}
    run_info = []
    for path in run_paths:
        values, _, run = load_run(path)
        planned.update({query_id(q): q for q in run["plan"]["queries"]})
        for record in values:
            key = record["query_id"]
            if key in records and records[key]["raw_locator"] != record["raw_locator"]:
                raise ValueError(
                    "conflicting captured versions of one query; choose an explicit source run"
                )
            records[key] = record
        run_info.append(
            {
                "path": str(Path(path).resolve()),
                "sha256": digest(path),
                "status": run["status"],
                "attempt_entries": len(run["queries"]),
            }
        )
    tables, issues = defaultdict(list), []
    for record in records.values():
        kind, rows, found = convert(record)
        tables[kind].extend(rows)
        issues.extend(found)
    events = (
        json.loads(Path(event_path).read_text(encoding="utf-8"))["events"] if event_path else []
    )
    missing = sorted(planned.keys() - records.keys())
    planned_by_api = Counter(q["api"] for q in planned.values())
    successful_by_api = Counter(q["api"] for q in records.values())
    report = audit(
        tables,
        list(records.values()),
        issues,
        acquisition_complete=not missing,
        events=events,
    )
    report["acquisition_coverage"] = {
        "count_basis": "unique source/SDK/API/parameter query IDs across input runs",
        "planned_queries": len(planned),
        "successful_queries": len(records),
        "missing_queries": len(missing),
        "successful_fraction": len(records) / len(planned) if planned else None,
        "by_api": {
            api: {
                "planned": count,
                "successful": successful_by_api[api],
                "missing": count - successful_by_api[api],
            }
            for api, count in sorted(planned_by_api.items())
        },
        "missing_query_ids": missing,
    }
    manifest = {
        "schema_version": VERSION,
        "data_kind": "real_candidate",
        "research_eligible": False,
        "created_at_utc": utc_now(),
        "source": code_identity(Path(project_root)),
        "runs": run_info,
        "tables": {},
        "checks": {},
        "status": "building",
        "events_sha256": digest(event_path) if event_path else None,
    }
    save_json(output / "manifest.json", manifest)
    for kind, rows in sorted(tables.items()):
        typed = table(kind, rows)
        path = output / f"{kind}.parquet"
        pq.write_table(typed, path, compression="zstd")
        if not typed.equals(pq.read_table(path)):
            raise ValueError("candidate Parquet roundtrip failed")
        with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as con:
            con.execute("SET TimeZone='UTC'")
            restored = con.execute("select * from read_parquet(?)", [str(path)]).fetch_arrow_table()
        if typed.to_pylist() != restored.to_pylist():
            raise ValueError("candidate DuckDB roundtrip failed")
        manifest["tables"][kind] = {
            "rows": len(rows),
            "sha256": digest(path),
            "parquet_roundtrip": True,
            "duckdb_roundtrip": True,
        }
    try:
        read_dataset(output)
    except ContractError:
        manifest["checks"]["canonical_reader_rejected"] = True
    else:
        raise ValueError("canonical research reader accepted an unqualified candidate")
    save_json(output / "quality.json", report)
    save_json(output / "conversion-issues.json", issues)
    manifest.update(
        status="complete_candidates_research_blocked",
        quality_sha256=digest(output / "quality.json"),
        issues_sha256=digest(output / "conversion-issues.json"),
    )
    save_json(output / "manifest.json", manifest)
    return manifest, report
