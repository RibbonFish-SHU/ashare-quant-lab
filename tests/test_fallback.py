"""Provider behavior and negative cases; all network is replaced in tests."""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ashare_lab.data.raw import digest, save_json
from ashare_lab.fallback import collect as collector
from ashare_lab.fallback.pipeline import audit, convert, load_capture
from ashare_lab.fallback.protocol import (
    SourceError,
    decode_response,
    eastmoney_request,
    request_identity,
    request_url,
)


def east_row(day="2023-07-18"):
    return [
        day,
        "11.29",
        "11.25",
        "11.32",
        "11.22",
        "471966",
        "531299772.68",
        "0.88",
        "-0.53",
        "-0.06",
        "0.24",
    ]


def east_body(rows=None, **changes):
    doc = {
        "rc": 0,
        "data": {
            "code": "000001",
            "market": 0,
            "klines": [",".join(r) for r in ([east_row()] if rows is None else rows)],
        },
    }
    doc.update(changes)
    return json.dumps(doc).encode()


def east_request():
    return eastmoney_request("sz.000001", "2023-07-18", "2023-07-24")


def record(body=None, request=None):
    request = request or east_request()
    identity, rows = decode_response(body or east_body(), request)
    return {
        **identity,
        "rows": rows,
        "raw_path": "fixture",
        "raw_sha256": "a" * 64,
        "observed_at_utc": datetime(2026, 10, 3, tzinfo=timezone.utc),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"beg": "20240101"},
        {"end": "20250101"},
        {"fqt": "1"},
        {"klt": "5"},
        {"secid": "1.000300"},
        {"end": "20220101"},
        {"lmt": "10"},
    ],
)
def test_requests_cannot_expand_range_or_use_adjusted_index_data(change):
    request = east_request()
    request["parameters"].update(change)
    with pytest.raises(ValueError):
        request_identity(request)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>captcha</html>",
        b'{"rc":0,"data":null}',
        b'{"rc":0,"rc":7,"data":{}}',
        b'{"rc":0,"data":',
        b'{"rc":102,"data":null}',
        b'{"rc":NaN,"data":null}',
    ],
)
def test_failed_partial_or_unidentified_response_is_not_success(body):
    with pytest.raises(SourceError):
        decode_response(body, east_request())


def test_identified_empty_array_is_distinct_from_error_and_null_data():
    value = record(east_body([]))
    assert value["rows"] == []
    rows, issues = convert(value)
    result = audit([value], rows, issues, {"calendar": [], "securities": [], "bars": []})
    check = next(c for c in result["checks"] if c["name"] == "successful_empty_responses")
    assert check["count"] == 1 and check["status"] == "unverified"


def tencent(code, rows):
    request = {
        "provider": "tencent",
        "parameters": {
            "_var": "kline_day2023",
            "param": code + ",day,2023-07-18,2023-07-24,10,",
            "r": "0.8205512681390605",
        },
    }
    body = (
        "kline_day2023="
        + json.dumps(
            {
                "code": 0,
                "data": {
                    code: {
                        "day": rows,
                        "qt": {"current_quote": "ignored"},
                        "name": "current name ignored",
                    }
                },
            }
        )
    ).encode()
    return request, body


@pytest.mark.parametrize(
    "code,expected,unit",
    [
        ("sz000001", Decimal("47196600"), "lot_100_shares"),
        ("sh688065", Decimal("471966"), "share"),
        ("sz300308", None, "unknown"),
    ],
)
def test_tencent_unit_basis_is_security_specific(code, expected, unit):
    request, body = tencent(
        code, [east_row()[:5] + ["471966.00", {}, "0.24", "53129.98", "0.00", "0.00"]]
    )
    row = convert(record(body, request))[0][0]
    assert row["volume_scaled_shares"] == expected and row["volume_source_unit"] == unit
    assert row["amount_scaled_cny"] == Decimal("531299800")
    assert row["source_trading_active"] is None and row["source_is_st"] is None
    assert row["preclose"] is None and row["available_time"] is None
    assert "current_quote" not in row["raw_values_json"]


def test_jsonp_is_parsed_without_accepting_trailing_javascript():
    request, body = tencent("sz000001", [])
    assert decode_response(body + b";", request)[1] == []
    with pytest.raises(SourceError):
        decode_response(body + b";dangerous()", request)


def test_displayed_lots_are_not_exact_shares_or_an_assumed_rounding_rule():
    row = convert(record())[0][0]
    assert row["volume_raw"] == "471966" and row["volume_scaled_shares"] == Decimal("47196600")
    assert row["volume_is_exact_shares"] is False
    assert "rounding_rule_unverified" in row["precision_basis"]
    assert json.loads(row["numeric_display_exponents_json"])["amount"] == -2
    assert row["amount_raw"] == "531299772.68"


def test_earlier_tencent_rows_are_retained_but_excluded_from_requested_coverage():
    old = east_row("2023-07-11")[:5] + ["570809.00", {}, "0.29", "64013.18", "0.00", "0.00"]
    request, body = tencent("sz000001", [old])
    value = record(body, request)
    rows, issues = convert(value)
    result = audit([value], rows, issues, {"calendar": [], "securities": [], "bars": []})
    assert result["raw_rows"] == 1 and result["interval_rows"] == 0
    assert rows[0]["record_status"] == "outside_requested_interval"


def test_unexplained_missing_dates_duplicate_and_order_are_visible():
    value = record(east_body([east_row("2023-07-20"), east_row(), east_row()]))
    rows, issues = convert(value)
    result = audit(
        [value],
        rows,
        issues,
        {
            "calendar": [{"event_date": date(2023, 7, d), "is_open": True} for d in (18, 19, 20)],
            "securities": [],
            "bars": [],
        },
    )
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["duplicate_dates"]["count"] == 1 and checks["response_order"]["count"] == 1
    gap = checks["calendar_missing_rows"]["gaps"][0]
    assert gap["dates"] == ["2023-07-19"] and "neither suspension nor truncation" in gap["meaning"]


def test_http_metadata_cannot_promote_an_error_body(tmp_path):
    request = east_request()
    response = tmp_path / "response.bin"
    response.write_bytes(east_body(rc=102))
    meta = {
        **request,
        "status": 200,
        "transfer_complete": True,
        "classification": "complete",
        "url": request_url(request),
        "final_url": request_url(request),
        "captured_at_utc": "2026-10-03T12:00:00+00:00",
        "bytes": response.stat().st_size,
        "sha256": digest(response),
    }
    path = tmp_path / "metadata.json"
    save_json(path, meta)
    with pytest.raises(SourceError):
        load_capture(path, response)


def fake_plan(tmp_path):
    return {
        "schema_version": "fallback-pilot-plan-v1",
        "provider": "eastmoney",
        "max_securities": 2,
        "requests": [eastmoney_request("sz.000001"), eastmoney_request("sz.000002")],
    }


def test_challenge_stops_source_and_persists_without_followup_or_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "verify_plan", lambda p: None)
    monkeypatch.setattr(collector, "access_directory", lambda p: tmp_path / "unused-baostock-path")
    monkeypatch.setattr(collector, "code_identity", lambda p: {"commit": "test"})
    monkeypatch.setattr(collector.time, "sleep", lambda t: None)
    calls = []

    def worker(command, **kwargs):
        calls.append(command)
        directory = Path(command[-1])
        body = directory / "response.bin"
        body.write_bytes(b"<html>captcha</html>")
        save_json(
            directory / "metadata.json",
            {
                "classification": "restricted",
                "status": 200,
                "error": "captcha",
                "bytes": body.stat().st_size,
                "sha256": digest(body),
            },
        )
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(collector.subprocess, "run", worker)
    plan = tmp_path / "plan.json"
    save_json(plan, fake_plan(tmp_path))
    first = collector.collect(plan, tmp_path / "first", project_root=tmp_path)
    assert first["status"] == "stopped_on_error" and len(calls) == 1
    second = collector.collect(plan, tmp_path / "second", project_root=tmp_path)
    assert second["status"] == "stopped_on_source_restriction" and len(calls) == 1
    assert not (tmp_path / "unused-baostock-path").exists()


def test_live_worker_refuses_tencent_even_for_valid_offline_identity(tmp_path):
    request, _ = tencent("sz000001", [])
    with pytest.raises(ValueError, match="offline-only"):
        collector.fetch(request, tmp_path)


def valid_http_archive(tmp_path):
    request = east_request()
    response = tmp_path / "response.bin"
    response.write_bytes(east_body())
    metadata = {
        **request,
        "status": 200,
        "classification": "complete",
        "transfer_complete": True,
        "url": request_url(request),
        "final_url": request_url(request),
        "captured_at_utc": "2026-10-03T12:00:00+00:00",
        "bytes": response.stat().st_size,
        "sha256": digest(response),
    }
    return request, response, metadata


@pytest.mark.parametrize(
    "change",
    [
        {"classification": "running"},
        {"classification": "provider_error"},
        {"classification": None},
        {"error": "captured failure"},
        {"exception": "captured failure"},
        {"transfer_complete": None},
        {"transfer_complete": False},
        {"transfer_complete": 1},
    ],
)
def test_valid_body_cannot_override_failed_or_incomplete_new_metadata(tmp_path, change):
    _, response, metadata = valid_http_archive(tmp_path)
    metadata.update(change)
    if metadata.get("transfer_complete") is None:
        metadata.pop("transfer_complete")
    path = tmp_path / "metadata.json"
    save_json(path, metadata)
    with pytest.raises(ValueError):
        load_capture(path, response)


def test_complete_entry_cannot_promote_failed_http_metadata(tmp_path, monkeypatch):
    request, response, metadata = valid_http_archive(tmp_path)
    metadata["classification"] = "running"
    path = tmp_path / "metadata.json"
    save_json(path, metadata)
    plan = {"requests": [request]}
    save_json(tmp_path / "plan.original.json", plan)
    save_json(
        tmp_path / "run.json",
        {
            "schema_version": "public-http-run-v1",
            "plan": plan,
            "plan_sha256": digest(tmp_path / "plan.original.json"),
            "attempts": [
                {
                    "status": "complete",
                    "query_id": request_identity(request)["query_id"],
                    "metadata_path": str(path),
                    "metadata_sha256": digest(path),
                    "response_path": str(response),
                    "response_sha256": digest(response),
                }
            ],
        },
    )
    monkeypatch.setattr(collector, "verify_plan", lambda p: None)
    with pytest.raises(ValueError):
        collector.load_collection(tmp_path / "run.json")


def test_missing_listing_dates_and_zero_comparisons_cannot_pass():
    value = record()
    rows, issues = convert(value)
    quality = audit(
        [value],
        rows,
        issues,
        {
            "calendar": [],
            "securities": [{"symbol": "000001.SZ", "ipo_date": None, "source_out_date": None}],
            "bars": [],
        },
    )
    checks = {c["name"]: c for c in quality["checks"]}
    assert checks["listing_metadata"]["status"] == "unverified"
    assert checks["cross_source_differences"]["status"] == "unverified"


def test_complete_raw_loads_but_unreviewed_legacy_exception_is_rejected(tmp_path):
    _, response, metadata = valid_http_archive(tmp_path)
    path = tmp_path / "metadata.json"
    save_json(path, metadata)
    assert load_capture(path, response)["completion_basis"] == "explicit_complete_transfer"
    metadata.pop("transfer_complete")
    metadata.pop("classification")
    save_json(path, metadata)
    with pytest.raises(ValueError, match="allowlist"):
        load_capture(path, response, reviewed_probe=True)


def matching_reference(value, rows):
    return {
        "calendar": [{"event_date": row["event_date"], "is_open": True} for row in rows],
        "securities": [
            {
                "symbol": value["symbol"],
                "ipo_date": date(2023, 1, 1),
                "source_out_date": date(2023, 12, 1),
                "out_date_basis": "verified_first_delisted_day",
            }
        ],
        "bars": [
            {
                **{
                    k: row[k]
                    for k in (
                        "symbol",
                        "event_date",
                        "query_id",
                        "raw_sha256",
                        "open",
                        "close",
                        "high",
                        "low",
                    )
                },
                "volume_shares": row["volume_scaled_shares"],
                "amount_cny": row["amount_scaled_cny"],
            }
            for row in rows
        ],
    }


def test_partial_independent_overlap_is_unverified_even_when_compared_fields_match():
    value = record(east_body([east_row(), east_row("2023-07-19")]))
    rows, issues = convert(value)
    reference = matching_reference(value, rows)
    reference["bars"] = reference["bars"][:1]
    result = audit([value], rows, issues, reference)
    check = next(c for c in result["checks"] if c["name"] == "cross_source_differences")
    assert check["count"] == 0 and check["status"] == "unverified"
    assert check["compared_rows"] == 1
    assert check["interval_rows_without_complete_comparison"] == 1
    reference["bars"] = matching_reference(value, rows)["bars"]
    result = audit([value], rows, issues, reference)
    check = next(c for c in result["checks"] if c["name"] == "cross_source_differences")
    assert check["status"] == "pass" and check["compared_rows"] == 2


def test_known_listing_boundary_violation_fails_and_unknown_calendar_is_unverified():
    value = record()
    rows, issues = convert(value)
    reference = matching_reference(value, rows)
    reference["securities"][0]["source_out_date"] = date(2023, 7, 18)
    reference["calendar"] = []
    result = audit([value], rows, issues, reference)
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["listing_boundaries"]["status"] == "fail"
    assert checks["calendar_reference_coverage"]["status"] == "unverified"
    assert checks["bars_on_closed_days"]["count"] == 0
    reference["securities"][0]["source_out_date"] = date(2023, 7, 19)
    result = audit([value], rows, issues, reference)
    assert (
        next(c for c in result["checks"] if c["name"] == "listing_boundaries")["status"] == "pass"
    )


def test_scaled_decimal_overflow_is_preserved_as_invalid_raw_not_rounded():
    raw = east_row()
    raw[5] = "9999999999999999999"
    value = record(east_body([raw]))
    rows, issues = convert(value)
    assert rows[0]["volume_raw"] == raw[5]
    assert rows[0]["volume_scaled_shares"] is None
    assert rows[0]["record_status"] == "invalid"
    assert issues[0]["field"] == "volume_scaled_shares"


@pytest.mark.parametrize("rc", [False, 0.0, "0"])
def test_provider_status_requires_explicit_integer_zero(rc):
    with pytest.raises(SourceError):
        decode_response(east_body(rc=rc), east_request())


@pytest.mark.parametrize("day", ["2022-12-30", "2024-01-02"])
def test_eastmoney_outside_dates_are_preserved_but_fail_boundary_check(day):
    value = record(east_body([east_row(day)]))
    rows, issues = convert(value)
    result = audit([value], rows, issues, {"calendar": [], "securities": [], "bars": []})
    check = next(c for c in result["checks"] if c["name"] == "outside_requested_interval")
    assert result["interval_rows"] == 0 and check["status"] == "fail"


def test_fetch_and_offline_loader_reject_content_length_mismatch(tmp_path, monkeypatch):
    from io import BytesIO

    body = east_body()
    response = BytesIO(body)
    response.status = 200
    response.url = request_url(east_request())
    response.headers = {"Content-Length": str(len(body) + 1)}
    monkeypatch.setattr(
        collector.urllib.request,
        "build_opener",
        lambda *a: SimpleNamespace(open=lambda *a, **kw: response),
    )
    metadata = collector.fetch(east_request(), tmp_path)
    assert metadata["classification"] == "incomplete_transfer"
    assert metadata["transfer_complete"] is False
    assert (tmp_path / "response.bin").read_bytes() == body
    metadata.update(classification="complete", transfer_complete=True, error=None)
    save_json(tmp_path / "metadata.json", metadata)
    with pytest.raises(ValueError, match="Content-Length"):
        load_capture(tmp_path / "metadata.json", tmp_path / "response.bin")


def configure_fake_collector(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "verify_plan", lambda p: None)
    monkeypatch.setattr(collector, "access_directory", lambda p: tmp_path / "unused-baostock-path")
    monkeypatch.setattr(collector, "code_identity", lambda p: {"commit": "test"})
    plan_path = tmp_path / "plan.json"
    save_json(plan_path, fake_plan(tmp_path))
    return plan_path


def successful_worker(command, **kwargs):
    directory = Path(command[-1])
    request = json.loads(Path(command[-2]).read_text())
    _, response, metadata = valid_http_archive(directory)
    number = request["parameters"]["secid"].split(".")[1]
    body = json.loads(response.read_bytes())
    body["data"]["code"] = number
    response.write_bytes(json.dumps(body).encode())
    metadata.update(
        **request,
        url=request_url(request),
        final_url=request_url(request),
        bytes=response.stat().st_size,
        sha256=digest(response),
    )
    save_json(directory / "metadata.json", metadata)
    return SimpleNamespace(returncode=0)


def test_serial_start_spacing_and_resume_reuse_verified_complete_raw(tmp_path, monkeypatch):
    plan = configure_fake_collector(tmp_path, monkeypatch)
    starts, now = [], [10.0]
    monkeypatch.setattr(collector.time, "time", lambda: now[0])
    monkeypatch.setattr(
        collector.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    def worker(*args, **kwargs):
        starts.append(now[0])
        return successful_worker(*args, **kwargs)

    monkeypatch.setattr(collector.subprocess, "run", worker)
    run = collector.collect(plan, tmp_path / "run", project_root=tmp_path)
    assert run["status"] == "complete" and starts == [10.0, 11.0]
    resumed = collector.collect(plan, tmp_path / "run", project_root=tmp_path)
    assert resumed["status"] == "complete" and len(starts) == 2
    assert len(collector.load_collection(tmp_path / "run/run.json")[0]) == 2
    # A contradictory success entry is rejected before any worker can start on resume.
    metadata_path = Path(run["attempts"][0]["metadata_path"])
    metadata = json.loads(metadata_path.read_text())
    metadata["error"] = "late recorded failure"
    save_json(metadata_path, metadata)
    resumed["attempts"][0]["metadata_sha256"] = digest(metadata_path)
    save_json(tmp_path / "run/run.json", resumed)
    with pytest.raises(ValueError):
        collector.collect(plan, tmp_path / "run", project_root=tmp_path)
    assert len(starts) == 2


def test_invalid_complete_worker_metadata_is_archived_and_stops_before_next_stock(
    tmp_path, monkeypatch
):
    plan = configure_fake_collector(tmp_path, monkeypatch)
    calls = []

    def worker(command, **kwargs):
        calls.append(command)
        successful_worker(command, **kwargs)
        path = Path(command[-1]) / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["transfer_complete"] = False
        save_json(path, metadata)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(collector.subprocess, "run", worker)
    result = collector.collect(plan, tmp_path / "run", project_root=tmp_path)
    assert len(calls) == 1 and result["status"] == "stopped_on_error"
    assert result["attempts"][0]["status"] == "invalid_complete_metadata"
    assert collector.load_collection(tmp_path / "run/run.json")[0] == []


def test_transport_retries_are_bounded_and_do_not_skip_to_another_security(tmp_path, monkeypatch):
    plan = configure_fake_collector(tmp_path, monkeypatch)
    monkeypatch.setattr(collector.time, "sleep", lambda t: None)
    calls = []

    def worker(command, **kwargs):
        calls.append(command)
        save_json(
            Path(command[-1]) / "metadata.json",
            {"classification": "transport_error", "exception": "timeout"},
        )
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(collector.subprocess, "run", worker)
    result = collector.collect(plan, tmp_path / "run", project_root=tmp_path)
    assert len(calls) == 2 and result["status"] == "stopped_on_retry_limit"
    assert len({e["query_id"] for e in result["attempts"]}) == 1
    collector.collect(plan, tmp_path / "run", project_root=tmp_path)
    assert len(calls) == 2
