"""Offline regressions for raw promotion and plan-truncation review findings."""

from copy import deepcopy
import json

import pytest

from ashare_lab.data.collect import collect
from ashare_lab.data.pipeline import load_run
from ashare_lab.data.queries import query_id, validate_query
from ashare_lab.data.raw import archive_attempt, digest, save_json
from ashare_lab.data.raw import cached_records, raw_table, read_reference
from test_data_pipeline import bar


def captured_run(tmp_path, monkeypatch):
    monkeypatch.setattr("ashare_lab.data.collect.code_identity", lambda p: {"commit": "test"})
    response = bar()
    archive_attempt(response, tmp_path / "raw/attempts/query/original")
    plan = tmp_path / "plan.json"
    save_json(plan, {"queries": [validate_query(response)]})
    collect(
        plan,
        tmp_path / "run",
        [tmp_path / "raw"],
        project_root=tmp_path,
        access_state=tmp_path / "access",
        offline=True,
    )
    return tmp_path / "run/run.json", plan, response


@pytest.mark.parametrize(
    "change",
    [
        {"status": "provider_error", "error_code": "10001011"},
        {"status": "partial"},
        {"status": "transport_error", "exception": "pagination dropped"},
        {"error_code": "10001011"},
        {"error_code": None},
        {"source": "other-provider"},
        {"sdk": "0.9.3"},
    ],
)
@pytest.mark.parametrize("consumer", ["load", "resume"])
def test_successful_entry_cannot_promote_unqualified_raw(tmp_path, monkeypatch, change, consumer):
    path, plan, response = captured_run(tmp_path, monkeypatch)
    response.update(change)
    raw = archive_attempt(response, tmp_path / "contradictory-attempt")
    run = json.loads(path.read_text())
    run["queries"][0].update(
        status="complete",
        error_code="0",
        raw_locator={"path": str(raw), "sha256": digest(raw)},
    )
    save_json(path, run)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="raw"):
        if consumer == "load":
            load_run(path)
        else:
            collect(
                plan,
                path.parent,
                [],
                project_root=tmp_path,
                access_state=tmp_path / "access",
                offline=True,
            )
    assert path.read_bytes() == before


def test_embedded_plan_cannot_remove_incomplete_queries(tmp_path, monkeypatch):
    path, plan, response = captured_run(tmp_path, monkeypatch)
    value = json.loads(plan.read_text())
    value["queries"].append({"api": "query_stock_basic", "parameters": {"code": "sh.600000"}})
    expanded = tmp_path / "expanded.json"
    save_json(expanded, value)
    collect(
        expanded,
        tmp_path / "partial",
        [tmp_path / "raw"],
        project_root=tmp_path,
        access_state=tmp_path / "access",
        offline=True,
    )
    path = tmp_path / "partial/run.json"
    run = json.loads(path.read_text())
    run["plan"]["queries"] = [validate_query(response)]
    run["queries"] = [e for e in run["queries"] if e["query_id"] == query_id(response)]
    save_json(path, run)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="plan"):
        load_run(path)
    with pytest.raises(ValueError, match="plan"):
        collect(
            expanded,
            path.parent,
            [],
            project_root=tmp_path,
            access_state=tmp_path / "access",
            offline=True,
        )
    assert path.read_bytes() == before


def test_v1_requires_original_plan_then_checks_embedded_content(tmp_path, monkeypatch):
    path, plan, _ = captured_run(tmp_path, monkeypatch)
    run = json.loads(path.read_text())
    run["schema_version"] = "source-run-v1"
    run.pop("plan_binding", None)
    save_json(path, run)
    with pytest.raises(ValueError, match="original plan"):
        load_run(path)
    records, complete, _ = load_run(path, original_plans=[plan])
    assert complete and len(records) == 1
    changed = deepcopy(run)
    changed["plan"]["queries"] = []
    changed["queries"] = []
    save_json(path, changed)
    with pytest.raises(ValueError, match="plan"):
        load_run(path, original_plans=[plan])


@pytest.mark.parametrize("change", [{"source": "other"}, {"sdk": "other"}, {"error_code": "9"}])
def test_successful_raw_cannot_hide_conflicting_entry_identity(tmp_path, monkeypatch, change):
    path, _, _ = captured_run(tmp_path, monkeypatch)
    run = json.loads(path.read_text())
    run["queries"][0].update(change)
    save_json(path, run)
    with pytest.raises(ValueError, match="authoritative raw"):
        load_run(path)


def test_new_run_retains_original_plan_bytes_and_rejects_snapshot_damage(tmp_path, monkeypatch):
    path, plan, _ = captured_run(tmp_path, monkeypatch)
    run = json.loads(path.read_text())
    assert run["schema_version"] == "source-run-v2"
    snapshot = path.parent / run["plan_binding"]["file"]
    assert snapshot.read_bytes() == plan.read_bytes()
    plan.unlink()
    records, complete, _ = load_run(path)
    assert complete and len(records) == 1
    snapshot.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="plan.*digest"):
        load_run(path)


def test_v1_plan_bytes_must_match_even_if_json_semantically_equal(tmp_path, monkeypatch):
    path, plan, _ = captured_run(tmp_path, monkeypatch)
    run = json.loads(path.read_text())
    run["schema_version"] = "source-run-v1"
    run.pop("plan_binding")
    save_json(path, run)
    plan.write_text(json.dumps(run["plan"], separators=(",", ":")), encoding="utf-8")
    with pytest.raises(ValueError, match="original plan"):
        load_run(path, original_plans=[plan])


def test_successful_empty_response_is_valid_and_failure_history_is_not_promoted(
    tmp_path, monkeypatch
):
    path, plan, _ = captured_run(tmp_path, monkeypatch)
    run = json.loads(path.read_text())
    empty = bar()
    empty["rows"] = []
    raw = archive_attempt(empty, tmp_path / "empty")
    entry = run["queries"][0]
    entry.update(row_count=0, raw_locator={"path": str(raw), "sha256": digest(raw)})
    run["queries"].insert(0, {**deepcopy(entry), "status": "provider_error", "error_code": "9"})
    save_json(path, run)
    records, complete, _ = load_run(path)
    assert complete and records[0]["rows"] == []
    resumed = collect(
        plan, path.parent, [], project_root=tmp_path, access_state=tmp_path / "access", offline=True
    )
    assert resumed["status"] == "complete" and len(resumed["queries"]) == 2


def legacy_capture(tmp_path):
    import pyarrow.parquet as pq

    query = {
        k: v for k, v in bar().items() if k in {"api", "parameters", "rows", "fields", "error_code"}
    }
    query["name"] = "bars"
    directory = tmp_path / "legacy"
    directory.mkdir()
    path = directory / "capture.json"
    payload = {
        "source": "baostock",
        "sdk": "0.9.4",
        "retrieved_at_utc": "2026-10-03T03:00:00+00:00",
        "queries": [query],
    }
    save_json(path, payload)
    pq.write_table(raw_table(query), directory / "rows.parquet")
    manifest = {
        "schema_version": "raw-source-probe-v1",
        "source": "baostock",
        "captures": [{"file": path.name, "sha256": digest(path)}],
        "queries": [
            {
                "name": "bars",
                "capture": path.name,
                "parameters": query["parameters"],
                "rows": 1,
                "status": "complete_raw_read_verified",
                "error_code": "0",
                "file": "rows.parquet",
                "sha256": digest(directory / "rows.parquet"),
            }
        ],
    }
    save_json(directory / "manifest.json", manifest)
    return path, payload, manifest


@pytest.mark.parametrize(
    "mutation", ["failed_query", "missing_sdk", "wrong_source", "partial_query"]
)
def test_legacy_manifest_cannot_override_failed_or_unidentified_capture(tmp_path, mutation):
    path, payload, manifest = legacy_capture(tmp_path)
    if mutation == "failed_query":
        payload["queries"][0]["error_code"] = "10001011"
    elif mutation == "partial_query":
        payload["queries"][0]["status"] = "partial"
    elif mutation == "missing_sdk":
        del payload["sdk"]
    else:
        payload["source"] = "other"
    save_json(path, payload)
    manifest["captures"][0]["sha256"] = digest(path)
    save_json(path.parent / "manifest.json", manifest)
    reference = {"path": str(path), "sha256": digest(path), "query_name": "bars"}
    with pytest.raises(ValueError, match="legacy raw"):
        read_reference(reference)
    with pytest.raises(ValueError, match="legacy raw"):
        cached_records([tmp_path])


def test_valid_legacy_capture_has_explicit_archive_basis(tmp_path):
    path, _, _ = legacy_capture(tmp_path)
    record = read_reference({"path": str(path), "sha256": digest(path), "query_name": "bars"})
    assert record["status"] == "complete" and record["sdk_basis"] == "capture"
    assert record["transport_integrity"] == "legacy_not_instrumented"
    assert len(cached_records([tmp_path])) == 1
