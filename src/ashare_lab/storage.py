"""Immutable Parquet snapshots, checked manifests and parameterized DuckDB reads."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import KEYS, SCHEMAS, SCHEMA_VERSION, ContractError, aware, validate_records


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_dataset(root: Path, version: str, tables: dict[str, pa.Table], *, data_kind: str) -> Path:
    if data_kind not in {"synthetic", "real"} or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", version
    ):
        raise ContractError("invalid dataset kind or version path component")
    if set(tables) != set(SCHEMAS):
        raise ContractError(f"dataset requires exactly these tables: {sorted(SCHEMAS)}")
    checked = {
        name: validate_records(name, table.to_pylist(), data_kind=data_kind)
        for name, table in tables.items()
    }
    parent = Path(root) / data_kind
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / version
    if destination.exists():
        raise FileExistsError(f"immutable dataset exists: {destination}")
    staging = parent / f".{version}-{uuid.uuid4().hex}.partial"
    staging.mkdir()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "data_kind": data_kind,
        "data_version": version,
        "tables": {},
    }
    # A failed staging directory remains for diagnosis; it is never a valid snapshot.
    for name, table in checked.items():
        path = staging / f"{name}.parquet"
        pq.write_table(table, path, compression="zstd")
        manifest["tables"][name] = {"rows": len(table), "sha256": sha256(path)}
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    staging.rename(destination)
    return destination


def read_dataset(path: Path) -> dict[str, pa.Table]:
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != SCHEMA_VERSION or set(manifest["tables"]) != set(SCHEMAS):
        raise ContractError("unsupported or incomplete dataset manifest")
    if path.parent.name != manifest["data_kind"] or path.name != manifest["data_version"]:
        raise ContractError("dataset namespace disagrees with manifest")
    tables = {}
    for name, expected in manifest["tables"].items():
        file = path / f"{name}.parquet"
        if sha256(file) != expected["sha256"]:
            raise ContractError(f"{name}: Parquet checksum mismatch")
        table = pq.read_table(file)
        metadata = table.schema.metadata or {}
        if not table.schema.equals(SCHEMAS[name], check_metadata=False):
            raise ContractError(f"{name}: Parquet schema mismatch")
        if metadata.get(b"data_kind", b"").decode() != manifest["data_kind"]:
            raise ContractError(f"{name}: Parquet data kind mismatch")
        if metadata.get(b"schema_version", b"").decode() != SCHEMA_VERSION:
            raise ContractError(f"{name}: Parquet schema version mismatch")
        if len(table) != expected["rows"]:
            raise ContractError(f"{name}: row count mismatch")
        tables[name] = validate_records(name, table.to_pylist(), data_kind=manifest["data_kind"])
    return tables


def query_as_of(path: Path, name: str, cutoff: datetime) -> pa.Table:
    """Read the verified snapshot with availability filtering inside the SQL window."""
    aware(cutoff, "cutoff")
    tables = read_dataset(path)
    if name not in SCHEMAS:
        raise ContractError(f"unknown table: {name}")
    kind = tables[name].schema.metadata[b"data_kind"].decode()
    keys = ", ".join(f'"{key}"' for key in KEYS[name])
    with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as connection:
        connection.execute("SET TimeZone='UTC'")
        table = connection.execute(
            f"SELECT * FROM read_parquet(?) WHERE available_time <= ? "
            f"QUALIFY row_number() OVER (PARTITION BY {keys} "
            "ORDER BY record_version DESC) = 1",
            [str(Path(path) / f"{name}.parquet"), cutoff],
        ).fetch_arrow_table()
    return validate_records(name, table.to_pylist(), data_kind=kind)
