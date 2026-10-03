"""One bounded offline candidate build, with immutable inputs and explicit research gate."""

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import subprocess
import time

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab.contracts import ContractError
from ashare_lab.data.raw import utc_now
from ashare_lab.runtime import code_identity
from ashare_lab.storage import read_dataset
from .archive import FIELDS, OHLC, read_archive
from .quality import compare_row, comparison_summary, membership_report, row_for_day
from .references import load_references
from .source import file_ref, load_source, require

STRINGS = (
    "symbol",
    "archive_sha256",
    "source_repository",
    "source_version",
    "time_basis",
    "record_status",
    "issues_json",
    "listing_reference_json",
    "status_reference_json",
    "volume_source_unit",
    "amount_source_unit",
    "precision_basis",
    "feature_locations_json",
)
SCHEMA = pa.schema(
    [
        *[(f, pa.string()) for f in STRINGS],
        *[
            (f, pa.date32())
            for f in (
                "event_date",
                "requested_start",
                "requested_end",
                "ipo_date_reference",
                "delisted_date",
            )
        ],
        *[
            (f, pa.timestamp("us", tz="UTC"))
            for f in ("observed_at_utc", "historical_publish_time", "available_time")
        ],
        *[
            (f, pa.bool_())
            for f in (
                "research_eligible",
                "reference_trading_active",
                "source_trading_active",
                "volume_is_exact_shares",
                "amount_is_exact_cny",
            )
        ],
        ("calendar_index", pa.int32()),
        ("volume_shares", pa.int64()),
        ("amount_cny", pa.float64()),
        ("experimental_volume_shares", pa.float64()),
        ("experimental_amount_cny", pa.float64()),
        *[(f"raw_{f}", pa.float32()) for f in FIELDS],
        *[(f"restored_{f}", pa.float64()) for f in OHLC],
        *[(f"float32_input_error_bound_{f}", pa.float64()) for f in OHLC],
    ]
)
COMPARISON_SCHEMA = pa.schema(
    [
        *[
            (f, pa.string())
            for f in (
                "symbol",
                "group",
                "reference_provider",
                "reference_locator_json",
                "archive_sha256",
            )
        ],
        ("event_date", pa.date32()),
        ("calendar_index", pa.int32()),
        *[(f"reference_{f}", pa.string()) for f in OHLC],
        *[(f"restored_{f}", pa.float64()) for f in OHLC],
        *[(f"delta_{f}", pa.float64()) for f in OHLC],
        *[(f"equal_cent_{f}", pa.bool_()) for f in OHLC],
        ("experimental_volume_delta", pa.float64()),
        ("experimental_amount_delta", pa.float64()),
    ]
)


def save_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def table_digest(table):
    return hashlib.sha256(
        json.dumps(table.to_pylist(), sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


def verify_roundtrip(path, expected_hashes):
    stored = pq.ParquetFile(path)
    require(stored.num_row_groups == len(expected_hashes), "Parquet row group count differs")
    for i, expected in enumerate(expected_hashes):
        require(table_digest(stored.read_row_group(i)) == expected, "Parquet roundtrip differs")
    table = pq.read_table(path)
    with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as con:
        con.execute("SET TimeZone='UTC'")
        restored = con.execute("select * from read_parquet(?)", [str(path)]).fetch_arrow_table()
    require(table.equals(restored.cast(table.schema)), "DuckDB roundtrip differs")
    return {
        "rows": table.num_rows,
        "parquet_roundtrip": True,
        "duckdb_roundtrip": True,
        "sha256": file_ref(path)["sha256"],
        "bytes": path.stat().st_size,
    }


def _execution_source_identity(project_root):
    """Bind the imported package and every module digest to the declared Git worktree."""
    root = Path(project_root).resolve()
    module_file = Path(__file__).resolve()
    require(module_file.is_relative_to(root), "community module is outside project_root")
    module_root = module_file.parent
    try:
        actual_root = Path(
            subprocess.check_output(
                ["git", "-C", str(module_root), "rev-parse", "--show-toplevel"],
                encoding="utf-8",
                errors="strict",
                timeout=15,
            ).strip()
        ).resolve()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("cannot identify the community module Git worktree") from exc
    require(actual_root == root, "community module belongs to a different project_root")
    source = code_identity(root)
    require(not source["dirty"], "stable clean source commit is required before candidate build")
    module_files = sorted(module_root.glob("*.py"))
    require(
        module_files and module_file in module_files, "community module source is not a package"
    )
    commit_files = {}
    for path in module_files:
        relative = path.relative_to(root).as_posix()
        require(relative in source["files_sha256"], f"community module is not tracked: {relative}")
        actual_bytes = path.read_bytes()
        require(
            hashlib.sha256(actual_bytes).hexdigest() == source["files_sha256"][relative],
            f"community module changed during source identification: {relative}",
        )
        try:
            committed_bytes = subprocess.check_output(
                ["git", "-C", str(root), "show", f"{source['commit']}:{relative}"],
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"cannot verify community module commit path: {relative}") from exc
        # Git's Windows checkout may use CRLF; Python treats CRLF and LF identically.
        require(
            actual_bytes.replace(b"\r\n", b"\n") == committed_bytes.replace(b"\r\n", b"\n"),
            f"community module differs from declared commit: {relative}",
        )
        commit_files[relative] = {
            "sha256": source["files_sha256"][relative],
            "commit_blob_sha256": hashlib.sha256(committed_bytes).hexdigest(),
            "comparison": "exact bytes after CRLF-to-LF normalization only",
        }
    source["community_module"] = {
        "path": str(module_root),
        "commit": source["commit"],
        "files": commit_files,
    }
    return source


def build(*, archive_path, description, root, plan, start, end, output, project_root):
    started, clock_start = utc_now(), time.perf_counter()
    source = _execution_source_identity(project_root)
    output = Path(output).resolve()
    require(not output.exists(), "output already exists; preserve old candidates")
    context = load_source(description, root, archive_path, plan, start, end)
    reference = load_references(context, root)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "community-quote-candidate-v1",
        "data_kind": "real_candidate",
        "research_eligible": False,
        "status": "running",
        "started_at_utc": started,
        "source": source,
        "inputs": context["references"],
        "release": context["release"],
        "release_latest": context["release_latest"],
        "observed_at_utc": context["observed_at_utc"],
        "historical_publish_time": None,
        "available_time": None,
        "start": start,
        "end": end,
        "symbols": context["symbols"],
        "online_requests": 0,
    }
    save_json(output / "manifest.json", manifest)
    try:
        result = _build(context, reference, archive_path, output)
        manifest.update(result)
        manifest.update(
            status="built_candidate",
            finished_at_utc=utc_now(),
            elapsed_seconds=time.perf_counter() - clock_start,
        )
        manifest["files"] = {
            p.name: file_ref(p)
            for p in sorted(output.iterdir())
            if p.is_file() and p.name != "manifest.json"
        }
        save_json(output / "manifest.json", manifest)
        try:
            read_dataset(output)
        except ContractError:
            manifest["canonical_reader_rejected"] = True
        else:
            raise ValueError("formal dataset reader accepted community candidate")
        save_json(output / "manifest.json", manifest)
        return manifest
    except Exception as exc:
        manifest.update(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at_utc=utc_now(),
            elapsed_seconds=time.perf_counter() - clock_start,
        )
        save_json(output / "manifest.json", manifest)
        raise


def _build(context, reference, archive_path, output):
    archive = read_archive(archive_path, set(context["symbols"]), latest=context["release_latest"])
    selected_days = [
        (i, d) for i, d in enumerate(archive["calendar"]) if context["start"] <= d <= context["end"]
    ]
    require(bool(selected_days), "archive has no requested dates")
    open_days = {
        d
        for d, r in reference["calendar"].items()
        if r["is_open"] and context["start"] <= d <= context["end"]
    }
    calendar_differences = {
        "missing_from_archive": sorted(open_days - {d for _, d in selected_days}),
        "extra_in_archive": sorted({d for _, d in selected_days} - open_days),
    }
    extra = defaultdict(list)
    for row in reference["extra"]:
        extra[(row["symbol"], row["event_date"])].append(row)
    counts, coverage, problems = Counter(), [], []
    missing_values, missing_status_rows = Counter(), 0
    primary_comparisons, extra_comparisons = [], []
    path = output / "quotes.parquet"
    batch, rowgroup_hashes = [], []
    with pq.ParquetWriter(path, SCHEMA, compression="zstd") as writer:
        for symbol in context["symbols"]:
            per_symbol = Counter()
            for index, day in selected_days:
                row = row_for_day(symbol, day, index, archive, context, reference)
                counts[row["record_status"]] += 1
                per_symbol[row["record_status"]] += 1
                missing_values.update(f for f in FIELDS if row[f"raw_{f}"] is None)
                missing_status_rows += row["reference_trading_active"] is None
                if row["record_status"] != "price_candidate":
                    problems.append(
                        {
                            k: row[k]
                            for k in (
                                "symbol",
                                "event_date",
                                "calendar_index",
                                "record_status",
                                "issues_json",
                                "ipo_date_reference",
                                "reference_trading_active",
                                "status_reference_json",
                            )
                        }
                    )
                other = reference["primary"].get((symbol, day))
                if other is not None:
                    primary_comparisons.append(compare_row(row, other, "retained_candidates"))
                for other in extra.get((symbol, day), []):
                    extra_comparisons.append(compare_row(row, other, "tencent_limited_reference"))
                batch.append(row)
                if len(batch) >= 2420:
                    table = pa.Table.from_pylist(batch, schema=SCHEMA)
                    rowgroup_hashes.append(table_digest(table))
                    writer.write_table(table)
                    batch = []
            basic = reference["listings"].get(symbol)
            coverage.append(
                {
                    "symbol": symbol,
                    "requested_calendar_days": len(selected_days),
                    "ipo_date_reference": basic.get("ipo_date") if basic else None,
                    "counts": dict(per_symbol),
                }
            )
        if batch:
            table = pa.Table.from_pylist(batch, schema=SCHEMA)
            rowgroup_hashes.append(table_digest(table))
            writer.write_table(table)
    comparisons = pa.Table.from_pylist(
        primary_comparisons + extra_comparisons, schema=COMPARISON_SCHEMA
    )
    pq.write_table(comparisons, output / "comparisons.parquet", compression="zstd")
    quote_roundtrip = verify_roundtrip(output / "quotes.parquet", rowgroup_hashes)
    comparison_roundtrip = verify_roundtrip(
        output / "comparisons.parquet", [table_digest(comparisons)]
    )
    primary_summary = comparison_summary(primary_comparisons)
    extra_summary = comparison_summary(extra_comparisons)
    membership = membership_report(
        archive,
        reference["conditional_membership"],
        context["release_latest"],
        context["start"],
        context["end"],
    )
    members = [
        {k: v for k, v in feature.items() if k != "body"}
        for _, feature in sorted(archive["selected"].items())
    ]
    for name, body in sorted(archive["texts"].items()):
        if name.endswith("/csi300.txt") or "/calendars/" in name:
            filename = (
                "csi300.reference.txt" if name.endswith("/csi300.txt") else name.split("/")[-1]
            )
            (output / filename).write_bytes(body)
            members.append(
                {"member": name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            )
    save_json(output / "members.json", members)
    save_json(
        output / "inventory.json",
        {
            "members": archive["inventory"],
            "feature_headers_checked": archive["feature_headers_checked"],
            "uncompressed_regular_bytes": archive["uncompressed_regular_bytes"],
            "stream_bytes": archive["stream_bytes"],
        },
    )
    save_json(output / "reference-evidence.json", reference["evidence"])
    save_json(output / "membership-reference.json", membership)
    failed = bool(
        counts["invalid_candidate"]
        or primary_summary["mismatches"]
        or extra_summary["mismatches"]
        or any(calendar_differences.values())
    )
    quality = {
        "schema_version": "community-quote-quality-v1",
        "research_eligible": False,
        "formal_research_gate": "blocked",
        "status": "fail" if failed else "unverified",
        "rows": sum(counts.values()),
        "securities": len(context["symbols"]),
        "calendar_days": len(selected_days),
        "row_status_counts": dict(counts),
        "missing_raw_values_by_field": dict(missing_values),
        "rows_without_primary_price_reference": sum(counts.values()) - len(primary_comparisons),
        "rows_without_status_reference": missing_status_rows,
        "missing_ipo_reference_symbols": [
            r["symbol"] for r in coverage if r["ipo_date_reference"] is None
        ],
        "calendar_reference": {
            "status": "fail" if any(calendar_differences.values()) else "pass",
            **calendar_differences,
        },
        "coverage": coverage,
        "missing_or_invalid_rows": problems,
        "retained_candidate_comparison": primary_summary,
        "tencent_limited_comparison": extra_summary,
        "historical_availability": "unverified",
        "delisting": "unverified",
        "trading_status": "unverified",
        "volume_amount": {
            "status": "unverified",
            "exact_quantities_recovered": False,
            "experimental_volume_formula": "raw_volume * raw_factor * 100",
            "experimental_amount_formula": "raw_amount * 1000",
            "price_error_bound_scope": "float32 input rounding only; excludes upstream methodology/revisions",
        },
        "membership": membership,
        "limits": [
            "The original 336-code plan is a coverage scope, not a certified universe.",
            "Raw NaNs retain IEEE bits and source location; no forward fill or zero-return inference.",
            "Release/download times do not establish row historical availability.",
            "Cent matches do not prove lossless prices, independent upstreams or execution eligibility.",
            "Publisher provided no manifest/digest; local digest binds observed bytes only.",
            "Corporate events are not inferred from factor changes; unknown units remain unknown.",
        ],
    }
    save_json(output / "quality.json", quality)
    return {
        "tables": {"quotes": quote_roundtrip, "comparisons": comparison_roundtrip},
        "quality_status": quality["status"],
        "rows": quality["rows"],
        "feature_headers_checked": archive["feature_headers_checked"],
    }
