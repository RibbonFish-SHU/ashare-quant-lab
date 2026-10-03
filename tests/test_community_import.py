"""Self-contained offline adversarial fixtures; never require retained real archives."""

from copy import deepcopy
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.community.archive import FIELDS, Limits, feature_value, read_archive, sha256_file
from ashare_lab.community.pipeline import build, save_json
from ashare_lab.community.quality import compare_row, membership_report, row_for_day
from ashare_lab.community.references import load_references
from ashare_lab.community.source import load_source, planned_symbols
from ashare_lab.data.queries import BAR_FIELDS

DAYS = [date(2023, 1, n) for n in (3, 4, 5)]
SYMBOLS = {"000001.SZ", "601059.SH"}


def entries():
    result = [
        ("qlib_bin/calendars/day.txt", b"2023-01-03\n2023-01-04\n2023-01-05\n"),
        ("qlib_bin/calendars/day_future.txt", b"2023-01-03\n2023-01-04\n2023-01-05\n"),
        (
            "qlib_bin/instruments/all.txt",
            b"SZ000001\t1991-04-03\t2023-01-05\nSH601059\t2023-01-04\t2023-01-05\n",
        ),
        ("qlib_bin/instruments/csi300.txt", b"SZ000001\t2023-01-03\t2023-01-05\n"),
    ]
    values = {
        "open": 3.0,
        "close": 3.0,
        "high": 3.3,
        "low": 2.7,
        "factor": 0.3,
        "volume": 123.4,
        "amount": 12345.67,
        "adjclose": 3.0,
        "change": 0.001,
        "vwap": 3.0,
    }
    for code in ("sz000001", "sh601059"):
        offset = 0 if code == "sz000001" else 1
        for field in FIELDS:
            body = [values[field]] * (3 - offset)
            if offset == 0 and field in {"open", "high", "low", "close", "factor"}:
                body[1] = float("nan")
            result.append(
                (
                    f"qlib_bin/features/{code}/{field}.day.bin",
                    struct.pack("<" + "f" * (1 + len(body)), offset, *body),
                )
            )
    return result


def make_archive(path, members):
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for item in members:
            name, body = item[:2]
            member = tarfile.TarInfo(name)
            if len(item) == 3:
                member.type, member.linkname = item[2], "qlib_bin/calendars/day.txt"
                body = b""
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return path


def ref(path):
    return {"path": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size}


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    archive = make_archive(tmp_path / "qlib_bin.tar.gz", entries())
    plan = {
        "schema_version": "source-plan-v1",
        "source": "baostock",
        "sdk": "0.9.4",
        "research_eligible": False,
        "codes": ["sz.000001", "sh.601059"],
        "queries": [],
    }
    for code in plan["codes"]:
        plan["queries"].append(
            {
                "api": "query_history_k_data_plus",
                "parameters": {
                    "code": code,
                    "start_date": "2023-01-01",
                    "end_date": "2023-12-31",
                    "frequency": "d",
                    "adjustflag": "3",
                    "fields": BAR_FIELDS,
                },
            }
        )
    plan_path = tmp_path / "plan.json"
    save_json(plan_path, plan)
    url = "https://github.com/chenditc/investment_data/releases/download/2023-12-31/qlib_bin.tar.gz"
    published, observed = "2023-12-31T09:28:09Z", "2026-10-03T14:11:57+00:00"
    identity = {
        "repository": "chenditc/investment_data",
        "tag": "2023-12-31",
        "tag_commit": "a" * 40,
        "release_id": 135431362,
        "asset_id": 143134949,
        "published_at": published,
        "asset_created_at": "2023-12-31T09:28:10Z",
        "asset_updated_at": "2023-12-31T09:28:43Z",
        "publisher_digest": None,
        "publisher_manifest_present": False,
    }
    release = {
        "id": identity["release_id"],
        "tag_name": identity["tag"],
        "published_at": published,
        "html_url": "https://github.com/chenditc/investment_data/releases/tag/2023-12-31",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "qlib_bin.tar.gz",
                "id": identity["asset_id"],
                "state": "uploaded",
                "browser_download_url": url,
                "size": archive.stat().st_size,
                "created_at": identity["asset_created_at"],
                "updated_at": identity["asset_updated_at"],
                "digest": None,
            }
        ],
    }
    download = {
        "source": identity["repository"],
        "tag": identity["tag"],
        "release_id": identity["release_id"],
        "asset_id": identity["asset_id"],
        "url": url,
        "complete": True,
        "status": 200,
        "expected_bytes": archive.stat().st_size,
        "bytes": archive.stat().st_size,
        "content_length": str(archive.stat().st_size),
        "sha256": sha256_file(archive),
        "started_at": observed,
        "completed_at": observed,
    }
    documents = {
        "release": release,
        "download": download,
        "tags": [{"ref": "refs/tags/2023-12-31", "object": {"type": "commit", "sha": "a" * 40}}],
    }
    config = {
        "schema_version": "community-import-source-v1",
        "research_eligible": False,
        "release": identity,
        "archive": ref(archive),
        "plan": ref(plan_path),
        "documents": {},
        "snapshots": [],
        "include_verified_szse_tencent": False,
    }
    for role, document in documents.items():
        path = tmp_path / (role + ".json")
        save_json(path, document)
        config["documents"][role] = ref(path)
    bars = []
    for symbol in sorted(SYMBOLS):
        for i, day in enumerate(DAYS):
            if symbol == "601059.SH" and i == 0:
                continue
            bars.append(
                {
                    "symbol": symbol,
                    "event_date": day,
                    "source": "baostock",
                    "record_status": "valid_candidate",
                    "source_adjustflag": "3",
                    "source_trading_active": not (symbol == "000001.SZ" and i == 1),
                    "query_id": symbol,
                    "raw_path": "synthetic fixture",
                    "raw_sha256": "b" * 64,
                    "row_number": i,
                    "open": 10.0,
                    "close": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "volume_shares": 3702,
                    "amount_cny": 12345670.0,
                }
            )
    tables = {
        "bars": bars,
        "calendar": [{"event_date": day, "is_open": True, "source": "baostock"} for day in DAYS],
        "securities": [
            {
                "symbol": symbol,
                "ipo_date": date(1991, 4, 3) if symbol == "000001.SZ" else DAYS[1],
                "source": "baostock",
            }
            for symbol in sorted(SYMBOLS)
        ],
    }
    manifest = {"research_eligible": False, "tables": {}}
    table_refs = {}
    for kind, rows in tables.items():
        path = tmp_path / (kind + ".parquet")
        pq.write_table(pa.Table.from_pylist(rows), path)
        table_refs[kind] = ref(path)
        manifest["tables"][kind] = {"sha256": sha256_file(path)}
    manifest_path = tmp_path / "reference-manifest.json"
    save_json(manifest_path, manifest)
    config["snapshots"].append({"manifest": ref(manifest_path), "tables": table_refs})
    description = tmp_path / "source.json"
    save_json(description, config)
    monkeypatch.setattr(
        "ashare_lab.community.pipeline._execution_source_identity",
        lambda root: {"commit": "c" * 40, "dirty": False, "purpose": "synthetic fixture"},
    )
    return {
        "archive_path": archive,
        "description": description,
        "root": tmp_path,
        "plan": plan_path,
        "start": date(2023, 1, 1),
        "end": date(2023, 12, 31),
        "output": tmp_path / "candidate",
        "project_root": tmp_path,
    }


def context(bundle):
    return load_source(
        bundle["description"],
        bundle["root"],
        bundle["archive_path"],
        bundle["plan"],
        bundle["start"],
        bundle["end"],
    )


def rewrite_document(bundle, role, change):
    path = bundle["root"] / (role + ".json")
    value = json.loads(path.read_text())
    change(value)
    save_json(path, value)
    config = json.loads(bundle["description"].read_text())
    config["documents"][role] = ref(path)
    save_json(bundle["description"], config)


def test_complete_fixture_preserves_unknowns_precision_locations_and_inputs(bundle):
    before = {p.name: sha256_file(p) for p in bundle["root"].iterdir() if p.is_file()}
    result = build(**bundle)
    rows = pq.read_table(bundle["output"] / "quotes.parquet").to_pylist()
    quality = json.loads((bundle["output"] / "quality.json").read_text())
    assert len(rows) == result["rows"] == 6
    assert result["canonical_reader_rejected"] and result["quality_status"] == "unverified"
    assert quality["row_status_counts"] == {
        "price_candidate": 4,
        "missing_with_suspension_reference": 1,
        "before_ipo_reference": 1,
    }
    stats = quality["retained_candidate_comparison"]
    assert stats["counts"] == {
        "reference_rows": 5,
        "equal_after_cent_rounding": 16,
        "compared_values": 16,
        "unavailable_values": 4,
    }
    row = rows[0]
    assert row["restored_open"] != 10 and round(row["restored_open"], 2) == 10
    assert abs(row["restored_open"] - 10) <= row["float32_input_error_bound_open"]
    loc = json.loads(row["feature_locations_json"])["open"]
    assert loc["byte_offset"] == 4 and loc["value_index"] == 0
    assert struct.unpack("<f", struct.pack("<I", loc["raw_bits"]))[0] == row["raw_open"]
    missing = rows[1]
    assert missing["restored_open"] is None
    assert json.loads(missing["feature_locations_json"])["open"]["value_state"] == "nan"
    assert all(
        r["available_time"] is None
        and r["source_trading_active"] is None
        and not r["research_eligible"]
        and r["volume_shares"] is None
        and r["amount_cny"] is None
        for r in rows
    )
    assert rows[4]["record_status"] == "price_candidate"  # IPO date inclusive.
    assert row["experimental_volume_shares"] != 3702
    assert before == {p.name: sha256_file(p) for p in bundle["root"].iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="output already exists"):
        build(**bundle)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "qlib_bin/../escape",
        "qlib_bin/./x",
        "qlib_bin/a\\b",
        "qlib_bin/C:x",
        "qlib_bin/a ",
        "qlib_bin//a",
    ],
)
def test_archive_rejects_unsafe_paths(tmp_path, name):
    path = make_archive(tmp_path / "a.tar.gz", [(name, b"x")])
    with pytest.raises(ValueError, match="unsafe"):
        read_archive(path, SYMBOLS)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE]
)
def test_archive_rejects_nonregular_members(tmp_path, kind):
    path = make_archive(tmp_path / "a.tar.gz", [("qlib_bin/x", b"", kind)])
    with pytest.raises(ValueError, match="unsafe"):
        read_archive(path, SYMBOLS)


@pytest.mark.parametrize("duplicate", ["qlib_bin/calendars/day.txt", "qlib_bin/calendars/DAY.txt"])
def test_duplicate_case_alias_archive_member(tmp_path, duplicate):
    path = make_archive(tmp_path / "a.tar.gz", entries() + [(duplicate, b"x")])
    with pytest.raises(ValueError, match="duplicate"):
        read_archive(path, SYMBOLS)


@pytest.mark.parametrize("offset", [-1.0, 0.5, float("nan"), float("inf"), 2.0])
def test_bad_header_and_calendar_overflow(tmp_path, offset):
    members = entries()
    name, value = members[4]
    members[4] = (name, struct.pack("<f", offset) + value[4:])
    with pytest.raises(ValueError, match="offset|beyond calendar"):
        read_archive(make_archive(tmp_path / "a.tar.gz", members), SYMBOLS)


@pytest.mark.parametrize(
    "body", [b"2023-01-03\n2023-01-03\n", b"2023-01-05\n2023-01-03\n", b"2024-01-02\n", b""]
)
def test_invalid_calendar(tmp_path, body):
    members = entries()
    members[0] = (members[0][0], body)
    with pytest.raises(ValueError, match="calendar"):
        read_archive(make_archive(tmp_path / "a.tar.gz", members), SYMBOLS)


@pytest.mark.parametrize(
    "limit",
    [Limits(members=2), Limits(member_bytes=4), Limits(total_bytes=20), Limits(stream_bytes=20)],
)
def test_archive_resource_limits(tmp_path, limit):
    with pytest.raises(ValueError, match="limit"):
        read_archive(make_archive(tmp_path / "a.tar.gz", entries()), SYMBOLS, limits=limit)


def test_truncated_archive_and_damaged_gzip_crc(tmp_path):
    path = make_archive(tmp_path / "a.tar.gz", entries())
    original = path.read_bytes()
    for content in (original[:-9], original[:-8] + bytes([original[-8] ^ 1]) + original[-7:]):
        path.write_bytes(content)
        with pytest.raises((EOFError, OSError, tarfile.TarError, ValueError)):
            read_archive(path, SYMBOLS)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("open", float("inf"), "infinite"),
        ("factor", 0, "nonpositive"),
        ("factor", -1, "nonpositive"),
        ("volume", -1, "invalid volume"),
    ],
)
def test_nonfinite_and_bad_values_are_not_silently_missing(bundle, field, value, message):
    members = entries()
    name = f"qlib_bin/features/sz000001/{field}.day.bin"
    members = [
        (n, b[:4] + struct.pack("<f", value) + b[8:] if n == name else b) for n, b in members
    ]
    archive = read_archive(make_archive(bundle["root"] / "bad.tar.gz", members), SYMBOLS)
    ctx = context(bundle)
    refs = load_references(ctx, bundle["root"])
    with pytest.raises(ValueError, match=message):
        row_for_day("000001.SZ", DAYS[0], 0, archive, ctx, refs)


def test_unknown_missing_is_not_suspension_and_preipo_price_is_invalid(bundle):
    ctx = context(bundle)
    refs = load_references(ctx, bundle["root"])
    archive = read_archive(bundle["archive_path"], SYMBOLS)
    refs["primary"] = {}
    assert (
        row_for_day("000001.SZ", DAYS[1], 1, archive, ctx, refs)["record_status"]
        == "missing_reason_unknown"
    )
    refs["listings"]["000001.SZ"]["ipo_date"] = DAYS[1]
    row = row_for_day("000001.SZ", DAYS[0], 0, archive, ctx, refs)
    assert row["record_status"] == "invalid_candidate"
    assert "price_before_ipo_reference" in row["issues_json"]


def test_current_price_changes_and_cent_precision_are_recomputed(bundle):
    ctx = context(bundle)
    refs = load_references(ctx, bundle["root"])
    row = row_for_day(
        "000001.SZ", DAYS[0], 0, read_archive(bundle["archive_path"], SYMBOLS), ctx, refs
    )
    other = refs["primary"][("000001.SZ", DAYS[0])]
    assert compare_row(row, other, "test")["equal_cent_open"]
    row["restored_open"] = 10.006
    comparison = compare_row(row, other, "test")
    assert comparison["equal_cent_open"] is False and comparison["delta_open"] > 0.005


@pytest.mark.parametrize(
    "role,change,message",
    [
        ("download", lambda d: d.update(complete=False), "download status"),
        ("download", lambda d: d.update(error="failed"), "download status"),
        ("download", lambda d: d.update(content_length="9999999"), "download size"),
        ("download", lambda d: d.update(asset_id=0), "download status"),
        ("download", lambda d: d.update(completed_at="2026-10-03T14:11:57"), "observation"),
        ("release", lambda d: d.update(published_at="2024-01-01T00:00:00Z"), "release identity"),
        (
            "release",
            lambda d: d["assets"][0].update(created_at="2024-01-01T00:00:00Z"),
            "asset identity",
        ),
        ("release", lambda d: d["assets"].append(deepcopy(d["assets"][0])), "ambiguous release"),
        ("tags", lambda d: d[0]["object"].update(sha="b" * 40), "tag commit"),
    ],
)
def test_semantic_release_download_identity_failures(bundle, role, change, message):
    rewrite_document(bundle, role, change)
    with pytest.raises(ValueError, match=message):
        context(bundle)


@pytest.mark.parametrize("key", ["archive_path", "plan"])
def test_mutated_archive_or_original_plan_binding(bundle, key):
    with bundle[key].open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="digest"):
        context(bundle)


def test_source_document_hash_and_path_escape(bundle):
    config = json.loads(bundle["description"].read_text())
    config["documents"]["release"]["sha256"] = "0" * 64
    save_json(bundle["description"], config)
    with pytest.raises(ValueError, match="digest"):
        context(bundle)
    config["documents"]["release"]["path"] = "../release.json"
    save_json(bundle["description"], config)
    with pytest.raises(ValueError, match="outside"):
        context(bundle)


def test_scope_must_match_original_requests_and_cannot_extend_future(bundle):
    plan = json.loads(bundle["plan"].read_text())
    assert set(planned_symbols(plan, bundle["start"], bundle["end"])) == SYMBOLS
    for change in (
        lambda p: p["codes"].pop(),
        lambda p: p["queries"].pop(),
        lambda p: p["codes"].append(p["codes"][0]),
    ):
        altered = deepcopy(plan)
        change(altered)
        with pytest.raises(ValueError, match="plan"):
            planned_symbols(altered, bundle["start"], bundle["end"])
    with pytest.raises(ValueError, match="2023"):
        planned_symbols(plan, bundle["start"], date(2024, 1, 1))


def test_mutated_snapshot_rejected_even_with_same_rows(bundle):
    path = bundle["root"] / "bars.parquet"
    rows = pq.read_table(path).to_pylist()
    rows[0]["open"] += 1
    pq.write_table(pa.Table.from_pylist(rows), path)
    with pytest.raises(ValueError, match="digest"):
        load_references(context(bundle), bundle["root"])


def test_instrument_future_and_missing_field_and_calendar_disagreement(tmp_path):
    variants = [
        entries()[:-1],
        [
            (n, b.replace(b"2023-01-05", b"2024-01-05") if n.endswith("/all.txt") else b)
            for n, b in entries()
        ],
        [
            (n, b"2023-01-03\n2023-01-04\n" if n.endswith("day_future.txt") else b)
            for n, b in entries()
        ],
    ]
    for members in variants:
        with pytest.raises(ValueError, match="missing|future|disagree"):
            read_archive(make_archive(tmp_path / "a.tar.gz", members), SYMBOLS)


def test_membership_is_conditional_and_reports_actual_late_days(bundle):
    archive = read_archive(bundle["archive_path"], SYMBOLS)
    conditional = {
        "assumptions": ["synthetic event completeness is unverified"],
        "inputs": [],
        "states": [
            {"from": "2023-01-01", "members": ["000001.SZ"]},
            {"from": "2023-01-04", "members": ["601059.SH"]},
        ],
    }
    report = membership_report(
        archive, conditional, date(2023, 12, 31), bundle["start"], bundle["end"]
    )
    assert report["baseline"]["observed_count"] == 1 and report["equal_days"] == 1
    assert report["mismatch_days_by_conditional_event"] == {"2023-01-04": 2}
    assert not report["formal_universe"] and not report["derived_membership_written"]
    assert report["assumptions"] == conditional["assumptions"]


def test_feature_index_preserves_nan_bits_and_out_of_span(tmp_path):
    archive = read_archive(make_archive(tmp_path / "a.tar.gz", entries()), SYMBOLS)
    feature = archive["selected"][("601059.SH", "open")]
    value, loc = feature_value(feature, 0)
    assert (
        value is None and loc["value_state"] == "outside_member_span" and loc["byte_offset"] is None
    )
    value, loc = feature_value(feature, 1)
    assert value == 3 and loc["byte_offset"] == 4
    value, loc = feature_value(archive["selected"][("000001.SZ", "open")], 1)
    assert value is None and loc["raw_bits"] & 0x7F800000 == 0x7F800000


def test_actual_observation_does_not_become_historical_availability(bundle):
    result = context(bundle)
    assert result["observed_at_utc"] == datetime(2026, 10, 3, 14, 11, 57, tzinfo=timezone.utc)
    assert result["historical_publish_time"] is None and result["available_time"] is None


def replace_reference(bundle, kind, change):
    path = bundle["root"] / (kind + ".parquet")
    rows = pq.read_table(path).to_pylist()
    change(rows)
    pq.write_table(pa.Table.from_pylist(rows), path)
    manifest_path = bundle["root"] / "reference-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tables"][kind]["sha256"] = sha256_file(path)
    save_json(manifest_path, manifest)
    config = json.loads(bundle["description"].read_text())
    config["snapshots"][0]["manifest"] = ref(manifest_path)
    config["snapshots"][0]["tables"][kind] = ref(path)
    save_json(bundle["description"], config)


@pytest.mark.parametrize(
    "kind,change,message",
    [
        ("bars", lambda rows: rows.append(deepcopy(rows[0])), "duplicate/conflicting"),
        ("bars", lambda rows: rows[0].update(source="chenditc/investment_data"), "same-source"),
        ("bars", lambda rows: rows[0].update(source_adjustflag="1"), "adjusted"),
        ("bars", lambda rows: rows[0].update(event_date=date(2024, 1, 2)), "outside 2023"),
        (
            "calendar",
            lambda rows: rows[0].update(source="another_calendar"),
            "sources are ambiguous",
        ),
        (
            "securities",
            lambda rows: rows.append(dict(rows[0], ipo_date=date(2024, 1, 1))),
            "duplicate/conflicting",
        ),
    ],
)
def test_bound_but_contradictory_references_rejected(bundle, kind, change, message):
    replace_reference(bundle, kind, change)
    with pytest.raises(ValueError, match=message):
        load_references(context(bundle), bundle["root"])


def test_calendar_omission_is_reported_without_manufacturing_prices(bundle):
    replace_reference(bundle, "calendar", lambda rows: rows[0].update(is_open=False))
    result = build(**bundle)
    report = json.loads((bundle["output"] / "quality.json").read_text())
    assert result["quality_status"] == "fail"
    assert report["calendar_reference"]["extra_in_archive"] == ["2023-01-03"]
    assert result["rows"] == 6 and not result["research_eligible"]


def test_dirty_source_rejected_before_creating_output(bundle, monkeypatch):
    from ashare_lab.community import pipeline

    monkeypatch.undo()
    module_root = Path(pipeline.__file__).resolve().parent
    root = Path(
        subprocess.check_output(
            ["git", "-C", str(module_root), "rev-parse", "--show-toplevel"], encoding="utf-8"
        ).strip()
    )
    monkeypatch.setattr("ashare_lab.community.pipeline.code_identity", lambda root: {"dirty": True})
    bundle["project_root"] = root
    with pytest.raises(ValueError, match="clean source"):
        build(**bundle)
    assert not bundle["output"].exists()


def test_archive_file_parent_collision(tmp_path):
    path = make_archive(tmp_path / "a.tar.gz", entries() + [("qlib_bin/features/sz000001", b"x")])
    with pytest.raises(ValueError, match="collides"):
        read_archive(path, SYMBOLS)


def test_membership_duplicates_or_future_intervals_rejected(bundle):
    archive = read_archive(bundle["archive_path"], SYMBOLS)
    key = "qlib_bin/instruments/csi300.txt"
    archive["texts"][key] += archive["texts"][key]
    with pytest.raises(ValueError, match="duplicate"):
        membership_report(archive, None, date(2023, 12, 31), bundle["start"], bundle["end"])


@pytest.fixture
def source_worktrees(tmp_path):
    """Real isolated Git roots; main lacks community, execution worktree contains it."""
    from ashare_lab.community import pipeline

    main = tmp_path / "main"
    work = main / ".cache" / "worktrees" / "execution"
    main.mkdir()

    def git(root, *args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], encoding="utf-8", stderr=subprocess.PIPE
        ).strip()

    git(main, "init", "-b", "main")
    git(main, "config", "user.name", "Offline test fixture")
    git(main, "config", "user.email", "fixture@example.invalid")
    git(main, "config", "core.autocrlf", "false")
    (main / ".gitignore").write_text(".cache/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    git(main, "add", ".gitignore")
    git(main, "commit", "-m", "Synthetic main without community importer")
    main_sha = git(main, "rev-parse", "HEAD")
    git(main, "worktree", "add", "-b", "importer", str(work))
    package = Path(pipeline.__file__).resolve().parent.parent
    shutil.copytree(
        package, work / "src/ashare_lab", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    git(work, "add", "src")
    git(work, "commit", "-m", "Synthetic execution source")
    return {
        "main": main,
        "work": work,
        "main_sha": main_sha,
        "work_sha": git(work, "rev-parse", "HEAD"),
        "git": git,
    }


def source_child(fixture, code, *args):
    return subprocess.run(
        [sys.executable, "-c", code, *map(str, args)],
        cwd=fixture["main"],
        env=dict(os.environ, PYTHONPATH=str(fixture["work"] / "src"), PYTHONIOENCODING="utf-8"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


@pytest.mark.parametrize("explicit_wrong_root", [False, True])
def test_main_cwd_cannot_claim_main_source(source_worktrees, explicit_wrong_root):
    f = source_worktrees
    args = [
        "--archive",
        "absent.tar.gz",
        "--source",
        "source.json",
        "--input-root",
        ".",
        "--plan",
        "plan.json",
        "--start",
        "2023-01-01",
        "--end",
        "2023-12-31",
        "--output",
        "must-not-exist",
    ]
    if explicit_wrong_root:
        args += ["--project-root", str(f["main"])]
    child = source_child(f, "from ashare_lab.community.cli import main; main()", *args)
    assert child.returncode != 0
    assert "ValueError: community module belongs to a different project_root" in child.stderr
    assert not (f["main"] / "must-not-exist").exists()


def test_main_cwd_accepts_only_explicit_execution_root_identity(source_worktrees):
    f = source_worktrees
    child = source_child(
        f,
        "import json,sys; from ashare_lab.community.pipeline import _execution_source_identity; print(json.dumps(_execution_source_identity(sys.argv[1])))",
        f["work"],
    )
    assert child.returncode == 0, child.stderr
    identity = json.loads(child.stdout)
    assert identity["commit"] == f["work_sha"] != f["main_sha"]
    bound = identity["community_module"]
    assert Path(bound["path"]) == f["work"] / "src/ashare_lab/community"
    assert bound["commit"] == identity["commit"] and len(bound["files"]) >= 7
    for name, loc in bound["files"].items():
        assert loc["sha256"] == identity["files_sha256"][name] == sha256_file(f["work"] / name)
        raw = subprocess.check_output(
            ["git", "-C", str(f["work"]), "show", f"{f['work_sha']}:{name}"]
        )
        import hashlib

        assert hashlib.sha256(raw).hexdigest() == loc["commit_blob_sha256"]


def test_clean_git_status_cannot_hide_changed_module_bytes(source_worktrees):
    f = source_worktrees
    name = "src/ashare_lab/community/quality.py"
    f["git"](f["work"], "update-index", "--assume-unchanged", name)
    with (f["work"] / name).open("a", encoding="utf-8") as stream:
        stream.write("\n# Synthetic changed source hidden from status\n")
    assert f["git"](f["work"], "status", "--porcelain=v1") == ""
    child = source_child(
        f,
        "import sys; from ashare_lab.community.pipeline import _execution_source_identity; _execution_source_identity(sys.argv[1])",
        f["work"],
    )
    assert child.returncode != 0
    assert "community module differs from declared commit" in child.stderr


def test_wrong_project_root_rejected_before_code_identity(tmp_path, monkeypatch):
    from ashare_lab.community import pipeline

    def forbidden(root):
        pytest.fail("code_identity must not be called for the wrong project_root")

    monkeypatch.setattr(pipeline, "code_identity", forbidden)
    with pytest.raises(ValueError, match="community module is outside project_root"):
        pipeline._execution_source_identity(tmp_path)


def test_self_consistent_early_release_cannot_accept_later_archive_dates(bundle):
    # Every metadata identity agrees, but a Jan 4 release cannot contain Jan 5 values.
    config = json.loads(bundle["description"].read_text())
    tag = "2023-01-04"
    config["release"]["tag"] = tag
    config["release"]["published_at"] = tag + "T09:28:09Z"
    config["release"]["asset_created_at"] = tag + "T09:28:10Z"
    config["release"]["asset_updated_at"] = tag + "T09:28:43Z"
    save_json(bundle["description"], config)

    def release_change(d):
        d.update(
            tag_name=tag,
            published_at=config["release"]["published_at"],
            html_url=d["html_url"].replace("2023-12-31", tag),
        )
        d["assets"][0].update(
            created_at=config["release"]["asset_created_at"],
            updated_at=config["release"]["asset_updated_at"],
            browser_download_url=d["assets"][0]["browser_download_url"].replace("2023-12-31", tag),
        )

    rewrite_document(bundle, "release", release_change)
    rewrite_document(bundle, "tags", lambda d: d[0].update(ref="refs/tags/" + tag))
    rewrite_document(
        bundle, "download", lambda d: d.update(tag=tag, url=d["url"].replace("2023-12-31", tag))
    )
    bundle["end"] = date(2023, 1, 4)
    with pytest.raises(ValueError, match="pinned 2023-12-31 release tag"):
        build(**bundle)
    assert not bundle["output"].exists()


def test_pipeline_passes_verified_release_boundary_to_archive(bundle):
    from ashare_lab.community.pipeline import _build

    ctx = context(bundle)
    refs = load_references(ctx, bundle["root"])
    ctx["release_latest"] = date(2023, 1, 4)
    bundle["output"].mkdir()
    with pytest.raises(ValueError, match="beyond historical release"):
        _build(ctx, refs, bundle["archive_path"], bundle["output"])


def test_future_instrument_boundary_uses_verified_release_date(bundle):
    archive = read_archive(bundle["archive_path"], SYMBOLS)
    with pytest.raises(ValueError, match="future instrument"):
        membership_report(archive, None, date(2023, 1, 4), bundle["start"], date(2023, 1, 4))
