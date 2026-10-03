"""Self-contained offline provenance and quality boundaries (no local data required)."""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from zipfile import ZipFile
from xml.sax.saxutils import escape

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.data.raw import digest, save_json
from ashare_lab.fallback import evidence as importer
from ashare_lab.fallback.evidence_quality import compare_prices, report_checks
from ashare_lab.fallback.pipeline import SCHEMA, audit, build, convert
from ashare_lab.fallback.protocol import eastmoney_request, request_identity, request_url


def tx_request(code="000938", start="2023-07-18", end="2023-07-24"):
    return {
        "provider": "tencent",
        "parameters": {
            "_var": "kline_day2023",
            "param": f"sz{code},day,{start},{end},10,",
            "r": "0.8205512681390605",
        },
    }


def price_record(
    provider="eastmoney", code="000938", days=("2023-07-18",), start="2023-07-18", end="2023-07-24"
):
    request = (
        eastmoney_request("sz." + code, start, end)
        if provider == "eastmoney"
        else tx_request(code, start, end)
    )
    rows = [
        [day, "10.10", "10.20", "10.30", "10.00", "100", "102000.00", "0", "0", "0", "0"]
        for day in days
    ]
    if provider == "tencent":
        rows = [r[:5] + ["100.00", {}, "0", "10.20", "0", "0"] for r in rows]
    return {
        **request_identity(request),
        "rows": rows,
        "observed_at_utc": datetime(2026, 10, 3, tzinfo=timezone.utc),
        "raw_path": provider + "/synthetic",
        "raw_sha256": ("a" if provider == "eastmoney" else "b") * 64,
    }


def reference_bundle(listed="2000-01-01", captures=None):
    return {
        "schema_version": "phase1-reference-evidence-v1",
        "research_eligible": False,
        "listing": [
            {
                "symbol": "000938.SZ",
                "listed_date": listed,
                "historical_available_time": None,
                "observed_at_utc": "2026-10-03T00:00:00+00:00",
                "delisted_date": None,
            }
        ],
        "price_captures": [
            {**r, "observed_at_utc": r["observed_at_utc"].isoformat()} for r in (captures or [])
        ],
        "references": [],
    }


def check_map(bundle, records, primary=None):
    rows = [row for r in records for row in convert(r)[0]]
    return {
        c["name"]: c for c in report_checks(bundle, records, rows, primary or {"securities": []})
    }


def write_workbook(path, listed="2000-01-01", *, duplicate=False, formula=False):
    header = '<row r="1"><c r="E1" t="inlineStr"><is><t>A股代码</t></is></c><c r="G1" t="inlineStr"><is><t>A股上市日期</t></is></c></row>'
    body = (
        '<row r="2"><c r="E2" t="inlineStr"><is><t>000938</t></is></c><c r="G2" t="inlineStr">'
        + ("<f>NOW()</f>" if formula else "")
        + f"<is><t>{escape(listed)}</t></is></c></row>"
    )
    with ZipFile(path, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="A股列表" r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + header
            + body
            + (body if duplicate else "")
            + "</sheetData></worksheet>",
        )


@pytest.fixture
def listing_archive(tmp_path):
    raw = tmp_path / "a-list.bin"
    write_workbook(raw)
    url = "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1"
    meta = {
        "url": url,
        "final_url": url,
        "status": 200,
        "transfer_complete": True,
        "sha256": digest(raw),
        "bytes": raw.stat().st_size,
        "headers": {"Content-Length": str(raw.stat().st_size)},
        "captured_at_utc": "2026-10-03T00:00:00+00:00",
    }
    save_json(tmp_path / "a-list.metadata.json", meta)
    plan = tmp_path / "plan.json"
    save_json(
        plan,
        {
            "schema_version": "source-plan-v1",
            "source": "baostock",
            "sdk": "0.9.4",
            "codes": ["sz.000938", "sh.600000"],
        },
    )
    summary = {
        "schema_version": "official-listing-date-audit-v1",
        "research_eligible": False,
        "raw_path": str(raw),
        "raw_sha256": digest(raw),
        "source_observation": meta,
        "historical_plan": {"path": str(plan), "sha256": digest(plan)},
        "historical_available_time": None,
        "current_list_rows": 1,
        "historical_plan_sz_codes": 1,
        "matched_historical_codes": 1,
        "unmatched_historical_codes": [],
    }
    extract = tmp_path / "extract.json"
    save_json(
        extract,
        {
            **summary,
            "records": [
                {
                    "symbol": "000938.SZ",
                    "listed_date": "2000-01-01",
                    "sheet": "A股列表",
                    "row": 2,
                    "code_cell": "E2",
                    "listing_date_cell": "G2",
                }
            ],
        },
    )
    summary.update(extract_path=str(extract), extract_sha256=digest(extract))
    return tmp_path, summary


def test_listing_raw_cells_and_plan_bind_reference_without_current_attributes(listing_archive):
    root, summary = listing_archive
    refs = []
    row = importer.listing_reference(root, summary, refs)[0]
    assert row["code_cell"] == "E2" and row["listed_date"] == "2000-01-01"
    assert row["observed_at_utc"] and row["historical_available_time"] is None
    assert row["delisted_date"] is None and "name" not in row
    assert {Path(r["path"]).name for r in refs} == {
        "a-list.bin",
        "a-list.metadata.json",
        "extract.json",
        "plan.json",
    }


@pytest.mark.parametrize("filename", ["a-list.bin", "extract.json", "plan.json"])
def test_reference_byte_change_is_rejected(listing_archive, filename):
    root, summary = listing_archive
    with (root / filename).open("ab") as handle:
        handle.write(b" ")
    with pytest.raises(ValueError, match="digest"):
        importer.listing_reference(root, summary, [])


@pytest.mark.parametrize(
    "changes",
    [
        {"transfer_complete": False},
        {"error": "failed"},
        {"status": 500},
        {"headers": {"Content-Length": "10"}},
    ],
)
def test_listing_complete_summary_cannot_promote_failed_metadata(listing_archive, changes):
    root, summary = listing_archive
    meta = deepcopy(summary["source_observation"])
    meta.update(changes)
    save_json(root / "a-list.metadata.json", meta)
    with pytest.raises(ValueError):
        importer.listing_reference(root, summary, [])


@pytest.mark.parametrize(
    "change",
    [
        {"listed_date": "2000-01-02"},
        {"listed_date": None},
        {"row": 3},
        {"code_cell": "F2"},
        {"symbol": "000963.SZ"},
    ],
)
def test_rehashed_extraction_must_match_plan_and_exact_cells(listing_archive, change):
    root, summary = listing_archive
    path = root / "extract.json"
    extraction = json.loads(path.read_text(encoding="utf-8"))
    extraction["records"][0].update(change)
    save_json(path, extraction)
    summary["extract_sha256"] = digest(path)
    with pytest.raises(ValueError):
        importer.listing_reference(root, summary, [])


@pytest.mark.parametrize("mode", ["missing", "duplicate", "formula"])
def test_xlsx_unknown_date_duplicates_and_formulas_are_not_literal_dates(tmp_path, mode):
    path = tmp_path / "fixture.xlsx"
    write_workbook(
        path,
        listed="" if mode == "missing" else "2000-01-01",
        duplicate=mode == "duplicate",
        formula=mode == "formula",
    )
    with pytest.raises(ValueError):
        importer.parse_listing_xlsx(path)


def test_unreviewed_summary_digest_is_rejected_before_traversing_raw(tmp_path):
    path = tmp_path / "docs/evidence"
    path.mkdir(parents=True)
    (path / "phase1_szse_listing_date_audit.json").write_text("{}")
    with pytest.raises(ValueError, match="digest"):
        importer.load_reference_evidence(tmp_path)


@pytest.mark.parametrize(
    "ipo, status",
    [("2023-07-17", "pass"), ("2023-07-18", "pass"), ("2023-07-19", "fail"), (None, "unverified")],
)
def test_ipo_is_inclusive_but_never_verifies_delisting_or_pit(ipo, status):
    checks = check_map(reference_bundle(ipo), [price_record()])
    assert checks["ipo_reference_boundary"]["status"] == status
    assert checks["delisting_boundary"]["status"] == "unverified"
    assert checks["pit_availability"]["status"] == "unverified"


def test_audit_rejects_prelisting_bar_without_promoting_unknown_outdate():
    record = price_record()
    rows, issues = convert(record)
    result = audit(
        [record],
        rows,
        issues,
        {"calendar": [], "securities": [], "bars": []},
        evidence=reference_bundle("2023-07-19"),
    )
    assert result["status"] == "fail" and result["research_eligible"] is False
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["listing_boundaries"]["status"] == "fail"
    assert checks["listing_metadata"]["missing_ipo_symbols"] == []
    assert checks["listing_metadata"]["status"] == "unverified"


def test_conflicting_primary_ipo_is_not_overwritten():
    primary = {"securities": [{"symbol": "000938.SZ", "ipo_date": date(2001, 1, 1)}]}
    checks = check_map(reference_bundle(), [price_record()], primary)
    assert checks["ipo_reference_boundary"]["status"] == "fail"
    assert primary["securities"][0]["ipo_date"] == date(2001, 1, 1)


@pytest.mark.parametrize("conflict", [False, True])
def test_duplicate_or_conflicting_listing_reference_rejected(conflict):
    bundle = reference_bundle()
    bundle["listing"].append(
        {**bundle["listing"][0], "listed_date": "2001-01-01" if conflict else "2000-01-01"}
    )
    with pytest.raises(ValueError, match="duplicate/conflicting"):
        check_map(bundle, [price_record()])


def test_comparison_recomputed_with_partial_overlap_units_precision_and_uncovered():
    refs = [price_record("tencent", days=("2023-07-11", "2023-07-18"))]
    targets = [
        price_record(days=("2023-07-11", "2023-07-18", "2023-07-19")),
        price_record(code="000963"),
    ]
    checks = check_map(reference_bundle(captures=refs), targets)
    check = checks["cross_source_ohlc_overlap"]
    assert check["status"] == "unverified" and check["compared_rows"] == 1
    assert check["compared_ohlc_values"] == 4 and check["nonzero_differences"] == 0
    assert check["interval_rows_without_complete_ohlc_comparison"] == 2
    assert check["uncovered_symbols"] == ["000963.SZ"]
    c = check["comparisons"][0]
    assert c["candidate"]["row_number"] == 1 and c["reference"]["row_number"] == 1
    assert c["reference"]["volume_source_unit"] == "unknown"
    assert c["display_volume_difference_shares"] is None
    assert c["reference"]["numeric_display_exponents_json"]
    assert checks["volume_unit_evidence"]["status"] == "unverified"


def test_same_symbol_in_disjoint_dates_has_zero_comparisons():
    ref = price_record("tencent", days=("2023-07-18",))
    record = price_record(days=("2023-07-19",), start="2023-07-19")
    check = check_map(reference_bundle(captures=[ref]), [record])["cross_source_ohlc_overlap"]
    assert check["compared_rows"] == 0 and check["status"] == "unverified"


def test_both_request_intervals_are_required_even_with_matching_dates():
    ref = price_record("tencent", days=("2023-07-11",))
    target = price_record(days=("2023-07-11",), start="2023-07-01")
    result = compare_prices(convert(target)[0], convert(ref)[0])
    assert result["compared_rows"] == 0 and result["interval_rows"] == 1


def test_same_supplier_does_not_count_as_independent():
    row = convert(price_record())[0][0]
    result = compare_prices([row], [row])
    assert result["compared_rows"] == 0 and len(result["same_provider_excluded"]) == 1


@pytest.mark.parametrize("conflict", [False, True])
def test_price_reference_duplicates_or_revisions_not_selected_silently(conflict):
    row = convert(price_record("tencent"))[0][0]
    second = {**row, "close": Decimal("10.21") if conflict else row["close"]}
    with pytest.raises(ValueError, match="duplicate/conflicting"):
        compare_prices(convert(price_record())[0], [row, second])


def test_two_independent_sources_disagreeing_are_retained_as_conflict():
    first = convert(price_record("tencent"))[0][0]
    second = {
        **first,
        "provider": "another_reviewed_source",
        "raw_sha256": "c" * 64,
        "close": Decimal("10.21"),
    }
    result = compare_prices(convert(price_record())[0], [first, second])
    assert result["compared_pairs"] == 2 and result["compared_rows"] == 1
    assert len(result["reference_conflicts"]) == 1 and result["nonzero_differences"] == 1


@pytest.fixture
def cross_archive(tmp_path):
    tx = price_record("tencent")
    target = price_record()
    captures = []
    for request, status in [(tx_request(), "complete"), (tx_request("000963"), "audit_stopped")]:
        identity = request_identity(request)
        directory = tmp_path / identity["symbol"]
        directory.mkdir()
        body = directory / "response.bin"
        body.write_bytes(
            json.dumps({"code": 0, "data": {"sz000938": {"day": tx["rows"]}}}).encode()
            if status == "complete"
            else b""
        )
        meta = {
            **request,
            "query_id": identity["query_id"],
            "url": request_url(request),
            "final_url": request_url(request),
            "status": 200 if status == "complete" else None,
            "classification": status,
            "transfer_complete": status == "complete",
            "sha256": digest(body),
            "bytes": body.stat().st_size,
            "captured_at_utc": "2026-10-03T00:00:00+00:00",
        }
        if status != "complete":
            meta["error"] = "synthetic TLS failure"
        save_json(directory / "metadata.json", meta)
        captures.append(
            {
                "symbol": identity["symbol"],
                "status": status,
                "metadata_path": str(directory / "metadata.json"),
                "metadata_sha256": digest(directory / "metadata.json"),
                "response_path": str(body),
                "response_sha256": digest(body),
            }
        )
    bars_path = tmp_path / "bars.parquet"
    pq.write_table(pa.Table.from_pylist(convert(target)[0], schema=SCHEMA), bars_path)
    save_json(
        tmp_path / "plan.json",
        {
            "max_requests": 3,
            "research_eligible": False,
            "reference_sha256": digest(bars_path),
            "requests": [tx_request(), tx_request("000963"), tx_request("000977")],
        },
    )
    save_json(tmp_path / "captures.json", captures)
    save_json(
        tmp_path / "comparisons.json",
        [
            {
                "symbol": "000938.SZ",
                "date": "2023-07-18",
                "tencent_raw_sha256": captures[0]["response_sha256"],
                "tencent_row": 0,
                "eastmoney_raw_sha256": target["raw_sha256"],
                "eastmoney_row": 0,
                "differences": dict.fromkeys(["open", "high", "low", "close"], "0"),
            }
        ],
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "provider": "tencent",
                    "symbol": "000938.SZ",
                    "event_date": "2023-07-18",
                    "raw_values_json": json.dumps(tx["rows"][0]),
                    "raw_sha256": captures[0]["response_sha256"],
                    "row_number": 0,
                }
            ]
        ),
        tmp_path / "raw.parquet",
    )
    summary = {
        "schema_version": "public-crosscheck-audit-v1",
        "research_eligible": False,
        "planned_requests": 3,
        "attempted_requests": 2,
        "successful_requests": 1,
        "failed_requests": 1,
        "not_attempted_requests": 1,
        "successful_symbols": ["000938.SZ"],
        "failed_symbols": ["000963.SZ"],
        "stop_metadata": meta,
        "stop_metadata_sha256": captures[1]["metadata_sha256"],
        "rows_compared": 1,
        "ohlc_values_compared": 4,
        "ohlc_nonzero_differences": 0,
    }
    for key, filename in {
        "plan": "plan.json",
        "captures": "captures.json",
        "comparisons": "comparisons.json",
        "raw_parquet": "raw.parquet",
        "reference": "bars.parquet",
    }.items():
        summary[key + "_path"] = str(tmp_path / filename)
        summary[key + "_sha256"] = digest(tmp_path / filename)
    return tmp_path, summary


def test_success_failure_and_unattempted_remain_distinct(cross_archive):
    root, summary = cross_archive
    records, state = importer.crosscheck_references(root, summary, [])
    assert len(records) == 1 and records[0]["symbol"] == "000938.SZ"
    assert (state["planned"], state["failed"], state["not_attempted"]) == (3, 1, 1)
    assert state["attempts"][1]["status"] == "audit_stopped"


def test_complete_entry_cannot_promote_failed_raw_in_cross_reference(cross_archive):
    root, summary = cross_archive
    path = root / "captures.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[1]["status"] = "complete"
    save_json(path, entries)
    summary["captures_sha256"] = digest(path)
    with pytest.raises(ValueError, match="promotes"):
        importer.crosscheck_references(root, summary, [])


@pytest.mark.parametrize("kind", ["classification", "transfer_complete", "error", "provider"])
def test_valid_json_cannot_override_capture_metadata(cross_archive, kind):
    root, summary = cross_archive
    path = root / "000938.SZ/metadata.json"
    meta = json.loads(path.read_text())
    meta.update(
        {
            kind: {
                "classification": "running",
                "transfer_complete": False,
                "error": "failure",
                "provider": "eastmoney",
            }[kind]
        }
    )
    save_json(path, meta)
    entries = json.loads((root / "captures.json").read_text())
    entries[0]["metadata_sha256"] = digest(path)
    save_json(root / "captures.json", entries)
    summary["captures_sha256"] = digest(root / "captures.json")
    with pytest.raises(ValueError):
        importer.crosscheck_references(root, summary, [])


def test_same_row_count_parquet_tamper_is_rejected(cross_archive):
    root, summary = cross_archive
    path = root / "raw.parquet"
    rows = pq.read_table(path).to_pylist()
    rows[0]["symbol"] = "000963.SZ"
    pq.write_table(pa.Table.from_pylist(rows), path)
    summary["raw_parquet_sha256"] = digest(path)
    with pytest.raises(ValueError, match="Parquet differs"):
        importer.crosscheck_references(root, summary, [])


def test_build_keeps_prices_and_old_cli_schema_compatible(tmp_path, monkeypatch):
    from ashare_lab.fallback import pipeline, cli

    ref = {"calendar": [], "securities": [], "bars": []}
    monkeypatch.setattr(
        pipeline, "reference_tables", lambda p: (ref, {"path": "synthetic-reference"})
    )
    monkeypatch.setattr(pipeline, "code_identity", lambda p: {"commit": "synthetic-test"})
    record = price_record()
    args = {"project_root": tmp_path, "reference": tmp_path}
    _, old = build([record], tmp_path / "old", **args)
    manifest, new = build(
        [record],
        tmp_path / "new",
        evidence=reference_bundle(captures=[price_record("tencent")]),
        **args,
    )
    assert (
        old["schema_version"] == "public-bars-quality-v1"
        and new["schema_version"] == "public-bars-quality-v2"
    )
    assert pq.read_table(tmp_path / "old/bars.parquet").equals(
        pq.read_table(tmp_path / "new/bars.parquet")
    )
    assert manifest["canonical_reader_rejected"] and new["research_eligible"] is False
    assert "reference-evidence.json" in manifest["files"]
    monkeypatch.setattr(cli, "load_probes", lambda p: ([record], []))
    assert (
        cli.main(
            [
                "probe",
                "--source-root",
                str(tmp_path),
                "--reference",
                str(tmp_path),
                "--output",
                str(tmp_path / "probe"),
            ]
        )
        == 2
    )
