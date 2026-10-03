"""Offline validation of reviewed listing and independent price evidence.

These archives describe evidence about candidate data.  They do not change the
candidate schema and never grant research eligibility.  Summaries are checked
against their retained bytes before a quality report can use their counts.
"""

from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

import pyarrow.parquet as pq

from ashare_lab.data.raw import digest
from .pipeline import convert, load_capture, load_probes
from .protocol import request_identity, strict_json, verify_url

# Trust anchors: the three reviewed summaries present in main d2a1f90. New
# observations require a separately reviewed version, not a self-signed manifest.
REVIEWED_SUMMARIES = {
    "phase1_szse_listing_date_audit.json": "6afb33c66af597f5f3a219ccdcbe749a29f86950ac942e1a1648bbdf9e8e5ebd",
    "phase1_alternative_source_probe.json": "6273b4de5ac780c8e1f5b182518000f426fcba43004c8452bbda34bb8f875944",
    "phase1_pilot_independent_crosscheck.json": "fc8e1675cb94f00730c8c6246df620d6155b758072b467b2abd4567c91df15c8",
}


class EvidenceError(ValueError):
    """Raised when a reviewed summary cannot be tied to its raw bytes."""


NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _json(path):
    try:
        return strict_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read evidence JSON {path}: {exc}") from exc


def _require(condition, message):
    if not condition:
        raise EvidenceError(message)


def _resolve(root, value, label):
    path = Path(value)
    if not path.is_absolute():
        path = Path(root) / path
    if not path.is_file():
        raise EvidenceError(f"{label} is missing: {path}")
    return path.resolve()


def _ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "sha256": digest(path), "bytes": path.stat().st_size}


def _verify_http(metadata_path, response_path, *, expected_sha=None, label="HTTP raw"):
    metadata_path, response_path = Path(metadata_path), Path(response_path)
    metadata = _json(metadata_path)
    _require(metadata.get("status") == 200, f"{label} status is not 200")
    _require(metadata.get("transfer_complete") is True, f"{label} transfer is incomplete")
    for key in ("error", "exception"):
        _require(metadata.get(key) in (None, ""), f"{label} contains {key}")
    actual_size = response_path.stat().st_size
    actual_sha = digest(response_path)
    _require(metadata.get("bytes") == actual_size, f"{label} byte count differs")
    _require(metadata.get("sha256") == actual_sha, f"{label} digest differs")
    if expected_sha is not None:
        _require(expected_sha == actual_sha, f"{label} differs from evidence digest")
    for key, value in metadata.get("headers", {}).items():
        if key.lower() == "content-length":
            try:
                declared = int(value)
            except (TypeError, ValueError) as exc:
                raise EvidenceError(f"{label} Content-Length is not an integer") from exc
            _require(declared == actual_size, f"{label} Content-Length differs")
    return metadata


def _cell_text(cell, shared_strings):
    kind = cell.attrib.get("t")
    if kind == "s":
        value = cell.find("x:v", NS)
        _require(value is not None and value.text is not None, "XLSX shared-string cell is empty")
        try:
            return shared_strings[int(value.text)]
        except (IndexError, ValueError) as exc:
            raise EvidenceError("XLSX shared-string index is invalid") from exc
    if kind == "inlineStr":
        value = cell.find("x:is", NS)
        return "".join(value.itertext()) if value is not None else ""
    value = cell.find("x:v", NS)
    return value.text if value is not None and value.text is not None else ""


def parse_listing_xlsx(path):
    """Read the A-share code/date cells from the archived SZSE XLSX."""

    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            _require(
                "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names,
                "SZSE XLSX workbook parts are missing",
            )
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {
                rel.attrib["Id"]: rel.attrib["Target"]
                for rel in relationships.findall("r:Relationship", REL_NS)
            }
            sheets = workbook.findall("x:sheets/x:sheet", NS)
            _require(len(sheets) == 1, "SZSE XLSX must contain one reviewed sheet")
            sheet = sheets[0]
            _require(sheet.attrib.get("name") == "A股列表", "SZSE XLSX sheet name differs")
            target = targets.get(sheet.attrib.get(REL_ID))
            _require(target is not None, "SZSE XLSX worksheet relationship is missing")
            worksheet_name = "xl/" + target.lstrip("/")
            _require(worksheet_name in names, "SZSE XLSX worksheet is missing")
            shared = []
            if "xl/sharedStrings.xml" in names:
                strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in strings.findall("x:si", NS)]
            worksheet = ET.fromstring(archive.read(worksheet_name))
    except (OSError, BadZipFile, ET.ParseError) as exc:
        raise EvidenceError(f"cannot parse SZSE XLSX {path}: {exc}") from exc

    rows = []
    for row in worksheet.findall("x:sheetData/x:row", NS):
        number = int(row.attrib.get("r", "0"))
        cell_nodes = row.findall("x:c", NS)
        _require(
            not any(c.find("x:f", NS) is not None for c in cell_nodes),
            "XLSX formula is not a literal observation",
        )
        _require(
            len({c.attrib.get("r") for c in cell_nodes}) == len(cell_nodes), "duplicate XLSX cells"
        )
        cells = {cell.attrib.get("r"): _cell_text(cell, shared) for cell in row.findall("x:c", NS)}
        rows.append(
            {
                "row": number,
                "code": cells.get(f"E{number}", ""),
                "listed_date": cells.get(f"G{number}", ""),
                "cells": cells,
            }
        )
    _require(rows and rows[0]["code"] == "A股代码", "SZSE XLSX header code differs")
    _require(rows[0]["listed_date"] == "A股上市日期", "SZSE XLSX header date differs")
    _require(len({r["row"] for r in rows}) == len(rows), "duplicate XLSX rows")
    data = []
    for row in rows[1:]:
        code, listed = row["code"], row["listed_date"]
        _require(
            len(code) == 6 and code.isdigit(), f"SZSE XLSX code is invalid at row {row['row']}"
        )
        try:
            _require(date.fromisoformat(listed).isoformat() == listed, "noncanonical listing date")
        except (TypeError, ValueError) as exc:
            raise EvidenceError(f"SZSE XLSX listing date is invalid at row {row['row']}") from exc
        data.append(row)
    _require(len({r["code"] for r in data}) == len(data), "duplicate XLSX codes")
    return {"sheet": "A股列表", "rows": data}


def verified_file(root, locator, references):
    path = _resolve(root, locator["path"], "reference")
    _require(digest(path) == locator["sha256"], f"reference digest differs: {path}")
    references.append(_ref(path))
    return path


def listing_reference(root, summary, references):
    """Reconcile the extraction with literal XLSX cells and the retained plan."""
    _require(
        summary["schema_version"] == "official-listing-date-audit-v1", "listing schema differs"
    )
    _require(summary["research_eligible"] is False, "listing research flag differs")
    raw = verified_file(
        root, {"path": summary["raw_path"], "sha256": summary["raw_sha256"]}, references
    )
    metadata_path = raw.with_name("a-list.metadata.json")
    metadata = _verify_http(metadata_path, raw, expected_sha=summary["raw_sha256"])
    _require(metadata == summary["source_observation"], "listing source observation differs")
    for key in ("url", "final_url"):
        url = urlsplit(metadata[key])
        params = parse_qs(url.query)
        _require(
            (url.scheme, url.netloc, url.path) == ("https", "www.szse.cn", "/api/report/ShowReport")
            and params.get("SHOWTYPE") == ["xlsx"]
            and params.get("CATALOGID") == ["1110"]
            and params.get("TABKEY") == ["tab1"],
            "listing source URL differs",
        )
    _require(
        datetime.fromisoformat(metadata["captured_at_utc"]).tzinfo is not None,
        "listing observation timezone missing",
    )
    references.append(_ref(metadata_path))
    extracted_path = verified_file(
        root, {"path": summary["extract_path"], "sha256": summary["extract_sha256"]}, references
    )
    extracted = _json(extracted_path)
    for key in (
        "schema_version",
        "research_eligible",
        "raw_path",
        "raw_sha256",
        "source_observation",
        "historical_plan",
        "historical_available_time",
    ):
        _require(extracted[key] == summary[key], f"listing extraction {key} differs")
    _require(summary["historical_available_time"] is None, "listing historical time is unverified")
    plan = _json(verified_file(root, summary["historical_plan"], references))
    _require(
        plan["schema_version"] == "source-plan-v1"
        and plan["source"] == "baostock"
        and plan["sdk"] == "0.9.4",
        "listing historical plan identity differs",
    )
    planned = {code[3:] + ".SZ" for code in plan["codes"] if code.startswith("sz.")}
    workbook = parse_listing_xlsx(raw)
    source = {r["code"] + ".SZ": r for r in workbook["rows"]}
    expected = planned & source.keys()
    records = extracted["records"]
    _require(len({r["symbol"] for r in records}) == len(records), "duplicate listing references")
    _require(
        {r["symbol"] for r in records} == expected, "listing records differ from historical plan"
    )
    _require(
        len(workbook["rows"]) == summary["current_list_rows"], "listing workbook row count differs"
    )
    _require(
        len(planned) == summary["historical_plan_sz_codes"]
        and len(expected) == summary["matched_historical_codes"]
        and sorted(planned - expected) == summary["unmatched_historical_codes"],
        "listing historical coverage differs",
    )
    result = []
    for row in records:
        cell = source[row["symbol"]]
        _require(
            row["listed_date"] == cell["listed_date"]
            and row["row"] == cell["row"]
            and row["code_cell"] == f"E{cell['row']}"
            and row["listing_date_cell"] == f"G{cell['row']}"
            and row["sheet"] == workbook["sheet"],
            "listing date/cell locator differs",
        )
        result.append(
            {
                **{
                    k: row[k]
                    for k in (
                        "symbol",
                        "listed_date",
                        "row",
                        "code_cell",
                        "listing_date_cell",
                        "sheet",
                    )
                },
                "provider": "szse",
                "raw_path": str(raw),
                "raw_sha256": digest(raw),
                "metadata_path": str(metadata_path),
                "metadata_sha256": digest(metadata_path),
                "extract_path": str(extracted_path),
                "extract_sha256": digest(extracted_path),
                "observed_at_utc": metadata["captured_at_utc"],
                "historical_available_time": None,
                "delisted_date": None,
                "scope": "current official listing-date reference only",
            }
        )
    return result


def _record_files(record, references):
    references.extend([_ref(record["raw_path"]), _ref(record["metadata_path"])])


def probe_references(root, summary, references):
    records, refs = load_probes(root)
    for locator in refs:
        verified_file(root, locator, references)
    _require(
        len(records) == summary["http_market_requests"]
        and sum(len(r["rows"]) for r in records) == summary["raw_bar_rows"],
        "probe counts differ",
    )
    # Check every raw Parquet row against HTTP raw, including out-of-interval rows.
    for locator in summary["raw_manifests"]:
        manifest_path = verified_file(root, locator, references)
        manifest = _json(manifest_path)
        path = verified_file(
            manifest_path.parent,
            {"path": manifest["parquet_file"], "sha256": manifest["parquet_sha256"]},
            references,
        )
        expected = []
        for record in records:
            if record["provider"] != manifest["provider"]:
                continue
            _record_files(record, references)
            for index, row in enumerate(record["rows"]):
                expected.append(
                    {
                        "provider": record["provider"],
                        "symbol": record["symbol"],
                        "source_date": row[0],
                        "requested_start": record["start"],
                        "requested_end": record["end"],
                        "within_requested_interval": record["start"] <= row[0] <= record["end"],
                        "row_number": index,
                        "raw_response": Path(record["raw_path"]).name,
                        "raw_response_sha256": record["raw_sha256"],
                        "raw_row_json": json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                    }
                )
        _require(expected == pq.read_table(path).to_pylist(), "probe Parquet differs from HTTP raw")
    return [r for r in records if r["provider"] == "tencent"]


def crosscheck_references(root, summary, references):
    """Import successful raws; retain failed and unattempted planned identities."""
    _require(
        summary["schema_version"] == "public-crosscheck-audit-v1"
        and summary["research_eligible"] is False,
        "crosscheck schema differs",
    )
    paths = {
        key: verified_file(
            root, {"path": summary[key + "_path"], "sha256": summary[key + "_sha256"]}, references
        )
        for key in ("plan", "captures", "comparisons", "raw_parquet", "reference")
    }
    plan, entries = _json(paths["plan"]), _json(paths["captures"])
    _require(
        plan["research_eligible"] is False
        and plan["reference_sha256"] == summary["reference_sha256"],
        "crosscheck plan reference differs",
    )
    requests = [request_identity(r) for r in plan["requests"]]
    _require(all(r["provider"] == "tencent" for r in requests), "crosscheck source differs")
    expected = {r["query_id"]: r for r in requests}
    _require(
        len(expected) == len(requests) == summary["planned_requests"] == plan["max_requests"],
        "crosscheck plan count/identity differs",
    )
    records, attempts, seen = [], [], set()
    for entry in entries:
        meta_path = verified_file(
            root, {"path": entry["metadata_path"], "sha256": entry["metadata_sha256"]}, references
        )
        response = verified_file(
            root, {"path": entry["response_path"], "sha256": entry["response_sha256"]}, references
        )
        meta = _json(meta_path)
        identity = request_identity(meta)
        key = identity["query_id"]
        _require(
            key in expected
            and key not in seen
            and meta["query_id"] == key
            and entry["symbol"] == identity["symbol"],
            "crosscheck capture identity/duplicate differs",
        )
        seen.add(key)
        verify_url(meta["url"], meta)
        verify_url(meta["final_url"], meta)
        _require(
            meta["sha256"] == digest(response) and meta["bytes"] == response.stat().st_size,
            "crosscheck raw bytes differ",
        )
        _require(entry["status"] == meta["classification"], "crosscheck entry promotes raw status")
        if entry["status"] == "complete":
            records.append(
                load_capture(
                    meta_path,
                    response,
                    metadata_sha256=entry["metadata_sha256"],
                    response_sha256=entry["response_sha256"],
                )
            )
        else:
            _require(
                meta["transfer_complete"] is False
                and bool(meta.get("error") or meta.get("exception")),
                "crosscheck failed metadata contradicts failure",
            )
            _require(
                digest(meta_path) == summary["stop_metadata_sha256"]
                and meta == summary["stop_metadata"],
                "crosscheck stopped evidence differs",
            )
        attempts.append(
            {
                **identity,
                "status": entry["status"],
                "metadata": _ref(meta_path),
                "response": _ref(response),
                "error": meta.get("error"),
            }
        )
    missing = [r for r in requests if r["query_id"] not in seen]
    failed = [a for a in attempts if a["status"] != "complete"]
    for key, value in (
        ("attempted_requests", len(attempts)),
        ("successful_requests", len(records)),
        ("failed_requests", len(failed)),
        ("not_attempted_requests", len(missing)),
    ):
        _require(summary[key] == value, f"crosscheck {key} differs")
    _require(
        sorted(r["symbol"] for r in records) == summary["successful_symbols"]
        and sorted(r["symbol"] for r in failed) == summary["failed_symbols"],
        "crosscheck successful/failed symbols differ",
    )
    raw_rows = [
        {
            "provider": r["provider"],
            "symbol": r["symbol"],
            "event_date": row[0],
            "raw_values_json": json.dumps(row, ensure_ascii=False),
            "raw_sha256": r["raw_sha256"],
            "row_number": i,
        }
        for r in records
        for i, row in enumerate(r["rows"])
    ]
    _require(
        pq.read_table(paths["raw_parquet"]).to_pylist() == raw_rows,
        "crosscheck Parquet differs from successful raws",
    )
    # The old comparison is evidence to verify, never the new build's result.
    from .evidence_quality import compare_prices

    target_rows = pq.read_table(paths["reference"]).to_pylist()
    reference_rows = [row for record in records for row in convert(record)[0]]
    comparisons = compare_prices(target_rows, reference_rows)["comparisons"]
    old = _json(paths["comparisons"])
    _require(
        len(comparisons) == len(old) == summary["rows_compared"],
        "crosscheck comparison count differs",
    )
    old_index = {(r["symbol"], r["date"]): r for r in old}
    _require(len(old_index) == len(old), "duplicate archived comparisons")
    for actual in comparisons:
        item = old_index[(actual["symbol"], actual["date"])]
        _require(
            item["tencent_raw_sha256"] == actual["reference"]["raw_sha256"]
            and item["tencent_row"] == actual["reference"]["row_number"]
            and item["eastmoney_raw_sha256"] == actual["candidate"]["raw_sha256"]
            and item["eastmoney_row"] == actual["candidate"]["row_number"],
            "archived comparison locator differs",
        )
        _require(
            {k: -Decimal(v) for k, v in actual["ohlc_differences"].items()}
            == {k: Decimal(v) for k, v in item["differences"].items()},
            "archived comparison value differs",
        )
    _require(
        sum(len(c["ohlc_differences"]) for c in comparisons) == summary["ohlc_values_compared"]
        and sum(Decimal(v) != 0 for c in comparisons for v in c["ohlc_differences"].values())
        == summary["ohlc_nonzero_differences"],
        "archived comparison summary differs",
    )
    return records, {
        "planned": len(requests),
        "successful": len(records),
        "failed": len(failed),
        "not_attempted": len(missing),
        "attempts": attempts,
        "unattempted_requests": missing,
    }


def load_reference_evidence(root):
    root = Path(root).resolve()
    references, summaries = [], {}
    for name, expected in REVIEWED_SUMMARIES.items():
        path = verified_file(
            root, {"path": "docs/evidence/" + name, "sha256": expected}, references
        )
        summaries[name] = _json(path)
    listing = listing_reference(root, summaries["phase1_szse_listing_date_audit.json"], references)
    probes = probe_references(root, summaries["phase1_alternative_source_probe.json"], references)
    cross, acquisition = crosscheck_references(
        root, summaries["phase1_pilot_independent_crosscheck.json"], references
    )
    captures = [
        {**r, "observed_at_utc": r["observed_at_utc"].isoformat()} for r in [*probes, *cross]
    ]
    return {
        "schema_version": "phase1-reference-evidence-v1",
        "research_eligible": False,
        "listing": listing,
        "price_captures": captures,
        "price_acquisition": {"reviewed_probe_successes": len(probes), "crosscheck": acquisition},
        "references": list({r["path"]: r for r in references}.values()),
    }
