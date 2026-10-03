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


# The reviewed Phase 0 followup omitted source/SDK/API metadata. Only these exact
# preserved bytes have the report/script compatibility basis; no blanket defaults.
PHASE0_FOLLOWUP_SHA256 = "f9e751c1de6fcae1d9e5a96054baeb7ee30777f66d5ff6acd1a9ce474e2cfe49"
PHASE0_FOLLOWUP_APIS = {
    "st_2023": "query_st_stocks",
    "suspended_2023": "query_suspended_stocks",
    "delisted_bars_2019": "query_history_k_data_plus",
}


def _legacy_reference(path, reference, payload):
    manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "raw-source-probe-v1"
        or manifest.get("source") != "baostock"
    ):
        raise ValueError("legacy raw archive identity differs")
    captures = [c for c in manifest["captures"] if c["file"] == path.name]
    if len(captures) != 1 or captures[0]["sha256"] != reference["sha256"]:
        raise ValueError("legacy raw capture digest differs from archive")
    known_followup = reference["sha256"] == PHASE0_FOLLOWUP_SHA256
    if (
        payload.get("source", "baostock" if known_followup else None) != "baostock"
        or payload.get("sdk", SDK_VERSION if known_followup else None) != SDK_VERSION
    ):
        raise ValueError("legacy raw source/SDK has no verified compatibility basis")
    matches = [q for q in payload["queries"] if q["name"] == reference["query_name"]]
    entries = [
        e
        for e in manifest["queries"]
        if e["capture"] == path.name and e["name"] == reference["query_name"]
    ]
    if len(matches) != 1 or len(entries) != 1:
        raise ValueError("legacy raw query identity is ambiguous")
    query, entry = matches[0], entries[0]
    if (
        query.get("error_code") != "0"
        or query.get("exception")
        or query.get("status") not in {None, "complete"}
        or entry.get("status") != "complete_raw_read_verified"
        or entry.get("error_code") != "0"
        or entry.get("exception")
        or payload.get("login", {"error_code": "0"}).get("error_code") != "0"
    ):
        raise ValueError("legacy raw response is not a verified success")
    if query["parameters"] != entry["parameters"] or len(query["rows"]) != entry["rows"]:
        raise ValueError("legacy raw parameters/row count differ from archive")
    api = query.get("api") or (PHASE0_FOLLOWUP_APIS.get(query["name"]) if known_followup else None)
    if not api:
        raise ValueError("legacy raw API has no verified compatibility basis")
    identity = validate_query({"api": api, "parameters": query["parameters"]})
    parquet = path.parent / entry["file"]
    if Path(entry["file"]).name != entry["file"] or digest(parquet) != entry["sha256"]:
        raise ValueError("legacy raw Parquet digest mismatch")
    if not raw_table(query).equals(pq.read_table(parquet)):
        raise ValueError("legacy raw Parquet values differ from capture")
    observed = payload["retrieved_at_utc"]
    if datetime.fromisoformat(observed).tzinfo is None:
        raise ValueError("legacy raw capture time has no timezone")
    return {
        **query,
        **identity,
        "query_id": query_id(identity),
        "source": "baostock",
        "sdk": SDK_VERSION,
        "observed_at_utc": observed,
        "sdk_basis": "capture" if "sdk" in payload else "phase0_report_and_script",
        "observation_precision": "legacy_capture_timestamp",
        "source_url": source_url(api),
        "status": "complete",
        "transport_integrity": "legacy_not_instrumented",
    }


def _attempt_files(path):
    manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "provider-raw-v1"
        or "response.json" not in manifest["files"]
    ):
        raise ValueError("raw attempt archive schema/files differ")
    for filename, expected in manifest["files"].items():
        if Path(filename).name != filename or digest(path.parent / filename) != expected:
            raise ValueError("raw attempt digest mismatch")
    return manifest


def legacy_records(root):
    for path in sorted(Path(root).glob("*/manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "raw-source-probe-v1":
            continue
        for entry in manifest["queries"]:
            if entry["status"] != "complete_raw_read_verified":
                continue
            captures = [c for c in manifest["captures"] if c["file"] == entry["capture"]]
            if len(captures) != 1 or Path(entry["capture"]).name != entry["capture"]:
                raise ValueError("legacy raw capture path/identity differs")
            reference = {
                "path": str((path.parent / entry["capture"]).resolve()),
                "sha256": captures[0]["sha256"],
                "query_name": entry["name"],
            }
            yield {**read_reference(reference), "raw_locator": reference}


def cached_records(roots):
    index = {}
    for root in roots:
        for record in legacy_records(root):
            if record["status"] == "complete":
                index.setdefault(record["query_id"], record)
        for path in sorted(Path(root).glob("attempts/*/*/response.json")):
            manifest = _attempt_files(path)
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["status"] == "complete" and not manifest.get("tabular_error"):
                reference = {"path": str(path.resolve()), "sha256": digest(path)}
                record = {**read_reference(reference), "raw_locator": reference}
                key = record["query_id"]
                index.setdefault(key, record)
    return index


def read_reference(reference):
    """Read a verified successful response; a run entry cannot supply its authority."""
    path = Path(reference["path"])
    if digest(path) != reference["sha256"]:
        raise ValueError("raw source capture digest changed after collection")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "query_name" in reference:
        return _legacy_reference(path, reference, payload)
    manifest = _attempt_files(path)
    if path.name != "response.json" or manifest["files"]["response.json"] != reference["sha256"]:
        raise ValueError("raw reference is not the archived response")
    if (
        payload.get("status") != "complete"
        or payload.get("error_code") != "0"
        or payload.get("exception")
        or manifest.get("status") != "complete"
        or manifest.get("tabular_error")
    ):
        raise ValueError("raw response is not a complete successful attempt")
    if payload.get("source") != "baostock" or payload.get("sdk") != SDK_VERSION:
        raise ValueError("raw response source/SDK differs from the pinned source")
    key = query_id(payload)
    if payload.get("query_id", key) != key:
        raise ValueError("raw response query identity mismatch")
    raw_table(payload)
    payload["query_id"] = key
    return payload


def successful_entry(entry):
    raw = read_reference(entry["raw_locator"])
    if entry["status"] not in {"complete", "reused"}:
        raise ValueError("raw reference requires a successful run entry")
    for field in ("source", "sdk", "api", "parameters", "error_code"):
        if entry.get(field) != raw.get(field):
            raise ValueError(f"run entry differs from authoritative raw {field}")
    for field in (
        "fields",
        "observed_at_utc",
        "observation_precision",
        "source_url",
        "sdk_basis",
        "transport_integrity",
    ):
        if field in entry and entry[field] != raw.get(field):
            raise ValueError(f"run entry differs from authoritative raw {field}")
    if entry["query_id"] != raw["query_id"] or entry["row_count"] != len(raw["rows"]):
        raise ValueError("run entry differs from authoritative raw identity/row count")
    return {**entry, **raw, "status": entry["status"], "raw_locator": entry["raw_locator"]}
