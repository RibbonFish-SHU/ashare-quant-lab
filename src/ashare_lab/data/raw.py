"""Immutable raw attempts, verified legacy reuse and lossless raw storage."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .queries import SDK_VERSION, query_id, source_url, validate_query


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def raw_table(record):
    fields, rows = record.get("fields", []), record.get("rows", [])
    if not fields or len(set(fields)) != len(fields):
        raise ValueError("raw fields must be nonempty and unique")
    if any(len(row) != len(fields) or any(not isinstance(c, str) for c in row) for row in rows):
        raise ValueError("raw rows must retain exactly one string per source field")
    return pa.Table.from_pylist(
        [dict(zip(fields, row)) for row in rows],
        schema=pa.schema(
            [(f, pa.string()) for f in fields],
            metadata={b"data_kind": b"real_raw", b"research_eligible": b"false"},
        ),
    )


def archive_attempt(record, directory):
    """Preserve errors and partial rows too; only complete attempts can be reused."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    save_json(directory / "response.json", record)
    files = {"response.json": digest(directory / "response.json")}
    storage_error = None
    if record.get("fields"):
        try:
            table = raw_table(record)
        except (ValueError, TypeError) as exc:
            # Preserve malformed provider responses as JSON, never discard evidence
            # just because it cannot be represented by the raw tabular contract.
            storage_error = str(exc)
            table = None
    else:
        table = None
    if table is not None:
        pq.write_table(table, directory / "rows.parquet", compression="zstd")
        restored = pq.read_table(directory / "rows.parquet")
        if not table.equals(restored):
            raise ValueError("raw Parquet changed provider values")
        with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as con:
            rows = con.execute(
                "select * from read_parquet(?)", [str(directory / "rows.parquet")]
            ).fetchall()
        if [list(row) for row in rows] != record["rows"]:
            raise ValueError("raw DuckDB changed provider values")
        files["rows.parquet"] = digest(directory / "rows.parquet")
    manifest = {
        "schema_version": "provider-raw-v1",
        "data_kind": "real_raw",
        "research_eligible": False,
        "status": record["status"],
        "files": files,
        "tabular_error": storage_error,
    }
    save_json(directory / "manifest.json", manifest)
    return directory / "response.json"


def legacy_records(root):
    fallback = {
        "st_2023": "query_st_stocks",
        "suspended_2023": "query_suspended_stocks",
        "delisted_bars_2019": "query_history_k_data_plus",
    }
    for manifest_path in sorted(Path(root).glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "raw-source-probe-v1":
            continue
        for capture in manifest["captures"]:
            path = manifest_path.parent / capture["file"]
            if path.parent != manifest_path.parent or digest(path) != capture["sha256"]:
                raise ValueError("legacy capture digest/path mismatch")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("source", "baostock") != "baostock"
                or payload.get("sdk", SDK_VERSION) != SDK_VERSION
            ):
                raise ValueError("legacy source/SDK differs from the pinned source")
            observed = payload["retrieved_at_utc"]
            if datetime.fromisoformat(observed).tzinfo is None:
                raise ValueError("legacy capture time has no timezone")
            for query in payload["queries"]:
                api = query.get("api", fallback.get(query["name"]))
                if not api:
                    raise ValueError("legacy query API cannot be established")
                identity = validate_query({"api": api, "parameters": query["parameters"]})
                entries = [
                    x
                    for x in manifest["queries"]
                    if x["capture"] == capture["file"] and x["name"] == query["name"]
                ]
                if len(entries) != 1:
                    raise ValueError("ambiguous legacy manifest entry")
                entry = entries[0]
                success = entry["status"] == "complete_raw_read_verified"
                if success:
                    raw_table(query)
                    if digest(manifest_path.parent / entry["file"]) != entry["sha256"]:
                        raise ValueError("legacy raw Parquet digest mismatch")
                yield {
                    **query,
                    **identity,
                    "query_id": query_id(identity),
                    "source": "baostock",
                    "sdk": payload.get("sdk", SDK_VERSION),
                    "observed_at_utc": observed,
                    "sdk_basis": "capture" if "sdk" in payload else "phase0_report_and_script",
                    "observation_precision": "legacy_capture_timestamp",
                    "source_url": source_url(api),
                    "status": "complete" if success else "provider_error",
                    "transport_integrity": "legacy_not_instrumented",
                    "raw_locator": {
                        "path": str(path.resolve()),
                        "sha256": capture["sha256"],
                        "query_name": query["name"],
                    },
                }


def cached_records(roots):
    index = {}
    for root in roots:
        for record in legacy_records(root):
            if record["status"] == "complete":
                index.setdefault(record["query_id"], record)
        for path in sorted(Path(root).glob("attempts/*/*/response.json")):
            manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
            for filename, expected in manifest["files"].items():
                if Path(filename).name != filename or digest(path.parent / filename) != expected:
                    raise ValueError("raw attempt digest mismatch")
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["status"] == "complete" and not manifest.get("tabular_error"):
                if record.get("source") != "baostock" or record.get("sdk") != SDK_VERSION:
                    raise ValueError("raw attempt source/SDK mismatch")
                raw_table(record)
                record["raw_locator"] = {"path": str(path.resolve()), "sha256": digest(path)}
                key = query_id(record)
                if record.get("query_id", key) != key:
                    raise ValueError("raw attempt query identity mismatch")
                record["query_id"] = key
                index.setdefault(key, record)
    return index


def read_reference(reference):
    path = Path(reference["path"])
    if digest(path) != reference["sha256"]:
        raise ValueError("source capture changed after collection")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "query_name" in reference:
        matches = [q for q in payload["queries"] if q["name"] == reference["query_name"]]
        if len(matches) != 1:
            raise ValueError("source query identity is ambiguous")
        return matches[0]
    return payload
