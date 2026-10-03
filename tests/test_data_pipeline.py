"""Regression cases based on Phase 0 source observations; no network in tests."""

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from ashare_lab.data.candidates import convert
from ashare_lab.data.collect import collect, source_lock
from ashare_lab.data.pipeline import build, load_run
from ashare_lab.data.plans import membership_2023
from ashare_lab.data.quality import audit
from ashare_lab.data.queries import BAR_FIELDS, query_id, symbol, validate_query
from ashare_lab.data.raw import archive_attempt, cached_records, digest, save_json
from ashare_lab.data.worker import fetch


def record(api, fields, rows, parameters):
    return {
        "api": api,
        "parameters": parameters,
        "fields": fields,
        "rows": rows,
        "source": "baostock",
        "sdk": "0.9.4",
        "status": "complete",
        "observed_at_utc": "2026-10-03T03:00:00+00:00",
        "observation_precision": "capture",
        "raw_locator": {"path": "fixture", "sha256": "a" * 64},
        "error_code": "0",
    }


def bar(**replacements):
    raw = dict(
        zip(
            BAR_FIELDS.split(","),
            [
                "2023-01-03",
                "sh.600000",
                "7.10",
                "7.20",
                "7.00",
                "7.15",
                "7.05",
                "10000",
                "71000.00",
                "3",
                "0.908624",
                "1",
                "0",
            ],
        )
    )
    raw.update(replacements)
    return record(
        "query_history_k_data_plus",
        list(raw),
        [list(raw.values())],
        {
            "code": "sh.600000",
            "fields": BAR_FIELDS,
            "frequency": "d",
            "adjustflag": "3",
            "start_date": "2023-01-03",
            "end_date": "2023-01-05",
        },
    )


def converted(r):
    kind, rows, issues = convert(r)
    return rows[0], issues


@pytest.mark.parametrize(
    "code,expected",
    [
        ("sh.600000", "600000.SH"),
        ("sz.000001", "000001.SZ"),
        ("sh.688223", "688223.SH"),
        ("sz.300001", "300001.SZ"),
    ],
)
def test_a_share_mapping(code, expected):
    assert symbol(code) == expected


@pytest.mark.parametrize("code", ["sh.000300", "sh.510300", "sz.399300", "sh.900001", "600000.SH"])
def test_non_equity_and_ambiguous_codes_rejected(code):
    with pytest.raises(ValueError):
        symbol(code)


@pytest.mark.parametrize(
    "change",
    [
        {"start_date": "2024-01-01", "end_date": "2025-12-31"},
        {"start_date": "20230103"},
        {"adjustflag": "2"},
        {"frequency": "5"},
        {"end_date": "2023-01-02"},
        {"fields": "date,code,open"},
    ],
)
def test_query_guard_excludes_holdout_and_default_semantics(change):
    r = bar()
    r["parameters"].update(change)
    with pytest.raises(ValueError):
        validate_query(r)


def test_turnover_percentage_and_units_preserved():
    row, issues = converted(bar())
    assert not issues
    assert row["turnover_fraction"] == Decimal("0.00908624")
    assert row["volume_shares"] == 10000 and row["amount_cny"] == Decimal("71000")
    assert row["historical_publish_time"] is None and row["available_time"] is None


def test_suspended_missing_quantity_not_zero_or_executable():
    row, issues = converted(bar(tradestatus="0", volume="", amount="", turn=""))
    assert not issues
    assert row["volume_shares"] is None and row["amount_cny"] is None
    assert row["trade_observation"] == "suspended_missing_quantity"
    zero, _ = converted(bar(tradestatus="0", volume="0", amount="0"))
    assert zero["trade_observation"] == "suspended_zero_quantity"


@pytest.mark.parametrize(
    "change",
    [
        {"volume": ""},
        {"open": ""},
        {"open": "NaN"},
        {"volume": "1.5"},
        {"high": "6.00"},
        {"isST": "2"},
        {"tradestatus": "0", "volume": "10"},
        {"code": "sh.600001"},
        {"date": "2023-01-06"},
    ],
)
def test_invalid_rows_remain_locatable(change):
    row, issues = converted(bar(**change))
    assert row["record_status"] == "invalid" and issues
    assert issues[0]["row_number"] == 0 and issues[0]["raw_sha256"] == "a" * 64
    raw = json.loads(row["raw_values_json"])
    assert dict(zip(raw["fields"], raw["values"])) | change == dict(
        zip(raw["fields"], raw["values"])
    )


def dividend(year, dates, tax):
    fields = [
        "code",
        "dividPreNoticeDate",
        "dividAgmPumDate",
        "dividPlanAnnounceDate",
        "dividPlanDate",
        "dividRegistDate",
        "dividOperateDate",
        "dividPayDate",
        "dividStockMarketDate",
        "dividCashPsBeforeTax",
        "dividCashPsAfterTax",
        "dividStocksPs",
        "dividCashStock",
        "dividReserveToStockPs",
    ]
    return record(
        "query_dividend_data",
        fields,
        [["sh.600000", *dates, "0.32", tax, "0", "original distribution text", ""]],
        {"code": "sh.600000", "year": str(year), "yearType": "operate"},
    )


def test_distinct_2010_corporate_action_dates():
    row, issues = converted(
        dividend(
            2010,
            [
                "",
                "2010-04-29",
                "2010-04-07",
                "2010-06-04",
                "2010-06-09",
                "2010-06-10",
                "2010-06-18",
                "2010-06-11",
            ],
            "0.105",
        )
    )
    assert not issues
    assert [
        row[k] for k in ("record_date", "ex_date", "cash_payment_date", "bonus_listing_date")
    ] == [date(2010, 6, 9), date(2010, 6, 10), date(2010, 6, 18), date(2010, 6, 11)]


def test_2023_tax_expression_and_implementation_stage():
    row, issues = converted(
        dividend(
            2023,
            [
                "",
                "2023-06-29",
                "2023-04-19",
                "2023-07-13",
                "2023-07-20",
                "2023-07-21",
                "2023-07-21",
                "",
            ],
            "0.288或0.32",
        )
    )
    assert not issues
    assert row["cash_after_tax_expression"] == "0.288或0.32"
    assert row["proposal_announcement_date"] == date(2023, 4, 19)
    assert row["implementation_announcement_date"] == date(2023, 7, 13)
    assert row["available_time"] is None


def test_cumulative_factors_not_event_multipliers():
    r = record(
        "query_adjust_factor",
        ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"],
        [["sh.600000", "2023-07-21", "0.893976", "11.949786", "11.949786"]],
        {"code": "sh.600000", "start_date": "2023-01-01", "end_date": "2023-12-31"},
    )
    row, issues = converted(r)
    assert not issues and row["source_backward_cumulative_factor"] == Decimal("11.949786")
    assert "split_multiplier" not in row


def quality(records, events=()):
    tables, issues = {}, []
    for r in records:
        r["query_id"] = query_id(r)
        kind, rows, found = convert(r)
        tables.setdefault(kind, []).extend(rows)
        issues.extend(found)
    report = audit(tables, records, issues, acquisition_complete=True, events=events)
    return {c["name"]: c for c in report["checks"]}, report


def test_missing_open_day_is_not_suspension_and_duplicates_fail():
    calendar = record(
        "query_trade_dates",
        ["calendar_date", "is_trading_day"],
        [["2023-01-03", "1"], ["2023-01-04", "1"], ["2023-01-05", "1"]],
        {"start_date": "2023-01-03", "end_date": "2023-01-05"},
    )
    r = bar()
    r["rows"].append(r["rows"][0])
    checks, report = quality([r, calendar])
    assert checks["duplicate_keys_within_response"]["status"] == "fail"
    assert checks["bars_missing_open_days"]["count"] == 2
    assert report["research_eligible"] is False


def test_snapshot_shape_does_not_pass_official_membership():
    r = record(
        "query_hs300_stocks",
        ["updateDate", "code", "code_name"],
        [["2022-08-01", f"sh.{600000 + i:06}", "name"] for i in range(300)],
        {"date": "2023-01-03"},
    )
    events = [
        {
            "id": "official",
            "effective_after_close_date": "2022-12-09",
            "first_trading_date": "2022-12-12",
            "added": ["688187.SH"],
            "removed": ["600000.SH"],
        }
    ]
    checks, report = quality([r], events)
    assert checks["membership_shape"]["status"] == "pass"
    assert checks["official_event_reconciliation"]["status"] == "fail"
    assert report["formal_research_gate"] == "blocked"


def test_overlapping_requests_allow_missing_optional_fields_but_reject_contradictions():
    a, b = bar(), bar()
    b["parameters"]["end_date"] = "2023-01-06"
    checks, _ = quality([a, b])
    assert checks["overlapping_observations"]["status"] == "pass"
    b["rows"][0][b["fields"].index("close")] = "7.16"
    checks, _ = quality([a, b])
    assert checks["overlapping_observations"]["count"] == 1


def archive(root, r):
    path = archive_attempt(r, root / "attempts" / query_id(r) / "attempt")
    r = {k: v for k, v in r.items() if k != "rows"} | {
        "query_id": query_id(r),
        "row_count": len(r["rows"]),
        "raw_locator": {"path": str(path), "sha256": digest(path)},
    }
    return r


def test_offline_reuse_resume_roundtrip_and_tamper_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr("ashare_lab.data.collect.code_identity", lambda p: {"commit": "test"})
    monkeypatch.setattr("ashare_lab.data.pipeline.code_identity", lambda p: {"commit": "test"})
    r = bar()
    entry = archive(tmp_path / "raw", r)
    plan = tmp_path / "plan.json"
    save_json(plan, {"queries": [validate_query(r)]})
    args = dict(project_root=tmp_path, access_state=tmp_path / "access", offline=True)
    run = collect(plan, tmp_path / "run", [tmp_path / "raw"], **args)
    assert run["status"] == "complete" and run["queries"][0]["status"] == "reused"
    again = collect(plan, tmp_path / "run", [], **args)
    assert len(again["queries"]) == 1
    records, complete, _ = load_run(tmp_path / "run/run.json")
    assert complete and records[0]["rows"] == r["rows"]
    manifest, report = build(
        [tmp_path / "run/run.json"], tmp_path / "candidates", project_root=tmp_path
    )
    assert manifest["checks"]["canonical_reader_rejected"] and not report["research_eligible"]
    assert manifest["tables"]["bars"]["duckdb_roundtrip"]
    Path(entry["raw_locator"]["path"]).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        cached_records([tmp_path / "raw"])


def test_corrupt_tabular_response_still_archived(tmp_path):
    r = bar()
    r["rows"][0].pop()
    path = archive_attempt(r, tmp_path / "failed")
    assert json.loads(path.read_text())["rows"] == r["rows"]
    assert json.loads(path.with_name("manifest.json").read_text())["tabular_error"]
    assert not cached_records([tmp_path])


def test_kernel_lock_prevents_second_collector(tmp_path):
    with source_lock(tmp_path):
        with pytest.raises(OSError):
            with source_lock(tmp_path):
                pytest.fail("second collector acquired the source lock")


@pytest.mark.parametrize(
    "mode,attempts",
    [("provider_error", 1), ("parse_or_local_error", 1), ("transport_error", 2), ("timeout", 2)],
)
def test_bounded_retry_and_no_followup_after_source_failure(tmp_path, monkeypatch, mode, attempts):
    monkeypatch.setattr("ashare_lab.data.collect.code_identity", lambda p: {"commit": "test"})
    monkeypatch.setattr("ashare_lab.data.collect.time.sleep", lambda seconds: None)
    calls = []

    def run(command, **kwargs):
        progress = Path(command[command.index("--progress") + 1])
        r = bar()
        r["status"] = "running" if mode == "timeout" else mode
        r["retryable"] = mode == "transport_error"
        r["error_code"] = "restriction" if mode == "provider_error" else "0"
        save_json(progress, r)
        calls.append(command)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("ashare_lab.data.collect.subprocess.run", run)
    p = tmp_path / "plan.json"
    save_json(
        p,
        {
            "queries": [
                validate_query(bar()),
                {"api": "query_stock_basic", "parameters": {"code": "sh.600000"}},
            ]
        },
    )
    result = collect(
        p,
        tmp_path / "run",
        [],
        project_root=tmp_path,
        access_state=tmp_path / "access",
        sdk_wheel=tmp_path / "wheel",
    )
    assert len(calls) == attempts and result["status"] == "stopped_on_error"
    assert len(result["queries"]) == attempts
    assert all(x["api"] == "query_history_k_data_plus" for x in result["queries"])
    assert len(list((tmp_path / "run/attempts").glob("*/*/response.json"))) == attempts


@pytest.mark.parametrize("failure_mode", ["provider", "transport"])
def test_failed_pagination_keeps_partial_rows(tmp_path, failure_mode):
    r = bar()

    class Result:
        fields, error_code, error_msg = r["fields"], "0", "ok"
        n = 0

        def next(self):
            self.n += 1
            if self.n == 1:
                return True
            if failure_mode == "transport":
                raise ConnectionError("connection lost on next page")
            self.error_code = "provider_restriction"
            return False

        def get_row_data(self):
            return r["rows"][0]

    result = fetch(
        r,
        SimpleNamespace(query_history_k_data_plus=lambda **kwargs: Result()),
        tmp_path / "progress.json",
    )
    assert len(result["rows"]) == 1
    assert result["status"] == (
        "provider_error" if failure_mode == "provider" else "transport_error"
    )


def test_2023_plan_has_event_boundaries_and_no_holdout():
    value = membership_2023()
    assert all(validate_query(q) for q in value["queries"])
    dates = {q["parameters"].get("date") for q in value["queries"]}
    assert {
        "2022-12-09",
        "2022-12-12",
        "2023-01-03",
        "2023-06-09",
        "2023-06-12",
        "2023-12-08",
        "2023-12-11",
    } <= dates
