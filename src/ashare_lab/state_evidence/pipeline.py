"""Offline preparation and immutable state-reference candidate construction."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
import sys
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab.contracts import ContractError
from ashare_lab.storage import read_dataset
from .announcements import parse_announcement
from .common import (
    digest,
    execution_identity,
    file_ref,
    json_text,
    require,
    save_json,
    verify_unchanged,
)
from .inputs import load_inputs, load_references, rejected_state_sources
from .names import parse_names
from .temporal import daily_reference, event_conflicts

EVENT_SCHEMA = pa.schema(
    [
        *[
            (k, pa.string())
            for k in (
                "event_id",
                "symbol",
                "kind",
                "effective_session",
                "before_name",
                "after_name",
                "availability_basis",
                "source_timestamp",
                "source_timestamp_precision",
                "source",
                "source_url",
                "locator_json",
            )
        ],
        *[(k, pa.date32()) for k in ("effective_date", "signature_date")],
        *[
            (k, pa.bool_())
            for k in (
                "before_contains_st",
                "after_contains_st",
                "certified_st",
                "signature_date_conflict",
                "research_eligible",
            )
        ],
        *[(k, pa.timestamp("us", tz="UTC")) for k in ("observed_at_utc", "available_time")],
    ]
)
DAILY_SCHEMA = pa.schema(
    [
        *[
            (k, pa.string())
            for k in (
                "symbol",
                "name_reference",
                "name_event_id",
                "classification",
                "morning_reference",
                "afternoon_reference",
                "trading_event_ids_json",
                "raw_daily_reference_json",
                "community_reference_json",
                "calendar_locator_json",
            )
        ],
        ("event_date", pa.date32()),
        ("source_volume_shares", pa.int64()),
        *[
            (k, pa.bool_())
            for k in (
                "name_contains_st_diagnostic",
                "name_continuity_verified",
                "source_trading_active",
                "source_is_st",
                "community_price_missing",
                "positive_volume_conflict",
                "certified_st",
                "can_trade",
                "historical_asof_certified",
                "research_eligible",
            )
        ],
    ]
)


def prepare(description, input_root, pdftotext):
    """Read-only validation. No candidate files or research conclusions are produced."""
    context = load_inputs(description, input_root)
    reference = load_references(context)
    config, paths = context["config"], context["paths"]
    events, name_quality = parse_names(
        paths,
        config["names"],
        set(context["symbols"]),
        reference["missing_bao_symbols"],
        context["start"],
        context["end"],
    )
    tools = []
    for selection in config["announcements"]:
        row, tool = parse_announcement(
            paths, selection, set(context["symbols"]), context["start"], context["end"], pdftotext
        )
        events.append(row)
        tools.append(tool)
    issues = event_conflicts(events)
    rejected = rejected_state_sources(context)
    verify_unchanged(context["references"])
    return {
        "context": context,
        "reference": reference,
        "events": events,
        "name_quality": name_quality,
        "event_conflicts": issues,
        "rejected_sources": rejected,
        "tools": tools,
    }


def preparation_summary(prepared):
    c, r = prepared["context"], prepared["reference"]
    return {
        "status": "inputs_parsed_candidate_not_built",
        "research_eligible": False,
        "input_files_verified": len(c["references"]),
        "symbols": len(c["symbols"]),
        "events": len(prepared["events"]),
        "event_kinds": dict(Counter(e["kind"] for e in prepared["events"])),
        "unknown_availability_events": sum(e["available_time"] is None for e in prepared["events"]),
        "calendar_civil_days": len(r["calendar"]),
        "calendar_open_days": sum(d["is_open"] for d in r["calendar"].values()),
        "daily_raw_rows_checked": r["daily_raw_checked"],
        "candidate_rows_read": r["counts"],
        "name_quality": prepared["name_quality"],
        "event_conflicts": prepared["event_conflicts"],
        "signature_date_conflicts": [
            e["event_id"] for e in prepared["events"] if e["signature_date_conflict"]
        ],
        "rejected_sources": prepared["rejected_sources"],
        "pdf_tools": prepared["tools"],
        "candidate_created_by_preparation": False,
        "online_requests": 0,
    }


def daily_rows(prepared):
    c, refs, events = prepared["context"], prepared["reference"], prepared["events"]
    by_symbol = defaultdict(list)
    for row in events:
        by_symbol[row["symbol"]].append(row)
    calendar_path = c["paths"][c["config"]["calendar"]["table"]]
    calendar_ref = file_ref(calendar_path)
    for symbol in c["symbols"]:
        for d, cal in sorted(refs["calendar"].items()):
            if not cal["is_open"]:
                continue
            quote = refs["primary"].get((symbol, d))
            community = refs["community"].get((symbol, d))
            volume = quote.get("volume_shares") if quote else None
            state = daily_reference(
                by_symbol[symbol], symbol, d, is_open=True, volume_shares=volume
            )
            ids = sorted(
                {
                    s["trading_event_id"]
                    for s in (state["morning"], state["afternoon"])
                    if s["trading_event_id"]
                }
            )
            name = state["afternoon"]
            yield {
                "symbol": symbol,
                "event_date": d,
                "name_reference": name["name_reference"],
                "name_event_id": name["name_event_id"],
                "name_contains_st_diagnostic": name["name_contains_st_diagnostic"],
                "name_continuity_verified": False,
                "classification": state["classification"],
                "morning_reference": state["morning"]["trading_reference"],
                "afternoon_reference": state["afternoon"]["trading_reference"],
                "trading_event_ids_json": json_text(ids),
                "source_trading_active": quote["source_trading_active"] if quote else None,
                "source_is_st": quote["source_is_st"] if quote else None,
                "source_volume_shares": volume,
                "raw_daily_reference_json": json_text(quote),
                "community_reference_json": json_text(community),
                "community_price_missing": community["raw_close"] is None if community else None,
                "calendar_locator_json": json_text(
                    {
                        **calendar_ref,
                        "raw_path": cal["raw_path"],
                        "raw_sha256": cal["raw_sha256"],
                        "row_number": cal["row_number"],
                        "source": cal["source"],
                        "exchange_scope": cal["exchange_scope"],
                    }
                ),
                "positive_volume_conflict": state["positive_volume_conflict"],
                "certified_st": None,
                "can_trade": None,
                "historical_asof_certified": False,
                "research_eligible": False,
            }


def quality_report(prepared, rows):
    coverage = []
    by_symbol = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    conflicts = [
        r
        for r in rows
        if r["positive_volume_conflict"] or r["classification"] == "conflicting_reference"
    ]
    for symbol, items in sorted(by_symbol.items()):
        covered = {
            r["event_date"]
            for r in items
            if r["classification"] in {"full_day_halt_reference", "partial_day_halt_reference"}
        }
        event_dates = {
            e["effective_date"]
            for e in prepared["events"]
            if e["symbol"] == symbol and e["kind"] != "name_change"
        }
        covered |= event_dates & {r["event_date"] for r in items}
        coverage.append(
            {
                "symbol": symbol,
                "open_days": len(items),
                "name_reference_days": sum(r["name_reference"] is not None for r in items),
                "name_unknown_days": sum(r["name_reference"] is None for r in items),
                "classification_counts": dict(Counter(r["classification"] for r in items)),
                "selected_event_or_halt_interval_days": sorted(covered),
                "days_without_selected_trading_evidence": [
                    r["event_date"] for r in items if r["event_date"] not in covered
                ],
                "certified_state_days": 0,
            }
        )
    total = len(rows)
    return {
        "schema_version": "state-evidence-quality-v1",
        "research_eligible": False,
        "formal_research_gate": "blocked",
        "status": "fail" if conflicts or prepared["event_conflicts"] else "unverified",
        "rows": total,
        "scope_symbols": prepared["context"]["symbols"],
        "validated_inputs": preparation_summary(prepared),
        "classification_counts": dict(Counter(r["classification"] for r in rows)),
        "full_day_halt_dates": [
            {"symbol": r["symbol"], "date": r["event_date"]}
            for r in rows
            if r["classification"] == "full_day_halt_reference"
        ],
        "partial_day_with_positive_volume": [
            {
                "symbol": r["symbol"],
                "date": r["event_date"],
                "source_volume_shares": r["source_volume_shares"],
            }
            for r in rows
            if r["classification"] == "partial_day_halt_reference"
            and (r["source_volume_shares"] or 0) > 0
        ],
        "conflicts": [
            {"symbol": r["symbol"], "date": r["event_date"], "classification": r["classification"]}
            for r in conflicts
        ],
        "source_status_unknown_rows": sum(r["source_trading_active"] is None for r in rows),
        "community_price_missing_rows": sum(r["community_price_missing"] is True for r in rows),
        "strict_default_unknown_availability_excluded": sum(
            e["available_time"] is None for e in prepared["events"]
        ),
        "certified_state_rows": 0,
        "uncertified_state_rows": total,
        "coverage": coverage,
        "limits": [
            "All daily rows are retrospective references, never execution eligibility.",
            "Name continuity/completeness is unverified; no name event does not mean non-ST.",
            "Source timestamps and midnight date labels are not verified availability.",
            "observed_at is usable only via the explicit conservative query policy, never backdated.",
            "A resume ends the selected halt reference, not all possible trading restrictions.",
            "Daily provider flags, missing prices and empty API results do not certify whole-day state.",
            "Joint BaoStock calendar is a retained reference with unknown historical availability.",
        ],
    }


def write_table(path, rows, schema):
    table = pa.Table.from_pylist(rows, schema=schema).replace_schema_metadata(
        {b"schema_version": b"state-evidence-candidate-v1", b"research_eligible": b"false"}
    )
    pq.write_table(table, path, compression="zstd", row_group_size=2420)
    stored = pq.read_table(path)
    require(stored.equals(table), "state Parquet roundtrip differs")
    with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as con:
        con.execute("SET TimeZone='UTC'")
        restored = con.execute("select * from read_parquet(?)", [str(path)]).fetch_arrow_table()
    require(restored.cast(table.schema).equals(table), "state DuckDB roundtrip differs")
    return {
        **file_ref(path),
        "rows": len(table),
        "parquet_roundtrip": True,
        "duckdb_roundtrip": True,
    }


def build(*, description, input_root, pdftotext, output, project_root):
    start, began = time.perf_counter(), datetime.now(timezone.utc)
    source = execution_identity(project_root)
    description = Path(description).resolve()
    project_root = Path(project_root).resolve()
    require(description.is_relative_to(project_root), "selection config is outside project_root")
    require(
        source["files_sha256"].get(description.relative_to(project_root).as_posix())
        == digest(description)
        == source["state_module"]["files"]
        .get(description.relative_to(project_root).as_posix(), {})
        .get("sha256"),
        "selection config is not bound to source commit",
    )
    output = Path(output).resolve()
    require(not output.exists(), "immutable output already exists")
    prepared = prepare(description, input_root, pdftotext)
    require(
        not prepared["event_conflicts"], "unresolved duplicate/conflicting name or trading events"
    )
    # All inputs are existing files; rejecting an existing output protects old candidate directories.
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "state-evidence-candidate-v1",
        "data_kind": "real_candidate",
        "research_eligible": False,
        "status": "running",
        "source": source,
        "started_at_utc": began,
        "inputs": prepared["context"]["references"],
        "config": prepared["context"]["config"],
        "pdf_tools": prepared["tools"],
        "online_requests": 0,
        "python": sys.version,
        "executable": sys.executable,
        "packages": {p: version(p) for p in ("pyarrow", "duckdb")},
    }
    save_json(output / "manifest.json", manifest)
    try:
        rows = list(daily_rows(prepared))
        tables = {
            "events": write_table(output / "events.parquet", prepared["events"], EVENT_SCHEMA),
            "daily_references": write_table(
                output / "daily_references.parquet", rows, DAILY_SCHEMA
            ),
        }
        quality = quality_report(prepared, rows)
        save_json(output / "quality.json", quality)
        require(quality["status"] != "fail", "state reference consistency failed; outputs retained")
        verify_unchanged(prepared["context"]["references"])
        require(execution_identity(project_root) == source, "source changed during candidate build")
        manifest.update(
            status="built_candidate",
            tables=tables,
            rows=len(rows),
            quality_status=quality["status"],
            inputs_unchanged=True,
        )
        save_json(output / "manifest.json", manifest)
        try:
            read_dataset(output)
        except ContractError:
            manifest["canonical_reader_rejected"] = True
        else:
            raise ValueError("formal reader accepted state reference candidate")
        manifest["files"] = {
            p.name: file_ref(p) for p in sorted(output.iterdir()) if p.name != "manifest.json"
        }
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        manifest.update(
            finished_at_utc=datetime.now(timezone.utc), elapsed_seconds=time.perf_counter() - start
        )
        save_json(output / "manifest.json", manifest)
    return manifest
