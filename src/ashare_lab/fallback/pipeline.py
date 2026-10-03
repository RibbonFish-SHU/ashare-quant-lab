"""Lossless independent-source candidates and explicit coverage/precision reports."""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab.contracts import ContractError
from ashare_lab.data.candidates import decimal_value
from ashare_lab.data.raw import digest, save_json, utc_now
from ashare_lab.runtime import code_identity
from ashare_lab.storage import read_dataset
from .compatibility import REVIEWED_PROBES
from .protocol import decode_response, verify_url

VERSION = "public-bars-candidate-v1"
NUMBER = pa.decimal128(32, 12)
SCHEMA = pa.schema(
    [
        *[
            (f, pa.string())
            for f in (
                "provider",
                "query_id",
                "symbol",
                "raw_path",
                "raw_sha256",
                "raw_values_json",
                "record_status",
                "volume_raw",
                "volume_source_unit",
                "volume_unit_basis",
                "amount_raw",
                "amount_source_unit",
                "numeric_display_exponents_json",
                "precision_basis",
                "time_basis",
            )
        ],
        ("row_number", pa.int64()),
        ("event_date", pa.date32()),
        ("requested_start", pa.date32()),
        ("requested_end", pa.date32()),
        ("within_requested_interval", pa.bool_()),
        ("volume_is_exact_shares", pa.bool_()),
        *[
            (f, NUMBER)
            for f in (
                "open",
                "high",
                "low",
                "close",
                "volume_scale_to_shares",
                "volume_scaled_shares",
                "amount_scale_to_cny",
                "amount_scaled_cny",
                "preclose",
            )
        ],
        ("source_trading_active", pa.bool_()),
        ("source_is_st", pa.bool_()),
        *[
            (f, pa.timestamp("us", tz="UTC"))
            for f in (
                "observed_at_utc",
                "historical_publish_time",
                "available_time",
            )
        ],
    ],
    metadata={
        b"schema_version": VERSION.encode(),
        b"data_kind": b"real_candidate",
        b"research_eligible": b"false",
    },
)


def load_capture(
    metadata_path,
    response_path,
    *,
    metadata_sha256=None,
    response_sha256=None,
    reviewed_probe=False,
):
    metadata_path, response_path = Path(metadata_path), Path(response_path)
    if metadata_sha256 and digest(metadata_path) != metadata_sha256:
        raise ValueError("HTTP metadata digest differs")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    actual = digest(response_path)
    if actual != metadata["sha256"] or (response_sha256 and actual != response_sha256):
        raise ValueError("HTTP response digest differs")
    compatibility = reviewed_probe and REVIEWED_PROBES.get(digest(metadata_path)) == actual
    if reviewed_probe and not compatibility:
        raise ValueError("capture is not in the reviewed seven-probe digest allowlist")
    if metadata.get("status") != 200 or (
        not compatibility
        and (
            metadata.get("transfer_complete") is not True
            or metadata.get("classification") != "complete"
        )
    ):
        raise ValueError("HTTP raw is not a complete successful response")
    if (
        any(metadata.get(k) not in (None, "") for k in ("exception", "error"))
        or response_path.stat().st_size != metadata["bytes"]
    ):
        raise ValueError("HTTP response error or byte count differs")
    for key, value in metadata.get("headers", {}).items():
        if key.lower() == "content-length" and int(value) != metadata["bytes"]:
            raise ValueError("HTTP Content-Length differs from archived bytes")
    verify_url(metadata["url"], metadata)
    verify_url(metadata["final_url"], metadata)
    identity, rows = decode_response(response_path.read_bytes(), metadata)
    if metadata.get("query_id", identity["query_id"]) != identity["query_id"]:
        raise ValueError("HTTP metadata query identity differs from parameters")
    observed = datetime.fromisoformat(metadata["captured_at_utc"])
    if observed.tzinfo is None:
        raise ValueError("capture timestamp must have a timezone")
    return {
        **identity,
        "rows": rows,
        "observed_at_utc": observed.astimezone(timezone.utc),
        "raw_path": str(response_path.resolve()),
        "raw_sha256": actual,
        "metadata_path": str(metadata_path.resolve()),
        "metadata_sha256": digest(metadata_path),
        "completion_basis": "reviewed_seven_probe_digests"
        if compatibility
        else "explicit_complete_transfer",
    }


def load_probes(project_root):
    root = Path(project_root)
    evidence_path = root / "docs/evidence/phase1_alternative_source_probe.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    records, refs = [], []
    for manifest_ref in evidence["raw_manifests"]:
        path = root / manifest_ref["path"]
        if digest(path) != manifest_ref["sha256"]:
            raise ValueError("reviewed HTTP probe manifest digest differs")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["schema_version"] != "public-http-probe-v1":
            raise ValueError("unsupported HTTP probe archive")
        if digest(path.parent / manifest["parquet_file"]) != manifest["parquet_sha256"]:
            raise ValueError("reviewed raw probe Parquet digest differs")
        for capture in manifest["captures"]:
            response = root / capture["response_path"]
            metadata = response.with_name(response.name.replace("response.txt", "metadata.json"))
            record = load_capture(
                metadata,
                response,
                metadata_sha256=capture["metadata_sha256"],
                response_sha256=capture["response_sha256"],
                reviewed_probe=True,
            )
            if (
                record["provider"] != manifest["provider"]
                or record["provider"] != capture["provider"]
                or record["symbol"] != capture["symbol"]
                or record["start"] != capture["requested_start"]
                or record["end"] != capture["requested_end"]
                or len(record["rows"]) != capture["raw_bar_rows"]
            ):
                raise ValueError("probe capture identity/count differs from archive")
            records.append(record)
        refs.append({"path": str(path.resolve()), "sha256": digest(path)})
    return records, [{"path": str(evidence_path.resolve()), "sha256": digest(evidence_path)}, *refs]


def units(provider, security):
    if provider == "eastmoney":
        return "lot_100_shares", Decimal(100), "reviewed_endpoint_documentation_and_three_samples"
    if security == "688065.SH":
        return "share", Decimal(1), "verified_688065_sample_only"
    if security in {"600000.SH", "000001.SZ"}:
        return "lot_100_shares", Decimal(100), "verified_named_security_sample_only"
    return "unknown", None, "no_security_specific_unit_verification"


def convert(record):
    rows, issues = [], []
    unit, volume_scale, basis = units(record["provider"], record["symbol"])
    amount_scale = Decimal(1 if record["provider"] == "eastmoney" else 10000)
    for index, raw in enumerate(record["rows"]):
        row = {
            "provider": record["provider"],
            "query_id": record["query_id"],
            "symbol": record["symbol"],
            "raw_path": record["raw_path"],
            "raw_sha256": record["raw_sha256"],
            "row_number": index,
            "raw_values_json": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            "observed_at_utc": record["observed_at_utc"],
            "historical_publish_time": None,
            "available_time": None,
            "time_basis": "unknown_historical_vintage",
            "record_status": "candidate_execution_unknown",
            "requested_start": date.fromisoformat(record["start"]),
            "requested_end": date.fromisoformat(record["end"]),
            "volume_source_unit": unit,
            "volume_unit_basis": basis,
            "volume_scale_to_shares": volume_scale,
            "volume_is_exact_shares": False,
            "amount_source_unit": "CNY" if amount_scale == 1 else "ten_thousand_CNY",
            "amount_scale_to_cny": amount_scale,
            "precision_basis": "lexical_decimal_places_only; accuracy_and_rounding_rule_unverified",
            "preclose": None,
            "source_trading_active": None,
            "source_is_st": None,
        }
        start_issues = len(issues)

        def problem(name, value):
            issues.append(
                {
                    "query_id": record["query_id"],
                    "raw_path": record["raw_path"],
                    "raw_sha256": record["raw_sha256"],
                    "row_number": index,
                    "field": name,
                    "raw_value": value,
                    "status": "fail",
                }
            )

        try:
            event = date.fromisoformat(raw[0])
            if event.isoformat() != raw[0]:
                raise ValueError("noncanonical date")
            row["event_date"] = event
            row["within_requested_interval"] = (
                row["requested_start"] <= event <= row["requested_end"]
            )
        except (ValueError, TypeError):
            row["event_date"], row["within_requested_interval"] = None, False
            problem("date", raw[0])
        exponents = {}
        positions = {
            "open": 1,
            "close": 2,
            "high": 3,
            "low": 4,
            "volume": 5,
            "amount": 6 if record["provider"] == "eastmoney" else 8,
        }
        values = {}
        for field, position in positions.items():
            text = raw[position]
            if field in {"volume", "amount"}:
                row[field + "_raw"] = text if isinstance(text, str) else json.dumps(text)
            try:
                if not isinstance(text, str) or not text:
                    raise ValueError("numeric source string required")
                values[field] = decimal_value(text)
                exponents[field] = Decimal(text).as_tuple().exponent
            except (ValueError, TypeError, ArithmeticError):
                values[field] = None
                problem(field, text)
        row.update({k: values[k] for k in ("open", "close", "high", "low")})
        for field, scale, target in (
            ("volume", volume_scale, "volume_scaled_shares"),
            ("amount", amount_scale, "amount_scaled_cny"),
        ):
            row[target] = None
            if values[field] is not None and scale is not None:
                try:
                    with localcontext() as context:
                        context.prec = 50
                        row[target] = decimal_value(str(values[field] * scale))
                except (ValueError, ArithmeticError):
                    problem(target, row[field + "_raw"])
        row["numeric_display_exponents_json"] = json.dumps(exponents, sort_keys=True)
        if all(row[k] is not None for k in ("open", "close", "high", "low")):
            if not (
                0 < row["low"] <= row["open"] <= row["high"]
                and row["low"] <= row["close"] <= row["high"]
            ):
                problem("OHLC", raw[1:5])
        if len(issues) != start_issues:
            row["record_status"] = "invalid"
        elif not row["within_requested_interval"]:
            row["record_status"] = "outside_requested_interval"
        elif volume_scale is None:
            row["record_status"] = "needs_unit_review"
        rows.append(row)
    return rows, issues


def reference_tables(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != "baostock-candidate-v2":
        raise ValueError("reference must be an explicit BaoStock candidate snapshot")
    tables = {}
    for kind in ("calendar", "securities", "bars"):
        path = directory / f"{kind}.parquet"
        if digest(path) != manifest["tables"][kind]["sha256"]:
            raise ValueError("reference candidate digest differs")
        tables[kind] = pq.read_table(path).to_pylist()
    return tables, {
        "path": str((directory / "manifest.json").resolve()),
        "sha256": digest(directory / "manifest.json"),
    }


def audit(records, rows, issues, reference):
    checks = []

    def check(name, status, count, **detail):
        checks.append({"name": name, "status": status, "count": count, **detail})

    check("typed_conversion", "fail" if issues else "pass", len(issues), examples=issues[:8])
    counts = Counter((r["query_id"], r["event_date"]) for r in rows)
    duplicate = [
        {"query_id": key[0], "date": str(key[1]), "count": count}
        for key, count in counts.items()
        if count > 1
    ]
    check("duplicate_dates", "fail" if duplicate else "pass", len(duplicate), examples=duplicate)
    unordered = []
    for record in records:
        dates_in_response = [
            r["event_date"] for r in rows if r["query_id"] == record["query_id"] and r["event_date"]
        ]
        if dates_in_response != sorted(dates_in_response):
            unordered.append(record["query_id"])
    check("response_order", "fail" if unordered else "pass", len(unordered), query_ids=unordered)
    outside = [r for r in rows if not r["within_requested_interval"]]
    unexpected_outside = [
        r
        for r in outside
        if r["event_date"] is not None
        and (r["provider"] != "tencent" or r["event_date"] > r["requested_end"])
    ]
    check(
        "outside_requested_interval",
        "fail" if unexpected_outside else "unverified" if outside else "pass",
        len(outside),
        unexpected_rows=len(unexpected_outside),
        disposition="retained; excluded from interval coverage and cross-source comparisons",
    )
    calendar_dates = {r["event_date"]: r["is_open"] for r in reference["calendar"]}
    calendar = {d for d, opened in calendar_dates.items() if opened}
    basics = {r["symbol"]: r for r in reference["securities"]}
    bars = {(r["symbol"], r["event_date"]): r for r in reference["bars"]}
    gaps, unknown_listing, comparisons, differences, closed = [], [], [], [], []
    unknown_calendar = []
    unknown_ipo = []
    listing_violations = []
    coverage = []
    for record in records:
        selected = [
            r
            for r in rows
            if r["query_id"] == record["query_id"] and r["within_requested_interval"]
        ]
        actual = {r["event_date"] for r in selected}
        basic = basics.get(record["symbol"])
        expected = {
            d
            for d in calendar
            if record["start"] <= str(d) <= record["end"]
            and (basic is None or basic["ipo_date"] is None or d >= basic["ipo_date"])
            and (
                basic is None
                or basic.get("out_date_basis") != "verified_first_delisted_day"
                or basic.get("source_out_date") is None
                or d < basic["source_out_date"]
            )
        }
        if basic is None or basic.get("ipo_date") is None:
            unknown_ipo.append(record["symbol"])
        if (
            basic is None
            or basic.get("ipo_date") is None
            or basic.get("source_out_date") is None
            or basic.get("out_date_basis") != "verified_first_delisted_day"
        ):
            unknown_listing.append(record["symbol"])
        for day in actual:
            if basic is not None and (
                (basic.get("ipo_date") is not None and day < basic["ipo_date"])
                or (
                    basic.get("out_date_basis") == "verified_first_delisted_day"
                    and basic.get("source_out_date") is not None
                    and day >= basic["source_out_date"]
                )
            ):
                listing_violations.append(
                    {"query_id": record["query_id"], "symbol": record["symbol"], "date": str(day)}
                )
        first, last = date.fromisoformat(record["start"]), date.fromisoformat(record["end"])
        if any(
            first + timedelta(days=i) not in calendar_dates for i in range((last - first).days + 1)
        ):
            unknown_calendar.append(record["query_id"])
        missing = sorted(expected - actual)
        if missing:
            gaps.append(
                {
                    "query_id": record["query_id"],
                    "symbol": record["symbol"],
                    "dates": [str(d) for d in missing],
                    "listing_metadata_available": basic is not None,
                    "meaning": "unexplained absence; neither suspension nor truncation established",
                }
            )
        closed.extend(
            {"query_id": record["query_id"], "date": str(d)}
            for d in actual
            if d in calendar_dates and not calendar_dates[d]
        )
        coverage.append(
            {
                "query_id": record["query_id"],
                "symbol": record["symbol"],
                "provider": record["provider"],
                "raw_rows": len(record["rows"]),
                "interval_rows": len(selected),
                "calendar_open_dates": len(expected),
                "missing_dates": len(missing),
                "first_returned": str(min(actual)) if actual else None,
                "last_returned": str(max(actual)) if actual else None,
            }
        )
        for row in selected:
            other = bars.get((row["symbol"], row["event_date"]))
            if other is None or row["record_status"] == "invalid":
                continue
            compared = {
                "provider": row["provider"],
                "symbol": row["symbol"],
                "date": str(row["event_date"]),
                "query_id": row["query_id"],
                "row_number": row["row_number"],
                "raw_sha256": row["raw_sha256"],
                "reference_query_id": other["query_id"],
                "reference_raw_sha256": other["raw_sha256"],
                "differences": {},
            }
            for field, other_field in {
                "open": "open",
                "close": "close",
                "high": "high",
                "low": "low",
                "volume_scaled_shares": "volume_shares",
                "amount_scaled_cny": "amount_cny",
            }.items():
                if row[field] is not None and other[other_field] is not None:
                    delta = row[field] - Decimal(other[other_field])
                    compared["differences"][field] = str(delta)
                    if delta:
                        differences.append(
                            {
                                **{k: v for k, v in compared.items() if k != "differences"},
                                "field": field,
                                "difference": str(delta),
                            }
                        )
            if compared["differences"]:
                comparisons.append(compared)
    check(
        "calendar_reference_coverage",
        "unverified" if unknown_calendar else "pass",
        len(unknown_calendar),
        query_ids=unknown_calendar,
    )
    check(
        "calendar_missing_rows",
        "unverified" if gaps or unknown_calendar else "pass",
        sum(len(g["dates"]) for g in gaps),
        gaps=gaps,
    )
    check(
        "listing_metadata",
        "unverified" if unknown_listing else "pass",
        len(set(unknown_listing)),
        symbols=sorted(set(unknown_listing)),
        missing_ipo_symbols=sorted(set(unknown_ipo)),
        reason="basic row presence does not verify unknown IPO/out dates or delisting-date semantics",
    )
    check("bars_on_closed_days", "fail" if closed else "pass", len(closed), examples=closed[:8])
    check(
        "listing_boundaries",
        "fail" if listing_violations else "unverified" if unknown_listing else "pass",
        len(listing_violations),
        examples=listing_violations,
    )
    check(
        "successful_empty_responses",
        "unverified" if any(not r["rows"] for r in records) else "pass",
        sum(not r["rows"] for r in records),
    )
    untested_queries = sorted(
        {r["query_id"] for r in records} - {r["query_id"] for r in comparisons}
    )
    interval_rows = sum(r["within_requested_interval"] for r in rows)
    compared_fields = Counter(f for c in comparisons for f in c["differences"])
    complete_comparisons = sum(len(c["differences"]) == 6 for c in comparisons)
    check(
        "cross_source_differences",
        "unverified"
        if differences
        or not comparisons
        or untested_queries
        or complete_comparisons < interval_rows
        else "pass",
        len(differences),
        compared_rows=len(comparisons),
        compared_fields=dict(compared_fields),
        interval_rows_without_complete_comparison=interval_rows - complete_comparisons,
        untested_query_ids=untested_queries,
        differences=differences,
        note="display precision differs; no source silently overrides another",
    )
    check(
        "historical_availability_and_execution",
        "unverified",
        len(rows),
        reason="vintage, ST, suspension intervals, limits, preclose and execution unknown",
    )
    check(
        "volume_and_amount_precision",
        "unverified",
        len(rows),
        unknown_volume_units=sum(r["volume_scale_to_shares"] is None for r in rows),
        reason="scaled displayed quantities are not exact executions; lexical decimals do not establish rounding",
    )
    return {
        "schema_version": "public-bars-quality-v1",
        "research_eligible": False,
        "formal_research_gate": "blocked",
        "status": "fail" if any(c["status"] == "fail" for c in checks) else "unverified",
        "raw_rows": len(rows),
        "interval_rows": sum(r["within_requested_interval"] for r in rows),
        "checks": checks,
        "coverage": coverage,
        "comparisons": comparisons,
    }


def build(records, output, *, project_root, reference, input_refs=(), acquisition=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if len({r["query_id"] for r in records}) != len(records):
        raise ValueError("choose one explicit response version per provider request")
    rows, issues = [], []
    for record in records:
        converted, found = convert(record)
        rows.extend(converted)
        issues.extend(found)
    ref, ref_identity = reference_tables(reference)
    quality = audit(records, rows, issues, ref)
    quality["acquisition"] = acquisition or {
        "successful_captures": len(records),
        "scope": "explicit reused captures",
    }
    typed = pa.Table.from_pylist(rows, schema=SCHEMA)
    path = output / "bars.parquet"
    pq.write_table(typed, path, compression="zstd")
    if not typed.equals(pq.read_table(path)):
        raise ValueError("fallback Parquet roundtrip failed")
    with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as con:
        con.execute("SET TimeZone='UTC'")
        restored = con.execute("select * from read_parquet(?)", [str(path)]).fetch_arrow_table()
    if typed.to_pylist() != restored.to_pylist():
        raise ValueError("fallback DuckDB roundtrip failed")
    save_json(output / "quality.json", quality)
    save_json(output / "conversion-issues.json", issues)
    manifest = {
        "schema_version": VERSION,
        "data_kind": "real_candidate",
        "research_eligible": False,
        "created_at_utc": utc_now(),
        "source": code_identity(Path(project_root)),
        "inputs": list(input_refs),
        "reference": ref_identity,
        "captures": [
            {k: v for k, v in r.items() if k not in {"rows", "observed_at_utc"}} for r in records
        ],
        "files": {p.name: digest(p) for p in output.iterdir() if p.is_file()},
        "rows": len(rows),
        "parquet_roundtrip": True,
        "duckdb_roundtrip": True,
    }
    save_json(output / "manifest.json", manifest)
    try:
        read_dataset(output)
    except ContractError:
        manifest["canonical_reader_rejected"] = True
    else:
        raise ValueError("canonical reader accepted fallback candidates")
    save_json(output / "manifest.json", manifest)
    return manifest, quality
