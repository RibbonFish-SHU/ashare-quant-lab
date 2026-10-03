"""Preserve observed provider responses and check complete raw Parquet / SQL reads.

This archive cannot enter the canonical PIT dataset reader. No historical
publication time, availability time, trading status or price is invented here.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab.contracts import ContractError
from ashare_lab.storage import read_dataset


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive(inputs: list[Path], output: Path) -> dict:
    project = Path(__file__).resolve().parents[1]
    raw_root = (project / "datasets/raw/baostock").resolve()
    output = output.resolve()
    if output.parent != raw_root:
        raise ValueError("output must be one snapshot directory in datasets/raw/baostock")
    if output.exists():
        raise FileExistsError(f"immutable raw snapshot already exists: {output}")
    captures = []
    for source in inputs:
        raw = source.read_bytes()
        capture = json.loads(raw)
        retrieved = datetime.fromisoformat(capture["retrieved_at_utc"])
        if retrieved.tzinfo is None or not isinstance(capture["queries"], list):
            raise ValueError("capture requires timezone-aware retrieval time and query list")
        captures.append((source, raw, capture))

    raw_root.mkdir(parents=True, exist_ok=True)
    staging = raw_root / f".{output.name}-{uuid.uuid4().hex}.partial"
    staging.mkdir()
    manifest = {
        "schema_version": "raw-source-probe-v1",
        "source": "baostock",
        "data_kind": "real_raw",
        "research_eligible": False,
        "historical_publication_and_availability": "unverified",
        "archived_at_utc": datetime.now(timezone.utc).isoformat(),
        "archive_script_sha256": digest(Path(__file__)),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=project, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=project, text=True)
        ),
        "captures": [],
        "queries": [],
    }
    with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as connection:
        for number, (source, raw, capture) in enumerate(captures):
            saved = staging / f"capture-{number:02d}.json"
            saved.write_bytes(raw)
            manifest["captures"].append(
                {"file": saved.name, "original_name": source.name, "sha256": digest(saved)}
            )
            for query in capture["queries"]:
                name = query["name"]
                if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                    raise ValueError("invalid query name")
                entry = {
                    "name": name,
                    "capture": saved.name,
                    "parameters": query["parameters"],
                    "observed_at_utc": capture["retrieved_at_utc"],
                    "error_code": query.get("error_code"),
                    "error_msg": query.get("error_msg"),
                    "exception": query.get("exception"),
                    "rows": len(query["rows"]),
                }
                manifest["queries"].append(entry)
                if query.get("error_code") != "0" or query.get("exception"):
                    entry["status"] = "provider_or_probe_error_preserved"
                    continue
                fields = query["fields"]
                if not fields or len(fields) != len(set(fields)):
                    raise ValueError("provider fields must be nonempty and unique")
                if any(
                    len(row) != len(fields) or any(not isinstance(cell, str) for cell in row)
                    for row in query["rows"]
                ):
                    raise ValueError("raw rows must preserve each original string field")
                schema = pa.schema([(field, pa.string()) for field in fields])
                records = [dict(zip(fields, row)) for row in query["rows"]]
                table = pa.Table.from_pylist(records, schema=schema).replace_schema_metadata(
                    {b"data_kind": b"real_raw", b"research_eligible": b"false"}
                )
                path = staging / f"{len(manifest['queries']):02d}-{name}.parquet"
                pq.write_table(table, path, compression="zstd")
                if pq.read_table(path).to_pylist() != records:
                    raise ValueError("raw Parquet roundtrip differs from source response")
                sql_rows = connection.execute(
                    "SELECT * FROM read_parquet(?)", [str(path)]
                ).fetchall()
                if [list(row) for row in sql_rows] != query["rows"]:
                    raise ValueError("DuckDB read differs from complete raw response")
                entry.update(
                    file=path.name,
                    sha256=digest(path),
                    status="complete_raw_read_verified",
                    empty_strings=sum(cell == "" for row in query["rows"] for cell in row),
                )
    manifest["total_rows"] = sum(entry["rows"] for entry in manifest["queries"])
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        read_dataset(staging)
    except ContractError:
        pass
    else:
        raise ValueError("raw data was accepted by the canonical research reader")
    staging.rename(output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = archive(args.input, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "queries": len(result["queries"]),
                "rows": result["total_rows"],
                "research_eligible": result["research_eligible"],
            }
        )
    )


if __name__ == "__main__":
    main()
