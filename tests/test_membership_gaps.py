"""Conditional membership stays uncertified and cannot remove original quote gaps."""

from copy import deepcopy
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.contracts import ContractError
from ashare_lab.data.queries import BAR_FIELDS
from ashare_lab.state_evidence import membership_gaps as gaps
from ashare_lab.state_evidence.common import digest, file_ref, read_json, save_json
from ashare_lab.state_evidence.inputs import calendar_rows
from ashare_lab.storage import read_dataset


def event(number, removed, added, source=None):
    return {
        "id": f"synthetic-{number}",
        "publish_date": ["2022-11-25", "2023-05-26", "2023-11-24"][number],
        "effective_after_close_date": ["2022-12-09", "2023-06-09", "2023-12-08"][number],
        "first_trading_date": ["2022-12-12", "2023-06-12", "2023-12-11"][number],
        "publish_precision": "day",
        "removed": removed,
        "added": added,
        "source_artifact": source.name if source else "synthetic-source",
        "source_sha256": digest(source) if source else "a" * 64,
    }


def vendor_rows(symbols):
    return [
        {
            "symbol": s,
            "query_date": date(2023, 1, 3),
            "source": "baostock",
            "sdk": "0.9.4",
            "available_time": None,
        }
        for s in symbols
    ]


def event_doc(events):
    return {
        "schema_version": "official-membership-events-v1",
        "coverage": "unverified",
        "events": events,
    }


@pytest.fixture
def small():
    symbols = ["000001.SZ", "000002.SZ", "000063.SZ", "000069.SZ", "000100.SZ"]
    events = event_doc(
        [
            event(0, [symbols[1]], [symbols[2]]),
            event(1, [symbols[2]], [symbols[3]]),
            event(2, [symbols[3]], [symbols[4]]),
        ]
    )
    states = [
        {"from": d, "event": f"synthetic-{i}", "members": [symbols[0], symbols[i + 2]]}
        for i, d in enumerate(("2023-01-01", "2023-06-12", "2023-12-11"))
    ]
    return symbols, events, states


def test_reconstructs_reviewed_deltas_not_handwritten_states(small):
    symbols, events, states = small
    original = deepcopy(events)
    assert (
        gaps.reconstruct_states(vendor_rows(symbols[:2]), events, states, member_count=2) == states
    )
    assert events == original
    states[1]["members"] = [symbols[0], symbols[2]]
    with pytest.raises(ValueError, match="reconstructed states differ"):
        gaps.reconstruct_states(vendor_rows(symbols[:2]), events, states, member_count=2)


@pytest.mark.parametrize(
    "change,error",
    [
        ("duplicate_vendor", "duplicate member"),
        ("availability", "availability"),
        ("duplicate_event", "duplicate event identity"),
        ("invalid_delta", "delta does not replace"),
        ("late_publication", "date order"),
        ("duplicate_symbol", "duplicate symbol"),
    ],
)
def test_reconstruction_rejects_ambiguous_inputs(small, change, error):
    symbols, events, states = small
    rows = vendor_rows(symbols[:2])
    if change == "duplicate_vendor":
        rows.append(deepcopy(rows[0]))
    elif change == "availability":
        rows[0]["available_time"] = "2023-01-03"
    elif change == "duplicate_event":
        events["events"][1]["id"] = events["events"][0]["id"]
    elif change == "invalid_delta":
        events["events"][1]["removed"] = [symbols[1]]
    elif change == "late_publication":
        events["events"][1]["publish_date"] = "2023-06-13"
    else:
        events["events"][1]["added"] *= 2
    with pytest.raises(ValueError, match=error):
        gaps.reconstruct_states(rows, events, states, member_count=2)


def small_calendar():
    days = [
        date.fromisoformat(d)
        for d in ("2023-01-03", "2023-06-09", "2023-06-12", "2023-12-08", "2023-12-11")
    ]
    return {d: {"event_date": d, "is_open": True} for d in days}


def test_first_open_day_boundary_never_backdates_or_certifies(small):
    symbols, _, states = small
    rows = gaps.conditional_calendar(states, small_calendar(), member_count=2)
    members = {d: {r["symbol"] for r in rows if r["event_date"] == d} for d in small_calendar()}
    assert symbols[2] in members[date(2023, 6, 9)]
    assert symbols[2] not in members[date(2023, 6, 12)]
    assert symbols[3] in members[date(2023, 12, 8)]
    assert symbols[4] in members[date(2023, 12, 11)]
    assert all(
        r["available_time"] is None
        and r["research_eligible"] is False
        and not r["baseline_certified"]
        and not r["temporary_event_coverage_certified"]
        for r in rows
    )
    cal = small_calendar()
    cal[date(2023, 6, 12)]["is_open"] = False
    with pytest.raises(ValueError, match="not an open day"):
        gaps.conditional_calendar(states, cal, member_count=2)


def test_calendar_missing_civil_day_is_rejected():
    row = {
        "event_date": date(2023, 1, 1),
        "is_open": False,
        "source": "baostock",
        "sdk": "0.9.4",
        "exchange_scope": "SSE_SZSE_provider_joint",
    }
    with pytest.raises(ValueError, match="missing civil days"):
        calendar_rows([row], date(2023, 1, 1), date(2023, 1, 2))
    with pytest.raises(ValueError, match="duplicate/invalid"):
        calendar_rows([row, row], date(2023, 1, 1), date(2023, 1, 1))


def quote_rows(symbols, days):
    return [
        {
            "symbol": s,
            "event_date": d,
            "raw_close": 10.0,
            "record_status": "price_candidate",
            "research_eligible": False,
            "available_time": None,
        }
        for d in days
        for s in symbols
    ]


def test_missing_quotes_are_preserved_inside_and_outside_conditional_membership(small):
    symbols, _, states = small
    days = list(small_calendar())
    membership = gaps.conditional_calendar(states, small_calendar(), member_count=2)
    quotes = quote_rows(symbols, days)
    for row in quotes:
        if row["event_date"] == date(2023, 6, 12) and row["symbol"] in symbols[2:4]:
            row.update(raw_close=None, record_status="missing_with_suspension_reference")
    before = deepcopy(quotes)
    missing, summary = gaps.diagnose_quotes(quotes, membership, symbols, days)
    assert quotes == before
    assert summary["original_plan_rows"] == 25
    assert summary["conditional_membership_rows"] == 10
    assert summary["original_plan_missing_quotes"] == 2
    assert summary["conditional_member_missing_quotes"] == 1
    assert summary["outside_conditional_membership_missing_quotes"] == 1
    assert all(r["raw_close"] is None and not r["imputed"] for r in missing)


@pytest.mark.parametrize(
    "change,error",
    [
        ("missing", "missing grid rows"),
        ("duplicate", "duplicate quote"),
        ("zero", "invalid/zero"),
        ("nan", "invalid/zero"),
        ("certified", "remain unverified"),
        ("wrong_status", "status/price inconsistency"),
    ],
)
def test_quote_grid_rejects_missing_rows_duplicates_and_imputed_zero(small, change, error):
    symbols, _, states = small
    quotes = quote_rows(symbols, small_calendar())
    membership = gaps.conditional_calendar(states, small_calendar(), member_count=2)
    if change == "missing":
        quotes.pop()
    elif change == "duplicate":
        quotes.append(deepcopy(quotes[0]))
    elif change in {"zero", "nan"}:
        quotes[0]["raw_close"] = 0.0 if change == "zero" else float("nan")
    elif change == "certified":
        quotes[0]["research_eligible"] = True
    else:
        quotes[0]["record_status"] = "missing_with_suspension_reference"
    with pytest.raises(ValueError, match=error):
        gaps.diagnose_quotes(quotes, membership, symbols, list(small_calendar()))


def test_community_lag_is_diagnosed_and_overlap_rejected(small):
    symbols, _, states = small
    rows = gaps.conditional_calendar(states, small_calendar(), member_count=2)
    intervals = [
        {"symbol": symbols[0], "start": gaps.START, "end": gaps.END},
        {"symbol": symbols[2], "start": gaps.START, "end": date(2023, 6, 12)},
        {"symbol": symbols[3], "start": date(2023, 6, 13), "end": date(2023, 12, 10)},
        {"symbol": symbols[4], "start": date(2023, 12, 11), "end": gaps.END},
    ]
    compared = gaps.compare_membership(intervals, rows, list(small_calendar()), member_count=2)
    mismatch = [r for r in compared if r["missing_from_community"]]
    assert len(mismatch) == 1 and mismatch[0]["event_date"] == date(2023, 6, 12)
    assert mismatch[0]["missing_from_community"] == [symbols[3]]
    intervals.append(deepcopy(intervals[0]))
    with pytest.raises(ValueError, match="overlapping community"):
        gaps.compare_membership(intervals, rows, list(small_calendar()), member_count=2)


def write_candidate(path, kind, tables, **extra):
    path.mkdir()
    manifest = {
        "schema_version": kind,
        "status": "built_candidate",
        "data_kind": "real_candidate",
        "research_eligible": False,
        "tables": {},
        **extra,
    }
    for name, rows in tables.items():
        target = path / (name + ".parquet")
        pq.write_table(pa.Table.from_pylist(rows), target)
        manifest["tables"][name] = {**file_ref(target), "rows": len(rows)}
    save_json(path / "manifest.json", manifest)
    return manifest


@pytest.mark.parametrize(
    "change,error",
    [
        ("digest", "digest differs"),
        ("research", "completed, unverified candidate"),
        ("status", "completed, unverified candidate"),
        ("count", "row count differs"),
    ],
)
def test_state_candidate_rejects_table_replacement_and_non_candidates(tmp_path, change, error):
    path = tmp_path / "state"
    manifest = write_candidate(
        path, "state-evidence-candidate-v1", {"events": [{"id": "original"}]}
    )
    if change == "digest":
        pq.write_table(pa.table({"id": ["replaced"]}), path / "events.parquet")
    elif change == "research":
        manifest["research_eligible"] = True
    elif change == "status":
        manifest["status"] = "failed"
    else:
        manifest["tables"]["events"]["rows"] = 10
    save_json(path / "manifest.json", manifest)
    with pytest.raises(ValueError, match=error):
        gaps._candidate(path, "state", ["events"], [], [])


@pytest.fixture
def integrated(tmp_path):
    """Synthetic 336-by-242 inputs exercise real provenance checks without network data."""
    symbols = [f"{i:06d}.SZ" for i in range(1, 337)]
    files = []
    for i in range(3):
        p = tmp_path / f"source-{i}.txt"
        p.write_text("synthetic reviewed source " + str(i), encoding="utf-8")
        files.append(p)
    events = event_doc([event(i, [symbols[i]], [symbols[300 + i]], files[i]) for i in range(3)])
    event_path = tmp_path / "events.json"
    save_json(event_path, events)
    states, current = [], set(symbols[:300])
    for i, e in enumerate(events["events"]):
        current = (current - set(e["removed"])) | set(e["added"])
        states.append(
            {
                "from": str(gaps.START) if i == 0 else e["first_trading_date"],
                "event": e["id"],
                "members": sorted(current),
            }
        )
    plan_path = tmp_path / "plan.json"
    codes = ["sz." + s[:6] for s in symbols]
    plan = {
        "schema_version": "source-plan-v1",
        "codes": codes,
        "queries": [
            {
                "api": "query_history_k_data_plus",
                "parameters": {
                    "code": code,
                    "fields": BAR_FIELDS,
                    "start_date": str(gaps.START),
                    "end_date": str(gaps.END),
                    "frequency": "d",
                    "adjustflag": "3",
                },
            }
            for code in codes
        ],
    }
    save_json(plan_path, plan)
    days = [gaps.START + timedelta(days=i) for i in range(365)]
    removed = {d for d in days if d.month == 2 and d.weekday() < 5}
    removed = set(sorted(removed)[:18])
    open_days = [d for d in days if d.weekday() < 5 and d not in removed]
    assert len(open_days) == 242
    calendar = [
        {
            "event_date": d,
            "is_open": d in open_days,
            "source": "baostock",
            "sdk": "0.9.4",
            "exchange_scope": "SSE_SZSE_provider_joint",
        }
        for d in days
    ]
    bao = tmp_path / "bao"
    write_candidate(
        bao,
        "baostock-candidate-v2",
        {"calendar": calendar, "membership_snapshots": vendor_rows(symbols[:300])},
        status="complete_candidates_research_blocked",
    )
    report_path = tmp_path / "reconciliation.json"
    union = sorted({s for state in states for s in state["members"]})
    save_json(
        report_path,
        {
            "kind": "conditional_membership_reconciliation",
            "research_eligible": False,
            "assumptions": gaps.ASSUMPTIONS,
            "states": states,
            "conditional_union": union,
            "conditional_union_size": len(union),
            "planned_union_size": 336,
            "inputs": [
                file_ref(event_path),
                file_ref(plan_path),
                file_ref(bao / "membership_snapshots.parquet"),
                *[{**file_ref(p), "event": f"synthetic-{i}"} for i, p in enumerate(files)],
            ],
        },
    )
    community = tmp_path / "community"
    quotes = quote_rows(symbols, open_days)
    # One original-plan gap outside the conditional baseline, one inside.
    for row in quotes[:336]:
        if row["symbol"] in (symbols[0], symbols[5]):
            row.update(raw_close=None, record_status="missing_with_suspension_reference")
    cm = write_candidate(
        community,
        "community-quote-candidate-v1",
        {"quotes": quotes},
        inputs=[file_ref(report_path), file_ref(plan_path), file_ref(bao / "calendar.parquet")],
        start=str(gaps.START),
        end=str(gaps.END),
        release_latest=str(gaps.END),
        symbols=symbols,
    )
    lines = []
    for i, state in enumerate(states):
        end = day_before(states[i + 1]["from"]) if i < 2 else gaps.END
        for symbol in state["members"]:
            lines.append(f"SZ{symbol[:6]}\t{state['from']}\t{end}\n")
    (community / "csi300.reference.txt").write_text("".join(lines), encoding="utf-8")
    (community / "day.txt").write_text("\n".join(map(str, open_days)) + "\n", encoding="utf-8")
    cm["files"] = {name: file_ref(community / name) for name in ("csi300.reference.txt", "day.txt")}
    save_json(community / "manifest.json", cm)
    return {
        "input_root": tmp_path,
        "reconciliation": report_path,
        "events": event_path,
        "original_plan": plan_path,
        "bao_candidate": bao,
        "community_candidate": community,
    }


def day_before(value):
    return date.fromisoformat(value) - timedelta(days=1)


def test_real_loader_and_immutable_candidate_roundtrip_keep_assumptions(
    integrated, tmp_path, monkeypatch
):
    prepared = gaps.prepare(**integrated)
    assert len(prepared["membership"]) == 72600
    assert prepared["summary"]["original_plan_missing_quotes"] == 2
    assert prepared["summary"]["conditional_member_missing_quotes"] == 1
    assert prepared["summary"]["community_membership_mismatch_days"] == 0
    source = {
        "commit": "synthetic",
        "files_sha256": {
            integrated[name].name: digest(integrated[name]) for name in ("reconciliation", "events")
        },
    }
    monkeypatch.setattr(gaps, "execution_identity", lambda _: source)
    gate_statuses = []

    def check_completed_candidate(path):
        gate_statuses.append(read_json(path / "manifest.json")["status"])
        return read_dataset(path)

    monkeypatch.setattr(gaps, "read_dataset", check_completed_candidate)
    output = tmp_path / "result"
    result = gaps.build(project_root=tmp_path, output=output, **integrated)
    assert gate_statuses == ["built_candidate"]
    assert result["manifest"]["status"] == "built_candidate"
    assert result["manifest"]["canonical_reader_rejected"] is True
    assert result["summary"]["assumptions"] == gaps.ASSUMPTIONS
    assert pq.read_table(output / "membership.parquet").num_rows == 72600
    assert pq.read_table(output / "gaps.parquet")["raw_close"].null_count == 2
    with pytest.raises(ContractError):
        read_dataset(output)
    with pytest.raises(ValueError, match="already exists"):
        gaps.build(project_root=tmp_path, output=output, **integrated)


def test_prior_evidence_and_source_digest_changes_are_rejected(integrated):
    report = read_json(integrated["reconciliation"])
    report["assumptions"] = ["certified"]
    save_json(integrated["reconciliation"], report)
    with pytest.raises(ValueError, match="original uncertified assumptions"):
        gaps.prepare(**integrated)
    report["assumptions"] = gaps.ASSUMPTIONS
    save_json(integrated["reconciliation"], report)
    (integrated["input_root"] / "source-0.txt").write_bytes(b"altered source")
    with pytest.raises(ValueError, match="digest differs"):
        gaps.prepare(**integrated)


def attach_synthetic_state(integrated):
    community = integrated["community_candidate"]
    quotes = pq.read_table(community / "quotes.parquet").to_pylist()
    state = integrated["input_root"] / "state"
    daily = [
        {
            "symbol": q["symbol"],
            "event_date": q["event_date"],
            "classification": "full_day_halt_reference"
            if q["raw_close"] is None
            else "no_selected_halt_reference",
            "trading_event_ids_json": '["synthetic-halt"]' if q["raw_close"] is None else "[]",
            "can_trade": None,
            "community_price_missing": q["raw_close"] is None,
            "historical_asof_certified": False,
            "research_eligible": False,
        }
        for q in quotes
    ]
    manifest = write_candidate(
        state,
        "state-evidence-candidate-v1",
        {
            "events": [
                {"event_id": "synthetic-halt", "available_time": None, "research_eligible": False}
            ],
            "daily_references": daily,
        },
        inputs=[
            file_ref(integrated["original_plan"]),
            file_ref(integrated["bao_candidate"] / "calendar.parquet"),
            file_ref(community / "quotes.parquet"),
        ],
    )
    return state, manifest


def test_attached_state_explains_but_does_not_fill_missing_quotes(integrated):
    state, manifest = attach_synthetic_state(integrated)
    prepared = gaps.prepare(**integrated, state_candidate=state)
    assert prepared["summary"]["state_candidate_attached"] is True
    assert len(prepared["gaps"]) == 2
    assert all(
        r["state_classification"] == "full_day_halt_reference"
        and r["raw_close"] is None
        and r["available_time"] is None
        and r["research_eligible"] is False
        for r in prepared["gaps"]
    )
    manifest["inputs"][-1]["sha256"] = "0" * 64
    save_json(state / "manifest.json", manifest)
    with pytest.raises(ValueError, match="state candidate input binding differs"):
        gaps.prepare(**integrated, state_candidate=state)


def test_attached_state_cannot_certify_tradeability(small):
    symbols, _, states = small
    days = list(small_calendar())
    membership = gaps.conditional_calendar(states, small_calendar(), member_count=2)
    quotes = quote_rows(symbols, days)
    state = [
        {
            "symbol": q["symbol"],
            "event_date": q["event_date"],
            "research_eligible": False,
            "historical_asof_certified": False,
            "can_trade": None,
            "community_price_missing": False,
        }
        for q in quotes
    ]
    state[0]["can_trade"] = True
    with pytest.raises(ValueError, match="state availability"):
        gaps.diagnose_quotes(quotes, membership, symbols, days, state)
    state[0]["can_trade"] = None
    state.pop()
    with pytest.raises(ValueError, match="state has missing grid rows"):
        gaps.diagnose_quotes(quotes, membership, symbols, days, state)


def test_build_records_failed_source_change_and_retains_outputs(integrated, tmp_path, monkeypatch):
    source = {
        "commit": "synthetic",
        "files_sha256": {
            integrated[name].name: digest(integrated[name]) for name in ("reconciliation", "events")
        },
    }
    calls = iter([source, {**source, "commit": "changed"}])
    monkeypatch.setattr(gaps, "execution_identity", lambda _: next(calls))
    output = tmp_path / "failed-result"
    with pytest.raises(ValueError, match="source changed"):
        gaps.build(project_root=tmp_path, output=output, **integrated)
    assert read_json(output / "manifest.json")["status"] == "failed"
    assert (output / "membership.parquet").is_file()
    assert (output / "gaps.parquet").is_file()
