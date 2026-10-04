"""Search coverage comes from retained bytes, not completion flags or titles."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from ashare_lab.dividend_evidence import DividendEvidenceError, audit_search_run
from ashare_lab.dividend_evidence.cli import main
from ashare_lab.dividend_evidence.search import SEARCH_URL, _payload


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_bytes())


def ref(root, path):
    raw = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def row(number, symbol="000001.SZ"):
    return {
        "secCode": symbol[:6],
        "secName": "Synthetic issuer",
        "orgId": "org" + symbol[:6],
        "announcementId": str(1000000 + number),
        "announcementTitle": "2022年度权益分派实施公告",
        "announcementTime": 1686153600000,
        "adjunctUrl": f"finalpage/2023-06-08/{1000000 + number}.PDF",
        "adjunctType": "PDF",
        "adjunctSize": 10,
        "additional_source_field": {"retained": True},
    }


def make_bundle(tmp_path):
    root, folder = tmp_path, tmp_path / "raw/batch"
    all_symbols = [f"{number:06}.SZ" for number in range(1, 337)]
    groups = {
        "original_plan_scope": {
            "symbols": 336,
            "missing_query_symbols": all_symbols[:100],
            "successful_empty_query_symbols": all_symbols[100:128],
            "successful_nonempty_query_symbols": all_symbols[128:],
        },
        "conditional_union_diagnostic_only": {
            "symbols": 323,
            "missing_query_symbols": all_symbols[:95],
            "successful_empty_query_symbols": all_symbols[100:125],
            "successful_nonempty_query_symbols": all_symbols[128:331],
        },
    }
    previous = root / "readiness.json"
    write(
        previous,
        {
            "schema_version": "retained-dividend-readiness-v1",
            "research_eligible": False,
            "formal_action_coverage_certified": False,
            **groups,
        },
    )
    mapping = root / "map.json"
    write(
        mapping, {"stockList": [{"code": s[:6], "orgId": "org" + s[:6]} for s in all_symbols[:95]]}
    )
    script = root / "collector.py"
    script.write_text("# Synthetic capture fixture, never executed.\n", encoding="utf-8")
    plan_path, run_path = folder / "plan.json", folder / "run.json"
    write(
        plan_path,
        {
            "schema_version": "cninfo-dividend-search-plan-v1",
            "research_eligible": False,
            "prior_readiness": ref(root, previous),
            "routing_map": ref(root, mapping),
            "script": ref(root, script),
            "symbols": all_symbols[:95],
            "year": 2023,
            "keyword": "权益分派",
            "category": "",
            "page_size": 30,
            "max_pages_per_symbol": 2,
            "max_requests": 190,
        },
    )
    write(
        run_path,
        {
            "schema_version": "cninfo-dividend-search-run-v1",
            "research_eligible": False,
            "status": "partial_selected_queries",
            "plan": ref(root, plan_path),
            "requests": 0,
            "retries": 0,
            "started_at_utc": "2026-10-04T00:00:00+00:00",
            "finished_at_utc": "2026-10-04T01:00:00+00:00",
            "entries": [],
        },
    )
    return root, plan_path, run_path


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(tmp_path)


def append_query(bundle, pages):
    root, _, run_path = bundle
    run = read(run_path)
    symbol = f"{len(run['entries']) + 1:06}.SZ"
    receipts, combined = [], []
    total = sum(len(rows) for rows in pages)
    for number, rows in enumerate(pages, 1):
        raw, meta = (
            run_path.parent / f"{symbol[:6]}-page{number}.json",
            run_path.parent / f"{symbol[:6]}-page{number}.metadata.json",
        )
        write(
            raw,
            {
                "totalAnnouncement": total,
                "totalRecordNum": total,
                "hasMore": number < len(pages),
                "totalpages": 0,
                "announcements": rows,
            },
        )
        start = datetime(2026, 10, 4, tzinfo=timezone.utc) + timedelta(seconds=run["requests"] * 2)
        write(
            meta,
            {
                "url": SEARCH_URL,
                "method": "POST",
                "payload": _payload(symbol, "org" + symbol[:6], number),
                "complete": True,
                "transfer_complete": True,
                "retries": 0,
                "status": 200,
                "final_url": SEARCH_URL,
                "content_type": "application/json;charset=UTF-8",
                "content_length": str(raw.stat().st_size),
                "started_at_utc": start.isoformat(),
                "completed_at_utc": (start + timedelta(seconds=1)).isoformat(),
                **ref(root, raw),
            },
        )
        receipts.append({"page": number, "raw": ref(root, raw), "metadata": ref(root, meta)})
        combined.extend(rows)
        run["requests"] += 1
    run["entries"].append(
        {
            "symbol": symbol,
            "expected_total": total,
            "rows": combined,
            "query_complete": True,
            "receipts": receipts,
        }
    )
    write(run_path, run)


def audit(bundle):
    root, plan, run = bundle
    return audit_search_run(plan, run, root)


def refresh(bundle, *, page=1, update_rows=True):
    root, _, run_path = bundle
    run = read(run_path)
    receipt = run["entries"][0]["receipts"][page - 1]
    raw, meta = root / receipt["raw"]["path"], root / receipt["metadata"]["path"]
    metadata = read(meta)
    metadata.update(ref(root, raw))
    metadata["content_length"] = str(raw.stat().st_size)
    write(meta, metadata)
    receipt.update(raw=ref(root, raw), metadata=ref(root, meta))
    if update_rows:
        run["entries"][0]["rows"] = [
            item
            for r in run["entries"][0]["receipts"]
            for item in read(root / r["raw"]["path"])["announcements"] or []
        ]
    write(run_path, run)


def test_complete_zero_and_nonempty_results_keep_exact_rows_and_limitations(bundle):
    append_query(bundle, [[row(1)]])
    for _ in range(94):
        append_query(bundle, [[]])
    run = read(bundle[2])
    run["status"] = "complete_selected_queries"
    write(bundle[2], run)
    result = audit(bundle)
    assert result["selected_queries_complete"] and result["counts"]["complete_queries"] == 95
    assert result["counts"]["nonempty_queries"] == 1 and result["counts"]["zero_queries"] == 94
    assert result["queries"][0]["announcements"][0]["record"] == row(1)
    assert result["queries"][0]["pages"][0]["reported_totalpages"] == 0
    assert result["research_eligible"] is False and result["available_time"] is None
    assert result["formal_action_coverage_certified"] is False and result["network_requests"] == 0
    assert Path(result["module_file"]).parent.name == "dividend_evidence"


def test_two_pages_close_by_distinct_ids_counts_and_has_more_not_totalpages(bundle):
    append_query(bundle, [[row(i) for i in range(30)], [row(30)]])
    result = audit(bundle)
    assert result["queries"][0]["status"] == "complete_nonempty"
    assert result["counts"]["validated_rows"] == 31
    assert result["counts"]["not_requested_queries"] == 94
    assert result["counts"]["zero_queries"] == 0 and not result["selected_queries_complete"]


@pytest.mark.parametrize(
    "case",
    [
        "issuer",
        "org",
        "future",
        "timestamp_bool",
        "id",
        "title",
        "url_id",
        "url_year",
        "adjunct_bool",
        "duplicate",
        "total_bool",
        "more_int",
        "bad_closure",
        "null_rows",
        "total_changed",
    ],
)
def test_self_consistent_run_cannot_hide_invalid_source_rows_or_pagination(bundle, case):
    append_query(bundle, [[row(1)]])
    raw = bundle[2].parent / "000001-page1.json"
    data = read(raw)
    changes = {
        "issuer": ("secCode", "000002"),
        "org": ("orgId", "different"),
        "future": ("announcementTime", 1704038400000),
        "timestamp_bool": ("announcementTime", True),
        "id": ("announcementId", 1000001),
        "title": ("announcementTitle", " "),
        "url_id": ("adjunctUrl", "finalpage/2023-06-08/999.PDF"),
        "url_year": ("adjunctUrl", "finalpage/2024-06-08/1000001.PDF"),
        "adjunct_bool": ("adjunctSize", True),
    }
    if case in changes:
        key, value = changes[case]
        data["announcements"][0][key] = value
    elif case == "duplicate":
        data["announcements"] *= 2
        data["totalAnnouncement"] = data["totalRecordNum"] = 2
    elif case == "total_bool":
        data["totalAnnouncement"] = True
    elif case == "more_int":
        data["hasMore"] = 0
    elif case == "bad_closure":
        data["hasMore"] = True
    elif case == "null_rows":
        data["announcements"] = None
    else:
        data["totalAnnouncement"] = 2
    write(raw, data)
    refresh(bundle)
    result = audit(bundle)
    assert result["status"] == "invalid_evidence" and result["counts"]["invalid_queries"] == 1
    assert not result["queries"][0]["query_complete"]


@pytest.mark.parametrize(
    "change",
    [
        {"final_url": "https://other.example/query"},
        {"url": SEARCH_URL + "?other=1"},
        {"content_type": "text/html"},
        {"content_type": "application/not-json"},
        {"complete": 1},
        {"status": True},
        {"transfer_complete": False},
        {"retries": 1},
        {"error": {}},
        {"exception": "failed"},
        {"failure": {"why": "challenge"}},
        {"content_length": "1"},
        {"content_length": True},
        {"started_at_utc": "2026-10-04T01:01:00+00:00"},
    ],
)
def test_metadata_failure_and_transfer_identity_are_independent_of_run_complete(bundle, change):
    append_query(bundle, [[row(1)]])
    root, _, run_path = bundle
    run = read(run_path)
    receipt = run["entries"][0]["receipts"][0]
    meta = root / receipt["metadata"]["path"]
    value = read(meta)
    value.update(change)
    write(meta, value)
    receipt["metadata"] = ref(root, meta)
    write(run_path, run)
    assert audit(bundle)["queries"][0]["status"] == "invalid"


def test_changed_fixed_payload_and_metadata_digest_fail(bundle):
    append_query(bundle, [[row(1)]])
    root, _, run_path = bundle
    run = read(run_path)
    receipt = run["entries"][0]["receipts"][0]
    meta = root / receipt["metadata"]["path"]
    value = read(meta)
    value["payload"]["seDate"] = "2024-01-01~2024-12-31"
    write(meta, value)
    assert "digest" in audit(bundle)["queries"][0]["issues"][0]
    receipt["metadata"] = ref(root, meta)
    write(run_path, run)
    assert "payload" in audit(bundle)["queries"][0]["issues"][0]


def test_run_row_omission_and_bool_numeric_equality_cannot_hide_source_difference(bundle):
    append_query(bundle, [[row(1)]])
    run = read(bundle[2])
    run["entries"][0]["rows"][0]["adjunctSize"] = 10.0
    write(bundle[2], run)
    assert "exact raw" in audit(bundle)["queries"][0]["issues"][0]


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'])
def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(bundle, raw):
    append_query(bundle, [[row(1)]])
    path = bundle[2].parent / "000001-page1.json"
    path.write_text(raw, encoding="utf-8")
    refresh(bundle, update_rows=False)
    assert audit(bundle)["queries"][0]["status"] == "invalid"


def test_failed_unindexed_capture_is_not_an_empty_success_or_unrequested(bundle):
    append_query(bundle, [[row(1)]])
    root, _, run_path = bundle
    run = read(run_path)
    run["entries"] = []
    run["status"] = "failed"
    run["failure"] = {"message": "HTTP 403"}
    raw, meta = (
        run_path.parent / "000001-page1.json",
        run_path.parent / "000001-page1.metadata.json",
    )
    raw.write_bytes(b"<html>blocked</html>")
    value = read(meta)
    value.update(
        complete=False,
        transfer_complete=False,
        status=403,
        error={"message": "HTTP 403"},
        **ref(root, raw),
    )
    write(meta, value)
    write(run_path, run)
    result = audit(bundle)
    assert (
        result["status"] == "partial_selected_queries" and result["counts"]["failed_queries"] == 1
    )
    assert result["counts"]["zero_queries"] == 0 and result["counts"]["not_requested_queries"] == 94
    assert result["queries"][0]["pages"][0]["binding"] == "unindexed_capture"


def test_interrupted_request_without_any_file_is_counted_as_incomplete(bundle):
    run = read(bundle[2])
    run.update(status="running", requests=1)
    run.pop("finished_at_utc")
    write(bundle[2], run)
    result = audit(bundle)
    assert (
        result["counts"]["incomplete_queries"] == 1
        and result["counts"]["not_requested_queries"] == 94
    )


def test_successful_orphan_capture_is_not_promoted_without_run_receipt(bundle):
    append_query(bundle, [[row(1)]])
    run = read(bundle[2])
    run.update(entries=[], status="failed", failure={"message": "crash after response"})
    write(bundle[2], run)
    result = audit(bundle)
    assert (
        result["queries"][0]["status"] == "incomplete" and not result["selected_queries_complete"]
    )
    assert result["counts"]["zero_queries"] == 0


@pytest.mark.parametrize(
    "case",
    ["scope", "plan_hash", "requests_bool", "route_duplicate", "path_escape", "plan_nonfinite"],
)
def test_invalid_authority_inputs_raise_before_claiming_coverage(bundle, case):
    root, plan_path, run_path = bundle
    plan, run = read(plan_path), read(run_path)
    if case == "scope":
        plan["symbols"][-1] = "000096.SZ"
    elif case == "plan_hash":
        run["plan"]["sha256"] = "0" * 64
    elif case == "requests_bool":
        run["requests"] = False
    elif case == "route_duplicate":
        path = root / "map.json"
        value = read(path)
        value["stockList"].append(value["stockList"][0])
        write(path, value)
        plan["routing_map"] = ref(root, path)
    elif case == "path_escape":
        plan["routing_map"]["path"] = "../map.json"
    else:
        plan_path.write_text('{"x":NaN}', encoding="utf-8")
        with pytest.raises(DividendEvidenceError):
            audit(bundle)
        return
    if case != "plan_hash":
        write(plan_path, plan)
        run["plan"] = ref(root, plan_path)
    write(run_path, run)
    with pytest.raises(DividendEvidenceError):
        audit(bundle)


def test_package_cli_preserves_immutable_report_and_nonpassing_status(bundle, tmp_path, capsys):
    output = tmp_path / "diagnostics/audit.json"
    arguments = [
        "audit-search",
        "--plan",
        str(bundle[1]),
        "--manifest",
        str(bundle[2]),
        "--evidence-root",
        str(bundle[0]),
        "--report",
        str(output),
    ]
    assert main(arguments) == 2
    assert read(output)["counts"]["not_requested_queries"] == 95
    original = output.read_bytes()
    with pytest.raises(SystemExit):
        main(arguments)
    assert output.read_bytes() == original
    capsys.readouterr()


@pytest.mark.parametrize("key", ["error", "exception", "failure"])
def test_complete_query_entry_cannot_contain_a_retained_failure(bundle, key):
    append_query(bundle, [[row(1)]])
    run = read(bundle[2])
    run["entries"][0][key] = {"message": "source failed"}
    write(bundle[2], run)
    result = audit(bundle)
    assert result["queries"][0]["status"] == "invalid"
    assert "retained failure" in result["queries"][0]["issues"][0]


def test_plan_failure_is_not_overridden_by_its_recomputed_digest(bundle):
    root, plan_path, run_path = bundle
    plan = read(plan_path)
    plan["failure"] = "incomplete scope"
    write(plan_path, plan)
    run = read(run_path)
    run["plan"] = ref(root, plan_path)
    write(run_path, run)
    with pytest.raises(DividendEvidenceError, match="plan.*failure"):
        audit(bundle)


@pytest.mark.parametrize("case", ["total_changed", "duplicate_id", "page_gap"])
def test_real_multi_page_failures_are_not_replaced_by_complete_flag(bundle, case):
    append_query(bundle, [[row(i) for i in range(30)], [row(30)]])
    raw = bundle[2].parent / "000001-page2.json"
    value = read(raw)
    if case == "total_changed":
        value["totalAnnouncement"] = value["totalRecordNum"] = 32
    elif case == "duplicate_id":
        value["announcements"][0] = row(0)
    else:
        run = read(bundle[2])
        run["entries"][0]["receipts"][1]["page"] = 3
        write(bundle[2], run)
    if case != "page_gap":
        write(raw, value)
        refresh(bundle, page=2)
    assert audit(bundle)["queries"][0]["status"] == "invalid"


def test_exhausted_two_page_budget_keeps_query_incomplete(bundle):
    append_query(bundle, [[row(i) for i in range(30)], [row(i) for i in range(30, 60)]])
    for number in (1, 2):
        raw = bundle[2].parent / f"000001-page{number}.json"
        data = read(raw)
        data.update(totalAnnouncement=61, totalRecordNum=61, hasMore=True)
        write(raw, data)
        refresh(bundle, page=number)
    run = read(bundle[2])
    run["entries"][0].update(expected_total=61, query_complete=False)
    write(bundle[2], run)
    result = audit(bundle)
    assert (
        result["queries"][0]["status"] == "incomplete" and result["counts"]["validated_rows"] == 60
    )
    assert result["counts"]["zero_queries"] == 0 and not result["selected_queries_complete"]


def test_requests_must_be_serial_and_spaced_one_second(bundle):
    append_query(bundle, [[row(1)]])
    append_query(bundle, [[]])
    root, _, run_path = bundle
    run = read(run_path)
    receipt = run["entries"][1]["receipts"][0]
    meta = root / receipt["metadata"]["path"]
    value = read(meta)
    value["started_at_utc"] = "2026-10-04T00:00:01.5+00:00"
    write(meta, value)
    receipt["metadata"] = ref(root, meta)
    write(run_path, run)
    result = audit(bundle)
    assert result["status"] == "invalid_evidence" and any(
        "one-second" in v for v in result["issues"]
    )
