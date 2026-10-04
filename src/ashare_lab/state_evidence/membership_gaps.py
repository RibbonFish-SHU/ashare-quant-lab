"""Offline conditional universe and quote-gap diagnostics; never a certified PIT universe."""

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import time

import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab.community.archive import parse_calendar, parse_intervals
from ashare_lab.community.source import planned_symbols, verified_file
from ashare_lab.contracts import ContractError
from ashare_lab.data.queries import symbol as canonical_symbol
from ashare_lab.storage import read_dataset
from .common import (
    day,
    digest,
    execution_identity,
    file_ref,
    json_text,
    read_json,
    require,
    save_json,
    verify_unchanged,
)
from .inputs import calendar_rows

START, END = date(2023, 1, 1), date(2023, 12, 31)
ASSUMPTIONS = [
    "January 3 vendor set becomes a full baseline after applying the verified December 2022 delta.",
    "Only the two selected regular events change CSI300 membership during 2023.",
    "Neither assumption is certified; this is a consistency diagnostic, not a historical universe.",
]
SCHEMA = "conditional-membership-gap-candidate-v1"


def _symbols(values):
    require(isinstance(values, list) and values, "empty/non-list symbol set")
    require(len(values) == len(set(values)), "duplicate symbol")
    for value in values:
        require(isinstance(value, str) and len(value) == 9, "invalid symbol")
        require(
            canonical_symbol(value[-2:].lower() + "." + value[:6]) == value,
            "noncanonical symbol",
        )
    return set(values)


def reconstruct_states(snapshot_rows, events, expected_states, *, member_count=300):
    """Recompute the prior conditional claim from its vendor baseline and reviewed deltas."""
    snapshots = defaultdict(set)
    for row in snapshot_rows:
        d, symbol = day(row["query_date"]), row["symbol"]
        require(symbol not in snapshots[d], "duplicate member in vendor snapshot")
        require(
            row["source"] == "baostock" and row["sdk"] == "0.9.4" and row["available_time"] is None,
            "snapshot source or historical availability differs",
        )
        snapshots[d].add(symbol)
    require(
        snapshots and all(len(s) == member_count for s in snapshots.values()),
        "vendor snapshot member count differs",
    )
    baseline = snapshots.get(date(2023, 1, 3))
    require(baseline is not None, "January 3 vendor baseline missing")
    require(
        events["schema_version"] == "official-membership-events-v1"
        and "unverified" in events["coverage"],
        "event coverage must remain explicitly unverified",
    )
    selected = events["events"]
    require(len(selected) == 3, "expected December 2022 and two 2023 selected events")
    require(len({e["id"] for e in selected}) == 3, "duplicate event identity")
    first_days = [day(e["first_trading_date"]) for e in selected]
    require(
        first_days == sorted(set(first_days))
        and first_days[0] < START <= first_days[1] < first_days[2] <= END,
        "selected event dates unordered or outside scope",
    )
    members, states = set(baseline), []
    for number, event in enumerate(selected):
        require(
            day(event["publish_date"])
            <= day(event["effective_after_close_date"])
            < day(event["first_trading_date"])
            and event["publish_precision"] == "day",
            "invalid announcement/effective-date order",
        )
        removed, added = _symbols(event["removed"]), _symbols(event["added"])
        require(
            len(added) == len(removed) and removed <= members and not added & members,
            "delta does not replace disjoint existing members",
        )
        members = (members - removed) | added
        require(len(members) == member_count, "conditional member count differs")
        states.append(
            {
                "from": str(START if number == 0 else first_days[number]),
                "members": sorted(members),
                "event": event["id"],
            }
        )
    require(states == expected_states, "reconstructed states differ from retained reconciliation")
    return states


def conditional_calendar(states, calendar, *, member_count=300):
    """Use each event from its first trading day, with unknown historical availability."""
    require(states and states[0]["from"] == str(START), "conditional baseline missing")
    dates = [day(s["from"]) for s in states]
    require(dates == sorted(set(dates)), "duplicate/unordered conditional state dates")
    for d in dates[1:]:
        require(d in calendar and calendar[d]["is_open"], "event start is not an open day")
    for state in states:
        require(len(_symbols(state["members"])) == member_count, "conditional count differs")
    result = []
    for d, reference in sorted(calendar.items()):
        if not reference["is_open"]:
            continue
        state = [s for s in states if day(s["from"]) <= d][-1]
        for symbol in state["members"]:
            result.append(
                {
                    "symbol": symbol,
                    "event_date": d,
                    "conditional_state_from": day(state["from"]),
                    "selected_event_id": state["event"],
                    "conditional_member": True,
                    "available_time": None,
                    "research_eligible": False,
                    "baseline_certified": False,
                    "temporary_event_coverage_certified": False,
                }
            )
    return result


def _candidate(directory, kind, tables, extra_files, references):
    directory = Path(directory).resolve()
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path)
    references.append(file_ref(manifest_path))
    schema, status = {
        "baostock": ("baostock-candidate-v2", "complete_candidates_research_blocked"),
        "community": ("community-quote-candidate-v1", "built_candidate"),
        "state": ("state-evidence-candidate-v1", "built_candidate"),
    }[kind]
    require(
        manifest.get("schema_version") == schema
        and manifest.get("status") == status
        and manifest.get("research_eligible") is False
        and manifest.get("data_kind") == "real_candidate",
        "input must be a completed, unverified candidate of the selected kind",
    )
    paths = {}
    for table in tables:
        spec = manifest["tables"][table]
        path = verified_file(directory, {**spec, "path": table + ".parquet"}, references)
        require(pq.read_metadata(path).num_rows == spec["rows"], "candidate row count differs")
        paths[table] = path
    for name in extra_files:
        paths[name] = verified_file(
            directory, {**manifest["files"][name], "path": name}, references
        )
    return manifest, paths


def _bound(path, locators, message):
    actual = file_ref(path)
    require(
        any(
            Path(r["path"].replace("\\", "/")).name == Path(path).name
            and r["sha256"] == actual["sha256"]
            and ("bytes" not in r or r["bytes"] == actual["bytes"])
            for r in locators
        ),
        message,
    )


def _grid(rows, symbols, open_days, label):
    result = {}
    for row in rows:
        key = row["symbol"], row["event_date"]
        require(key not in result, f"duplicate {label} row")
        require(key[0] in symbols and key[1] in open_days, f"{label} outside original grid")
        result[key] = row
    require(len(result) == len(symbols) * len(open_days), f"{label} has missing grid rows")
    return result


def compare_membership(intervals, membership, open_days, *, member_count=300):
    expected = defaultdict(set)
    for row in membership:
        expected[row["event_date"]].add(row["symbol"])
    comparisons = []
    for d in open_days:
        symbols = [r["symbol"] for r in intervals if r["start"] <= d <= r["end"]]
        require(None not in symbols, "unmapped community membership overlaps selected year")
        require(len(symbols) == len(set(symbols)), "overlapping community membership intervals")
        require(len(symbols) == member_count, "community daily member count differs")
        observed = set(symbols)
        comparisons.append(
            {
                "event_date": d,
                "observed_count": len(observed),
                "conditional_count": len(expected[d]),
                "missing_from_community": sorted(expected[d] - observed),
                "extra_in_community": sorted(observed - expected[d]),
            }
        )
    require(
        not comparisons[0]["missing_from_community"] and not comparisons[0]["extra_in_community"],
        "conditional baseline does not agree with independent community reference",
    )
    return comparisons


def diagnose_quotes(quotes, membership, symbols, open_days, state_rows=None):
    """A missing quote stays missing, regardless of membership or a halt reference."""
    quote_grid = _grid(quotes, symbols, open_days, "quote")
    states = _grid(state_rows, symbols, open_days, "state") if state_rows is not None else {}
    member_grid = {(r["symbol"], r["event_date"]): r for r in membership}
    require(len(member_grid) == len(membership), "duplicate conditional membership row")
    require(set(member_grid) <= set(quote_grid), "conditional membership outside original plan")
    gaps = []
    for key, quote in sorted(quote_grid.items(), key=lambda pair: (pair[0][1], pair[0][0])):
        require(
            quote["research_eligible"] is False and quote["available_time"] is None,
            "quote availability must remain unverified",
        )
        price = quote["raw_close"]
        require(
            price is None or (math.isfinite(price) and price > 0),
            "invalid/zero quote must not be treated as observed price",
        )
        require(
            (price is not None) == (quote["record_status"] == "price_candidate"),
            "quote status/price inconsistency",
        )
        state = states.get(key)
        if state:
            require(
                state["research_eligible"] is False
                and state["historical_asof_certified"] is False
                and state["can_trade"] is None
                and state["community_price_missing"] is (price is None),
                "state availability or bound community quote differs",
            )
        if price is None:
            gaps.append(
                {
                    "symbol": key[0],
                    "event_date": key[1],
                    "raw_close": None,
                    "original_record_status": quote["record_status"],
                    "conditional_member": key in member_grid,
                    "state_classification": state["classification"] if state else None,
                    "state_event_ids_json": state["trading_event_ids_json"] if state else "[]",
                    "available_time": None,
                    "research_eligible": False,
                    "imputed": False,
                }
            )
    selected = [r for r in gaps if r["conditional_member"]]
    by_symbol = []
    for symbol in sorted({r["symbol"] for r in gaps}):
        rows = [r for r in gaps if r["symbol"] == symbol]
        by_symbol.append(
            {
                "symbol": symbol,
                "missing_rows_original_plan": len(rows),
                "missing_rows_conditional_members": sum(r["conditional_member"] for r in rows),
                "original_record_statuses": dict(
                    Counter(r["original_record_status"] for r in rows)
                ),
                "state_classifications": dict(
                    Counter(r["state_classification"] or "unattached" for r in rows)
                ),
                "dates": [str(r["event_date"]) for r in rows],
            }
        )
    return gaps, {
        "original_plan_symbols": len(symbols),
        "open_days": len(open_days),
        "original_plan_rows": len(quote_grid),
        "conditional_membership_rows": len(member_grid),
        "original_plan_missing_quotes": len(gaps),
        "conditional_member_missing_quotes": len(selected),
        "outside_conditional_membership_missing_quotes": len(gaps) - len(selected),
        "missing_by_symbol": by_symbol,
        "missing_values_imputed": 0,
    }


def prepare(
    *,
    input_root,
    reconciliation,
    events,
    original_plan,
    bao_candidate,
    community_candidate,
    state_candidate=None,
):
    """Read and bind selected retained inputs; this function writes no artifacts."""
    root = Path(input_root).resolve()
    reconciliation, events, original_plan = map(Path, (reconciliation, events, original_plan))
    references = [file_ref(p) for p in (reconciliation, events, original_plan)]
    report = read_json(reconciliation)
    require(
        report.get("kind") == "conditional_membership_reconciliation"
        and report.get("research_eligible") is False
        and report["assumptions"] == ASSUMPTIONS,
        "conditional evidence requires the original uncertified assumptions",
    )
    retained = {}
    for locator in report["inputs"]:
        path = verified_file(root, locator, references)
        if "event" in locator:
            require(locator["event"] not in retained, "duplicate event source binding")
            retained[locator["event"]] = file_ref(path)
    _bound(events, report["inputs"], "event config is not bound to prior reconciliation")
    _bound(original_plan, report["inputs"], "original plan is not bound to prior reconciliation")
    event_doc = read_json(events)
    for event in event_doc["events"]:
        source = retained.get(event["id"], {})
        require(
            source.get("sha256") == event["source_sha256"]
            and Path(source["path"]).name == event["source_artifact"],
            "reviewed event is not bound to its original artifact",
        )
    symbols = planned_symbols(read_json(original_plan), START, END)
    require(len(symbols) == 336, "original 336-symbol coverage denominator changed")
    _, bao = _candidate(
        bao_candidate, "baostock", ["calendar", "membership_snapshots"], [], references
    )
    _bound(bao["membership_snapshots"], report["inputs"], "vendor baseline binding differs")
    calendar = calendar_rows(pq.read_table(bao["calendar"]).to_pylist(), START, END)
    open_days = sorted(d for d, r in calendar.items() if r["is_open"])
    require(len(open_days) == 242, "retained 2023 open-day denominator changed")
    states = reconstruct_states(
        pq.read_table(bao["membership_snapshots"]).to_pylist(), event_doc, report["states"]
    )
    union = sorted({symbol for state in states for symbol in state["members"]})
    require(
        union == report["conditional_union"]
        and len(union) == report["conditional_union_size"]
        and set(union) <= set(symbols)
        and report["planned_union_size"] == len(symbols),
        "conditional/original union evidence differs",
    )
    community_manifest, community = _candidate(
        community_candidate,
        "community",
        ["quotes"],
        ["csi300.reference.txt", "day.txt"],
        references,
    )
    for path in (reconciliation, original_plan, bao["calendar"]):
        _bound(path, community_manifest["inputs"], "community input binding differs")
    require(
        community_manifest["start"] == str(START)
        and community_manifest["end"] == str(END)
        and community_manifest["symbols"] == symbols,
        "community candidate scope differs from original plan",
    )
    latest = day(community_manifest["release_latest"])
    require(latest == END, "community release is outside selected 2023 scope")
    days = parse_calendar(community["day.txt"].read_bytes(), latest)
    require(
        [d for d in days if START <= d <= END] == open_days, "community/calendar open days differ"
    )
    membership = conditional_calendar(states, calendar)
    comparisons = compare_membership(
        parse_intervals(
            community["csi300.reference.txt"].read_bytes(), latest, allow_unmapped=True
        ),
        membership,
        open_days,
    )
    quote_rows = pq.read_table(
        community["quotes"],
        columns=[
            "symbol",
            "event_date",
            "raw_close",
            "record_status",
            "available_time",
            "research_eligible",
        ],
    ).to_pylist()
    state_rows = None
    if state_candidate is not None:
        state_manifest, state = _candidate(
            state_candidate, "state", ["events", "daily_references"], [], references
        )
        for path in (original_plan, bao["calendar"], community["quotes"]):
            _bound(path, state_manifest["inputs"], "state candidate input binding differs")
        event_rows = pq.read_table(state["events"]).to_pylist()
        event_ids = {r["event_id"] for r in event_rows}
        require(len(event_ids) == len(event_rows), "duplicate state event identity")
        require(
            all(
                r["available_time"] is None and r["research_eligible"] is False for r in event_rows
            ),
            "state events must remain unverified",
        )
        state_rows = pq.read_table(
            state["daily_references"],
            columns=[
                "symbol",
                "event_date",
                "classification",
                "trading_event_ids_json",
                "can_trade",
                "community_price_missing",
                "historical_asof_certified",
                "research_eligible",
            ],
        ).to_pylist()
        require(
            all(set(json.loads(r["trading_event_ids_json"])) <= event_ids for r in state_rows),
            "state daily reference points to absent event",
        )
    gaps, summary = diagnose_quotes(quote_rows, membership, symbols, open_days, state_rows)
    mismatches = [r for r in comparisons if r["missing_from_community"] or r["extra_in_community"]]
    summary.update(
        research_eligible=False,
        available_time=None,
        formal_universe=False,
        assumptions=report["assumptions"],
        conditional_union_symbols=len(union),
        state_candidate_attached=state_candidate is not None,
        community_membership_mismatch_days=len(mismatches),
        membership_mismatches=mismatches,
        community_membership_mismatch_days_by_month=dict(
            Counter(str(r["event_date"])[:7] for r in mismatches)
        ),
        source_validation="prior reviewed event transcription; original artifact hashes verified; no new text extraction",
        baseline_certified=False,
        temporary_event_coverage_certified=False,
        warnings=[
            "Conditional membership is a diagnostic assumption, not a certified historical universe.",
            "Outside-conditional gaps remain in the original 336-symbol denominator and raw data.",
            "Selected halt references do not certify tradeability or unknown historical availability.",
            "Missing prices are not zero returns; no price, return or execution is imputed.",
            "Corporate-action cash/share accounting and publication completeness are not certified here.",
        ],
    )
    references = list({r["path"]: r for r in references}.values())
    verify_unchanged(references)
    return {"membership": membership, "gaps": gaps, "summary": summary, "inputs": references}


MEMBERSHIP_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_date", pa.date32()),
        ("conditional_state_from", pa.date32()),
        ("selected_event_id", pa.string()),
        ("conditional_member", pa.bool_()),
        ("available_time", pa.timestamp("us", tz="UTC")),
        ("research_eligible", pa.bool_()),
        ("baseline_certified", pa.bool_()),
        ("temporary_event_coverage_certified", pa.bool_()),
    ]
)
GAP_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_date", pa.date32()),
        ("raw_close", pa.float64()),
        ("original_record_status", pa.string()),
        ("conditional_member", pa.bool_()),
        ("state_classification", pa.string()),
        ("state_event_ids_json", pa.string()),
        ("available_time", pa.timestamp("us", tz="UTC")),
        ("research_eligible", pa.bool_()),
        ("imputed", pa.bool_()),
    ]
)


def build(*, project_root, output, **inputs):
    """Write a new immutable diagnostic candidate from a clean source commit."""
    began, clock = datetime.now(timezone.utc), time.perf_counter()
    project_root, output = Path(project_root).resolve(), Path(output).resolve()
    source = execution_identity(project_root)
    require(not output.exists(), "immutable output already exists")
    for name in ("reconciliation", "events"):
        path = Path(inputs[name]).resolve()
        require(path.is_relative_to(project_root), f"{name} must belong to project_root")
        require(
            source["files_sha256"].get(path.relative_to(project_root).as_posix()) == digest(path),
            f"{name} not bound to source commit",
        )
    prepared = prepare(**inputs)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": SCHEMA,
        "data_kind": "real_candidate",
        "status": "running",
        "research_eligible": False,
        "available_time": None,
        "online_requests": 0,
        "started_at_utc": began,
        "source": source,
        "inputs": prepared["inputs"],
        "assumptions": ASSUMPTIONS,
    }
    save_json(output / "manifest.json", manifest)
    try:
        tables = {}
        for name, schema in (("membership", MEMBERSHIP_SCHEMA), ("gaps", GAP_SCHEMA)):
            table = pa.Table.from_pylist(prepared[name], schema=schema).replace_schema_metadata(
                {
                    b"schema_version": SCHEMA.encode(),
                    b"research_eligible": b"false",
                    b"data_kind": b"real_candidate",
                }
            )
            path = output / (name + ".parquet")
            pq.write_table(table, path, compression="zstd")
            require(pq.read_table(path).equals(table), "diagnostic Parquet roundtrip differs")
            tables[name] = {**file_ref(path), "rows": len(table), "parquet_roundtrip": True}
        save_json(output / "diagnostic.json", prepared["summary"])
        verify_unchanged(prepared["inputs"])
        require(
            execution_identity(project_root) == source, "source changed during diagnostic build"
        )
        manifest.update(
            status="built_candidate",
            tables=tables,
            inputs_unchanged=True,
            diagnostic=file_ref(output / "diagnostic.json"),
        )
        save_json(output / "manifest.json", manifest)
        try:
            read_dataset(output)
        except ContractError:
            manifest["canonical_reader_rejected"] = True
        else:
            raise ValueError("formal reader accepted conditional diagnostic")
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        manifest.update(
            finished_at_utc=datetime.now(timezone.utc), elapsed_seconds=time.perf_counter() - clock
        )
        save_json(output / "manifest.json", manifest)
    return {"manifest": manifest, "summary": prepared["summary"]}


def summary_text(result):
    """Compact CLI response; full date/symbol evidence remains in diagnostic.json."""
    return json_text(
        {
            k: v
            for k, v in result["summary"].items()
            if k not in {"warnings", "membership_mismatches", "missing_by_symbol"}
        }
    )
