"""Continuation exclusion, provenance and resume checks, with synthetic offline archives."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from ashare_lab.data.queries import BAR_FIELDS
from ashare_lab.data.raw import digest, save_json
from ashare_lab.fallback import collect as collector
from ashare_lab.fallback.protocol import request_identity
from test_fallback import successful_worker


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def complete_capture(directory, request):
    directory.mkdir(parents=True)
    save_json(directory / "request.json", request)
    successful_worker([str(directory / "request.json"), str(directory)])
    return {
        "query_id": request_identity(request)["query_id"],
        "status": "complete",
        "worker_exit": 0,
        "metadata_path": str(directory / "metadata.json"),
        "metadata_sha256": digest(directory / "metadata.json"),
        "response_path": str(directory / "response.bin"),
        "response_sha256": digest(directory / "response.bin"),
    }


@pytest.fixture
def origin(tmp_path):
    directory = tmp_path / "original"
    directory.mkdir()
    plan = {
        "schema_version": "source-plan-v1",
        "source": "baostock",
        "sdk": "0.9.4",
        "queries": [
            {
                "api": "query_history_k_data_plus",
                "parameters": {
                    "code": f"sz.{number:06d}",
                    "fields": BAR_FIELDS,
                    "frequency": "d",
                    "adjustflag": "3",
                    "start_date": "2023-01-01",
                    "end_date": "2023-12-31",
                },
            }
            for number in range(100, 200)
        ],
    }
    plan_path, run_path = directory / "plan.json", directory / "run.json"
    save_json(plan_path, plan)
    save_json(
        run_path,
        {
            "schema_version": "source-run-v1",
            "plan": plan,
            "plan_sha256": digest(plan_path),
            "status": "incomplete",
            "queries": [],
        },
    )
    pilot = tmp_path / "pilot"
    pilot.mkdir()
    pilot_plan = collector.plan_missing(run_path, plan_path)
    pilot_plan_path = pilot / "plan.original.json"
    save_json(pilot_plan_path, pilot_plan)
    attempts = [
        complete_capture(pilot / str(i), request)
        for i, request in enumerate(pilot_plan["requests"])
    ]
    pilot_run_path = pilot / "run.json"
    save_json(
        pilot_run_path,
        {
            "schema_version": "public-http-run-v1",
            "plan": pilot_plan,
            "plan_sha256": digest(pilot_plan_path),
            "status": "complete",
            "attempts": attempts,
        },
    )
    return run_path, plan_path, pilot_run_path, pilot_plan_path


def test_remaining_90_are_exact_and_old_pilot_limit_is_preserved(origin):
    plan = collector.plan_continuation(*origin)
    collector.verify_plan(plan)
    assert plan["remaining_codes"] == [f"sz.{n:06d}" for n in range(110, 200)]
    assert len(plan["requests"]) == 90 and len(plan["excluded_pilot_query_ids"]) == 10
    assert not (
        {request_identity(r)["query_id"] for r in plan["requests"]}
        & set(plan["excluded_pilot_query_ids"])
    )
    with pytest.raises(ValueError, match="at most ten"):
        collector.plan_missing(origin[0], origin[1], limit=90)


@pytest.mark.parametrize("field", ["origin_run", "origin_plan", "pilot_run", "pilot_plan"])
def test_bound_file_byte_changes_are_rejected(origin, field):
    plan = collector.plan_continuation(*origin)
    path = Path(plan[field]["path"])
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="digest"):
        collector.verify_plan(plan)


@pytest.mark.parametrize(
    "change", ["drop", "reorder", "exclude", "provider", "year", "adjusted", "max"]
)
def test_edited_selection_source_and_range_cannot_bypass_continuation(origin, change):
    plan = collector.plan_continuation(*origin)
    if change == "drop":
        plan["requests"].pop()
    elif change == "reorder":
        plan["requests"].reverse()
    elif change == "exclude":
        plan["excluded_pilot_query_ids"].pop()
    elif change == "provider":
        plan["provider"] = "tencent"
    elif change == "year":
        plan["requests"][0]["parameters"]["end"] = "20251231"
    elif change == "adjusted":
        plan["requests"][0]["parameters"]["fqt"] = "1"
    else:
        plan["max_securities"] = 91
    with pytest.raises(ValueError, match="remaining-90"):
        collector.verify_plan(plan)


@pytest.mark.parametrize(
    "change",
    [
        "status",
        "missing_attempt",
        "failed_attempt",
        "worker_exit",
        "raw_failure",
        "raw_identity",
        "schema",
        "embedded_plan",
        "retained_plan",
    ],
)
def test_pilot_exclusion_needs_ten_authoritative_complete_captures(origin, change):
    _, _, run_path, plan_path = origin
    run = read(run_path)
    if change == "status":
        run["status"] = "running"
    elif change == "missing_attempt":
        run["attempts"].pop()
    elif change == "failed_attempt":
        run["attempts"][0]["status"] = "transport_error"
    elif change == "worker_exit":
        run["attempts"][0]["worker_exit"] = 2
    elif change in {"raw_failure", "raw_identity"}:
        entry = run["attempts"][0]
        metadata = read(entry["metadata_path"])
        if change == "raw_failure":
            metadata["classification"] = "running"
        else:
            metadata["parameters"]["secid"] = "0.000199"
        save_json(entry["metadata_path"], metadata)
        entry["metadata_sha256"] = digest(entry["metadata_path"])
    elif change == "schema":
        run["schema_version"] = "public-http-run-v2"
        run["plan"]["schema_version"] = "fallback-continuation-plan-v1"
    elif change == "embedded_plan":
        run["plan"]["requests"].pop()
    else:
        plan = read(plan_path)
        plan["requests"].pop()
        save_json(plan_path, plan)
        run["plan"] = plan
        run["plan_sha256"] = digest(plan_path)
    save_json(run_path, run)
    with pytest.raises(ValueError):
        collector.plan_continuation(*origin)


def test_cross_origin_pilot_cannot_bind_to_another_baostock_plan(origin, tmp_path):
    path = tmp_path / "other-original-plan.json"
    path.write_bytes(origin[1].read_bytes())
    with pytest.raises(ValueError, match="original missing 100"):
        collector.plan_continuation(origin[0], path, origin[2], origin[3])


def test_continuation_resume_reuses_verified_capture_and_never_repeats_pilot(
    origin, tmp_path, monkeypatch
):
    plan = collector.plan_continuation(*origin)
    plan_path = tmp_path / "continuation-plan.json"
    save_json(plan_path, plan)
    output = tmp_path / "continuation"
    output.mkdir()
    save_json(output / "plan.original.json", plan)
    first = complete_capture(output / "retained", plan["requests"][0])
    save_json(
        output / "run.json",
        {
            "schema_version": "public-http-run-v2",
            "plan": plan,
            "plan_sha256": digest(plan_path),
            "status": "running",
            "attempts": [first],
            "executions": [],
        },
    )
    first_bytes = Path(first["metadata_path"]).read_bytes()
    calls = []
    monkeypatch.setattr(collector, "access_directory", lambda p: tmp_path / "unused-baostock-state")
    monkeypatch.setattr(collector, "code_identity", lambda p: {"commit": "offline-test"})
    monkeypatch.setattr(collector.time, "sleep", lambda t: None)

    def worker(command, **kwargs):
        calls.append(request_identity(read(command[-2]))["query_id"])
        return successful_worker(command, **kwargs)

    monkeypatch.setattr(collector.subprocess, "run", worker)
    result = collector.collect(plan_path, output, project_root=tmp_path)
    assert result["status"] == "complete" and result["schema_version"] == "public-http-run-v2"
    assert len(calls) == 89 and len(set(calls)) == 89
    assert first["query_id"] not in calls
    assert not set(calls) & set(plan["excluded_pilot_query_ids"])
    assert len(collector.load_collection(output / "run.json")[0]) == 90
    assert Path(first["metadata_path"]).read_bytes() == first_bytes
    collector.collect(plan_path, output, project_root=tmp_path)
    assert len(calls) == 89
    assert not (tmp_path / "unused-baostock-state").exists()
    # A reduced embedded denominator cannot disguise one missing request.
    result = deepcopy(result)
    result["plan"]["requests"].pop()
    save_json(output / "run.json", result)
    with pytest.raises(ValueError, match="embedded"):
        collector.load_collection(output / "run.json")
