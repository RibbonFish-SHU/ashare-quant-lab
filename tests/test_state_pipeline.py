"""Bounded synthetic candidate I/O and real Git/module provenance regressions."""

from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.contracts import ContractError
from ashare_lab.state_evidence import pipeline
from ashare_lab.state_evidence.common import digest, file_ref, read_json, save_json
from ashare_lab.state_evidence.inputs import load_references
from ashare_lab.state_evidence.temporal import event
from ashare_lab.storage import read_dataset

PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path):
    config = tmp_path / "selection.json"
    save_json(config, {"synthetic": True})
    calendar_file = tmp_path / "calendar.original"
    calendar_file.write_bytes(b"synthetic retained calendar")
    original = tmp_path / "old-candidate"
    original.write_bytes(b"synthetic old candidate must remain immutable")
    days = [date(2023, 6, n) for n in (15, 16, 17, 19, 20, 21, 22, 26)]
    calendar = {
        d: {
            "event_date": d,
            "is_open": d.day not in {17, 22},
            "raw_path": "synthetic-calendar",
            "raw_sha256": "0" * 64,
            "row_number": i,
            "source": "baostock",
            "exchange_scope": "SSE_SZSE_provider_joint",
        }
        for i, d in enumerate(days)
    }
    events = [
        event(
            symbol="688065.SH",
            kind=kind,
            effective_date=d,
            effective_session=session,
            source="synthetic",
            source_url="synthetic://not-a-real-source",
            locator_json='{"synthetic":true}',
            observed_at_utc="2026-10-03T00:00:00+00:00",
        )
        for kind, d, session in [
            ("halt", "2023-06-15", "afternoon_open"),
            ("resume", "2023-06-26", "morning_open"),
        ]
    ]
    primary = {}
    for d in days:
        if not calendar[d]["is_open"]:
            continue
        volume = 353426 if d.day == 15 else 8046605 if d.day == 26 else 0
        primary[("688065.SH", d)] = {
            "symbol": "688065.SH",
            "event_date": d,
            "source_trading_active": d.day == 26,
            "source_is_st": False,
            "volume_shares": volume,
            "raw_values_json": "synthetic",
        }
    c = {
        "symbols": ["000002.SZ", "688065.SH"],
        "references": [file_ref(config), file_ref(calendar_file), file_ref(original)],
        "paths": {"calendar": calendar_file},
        "config": {"calendar": {"table": "calendar"}},
        "start": date(2023, 6, 15),
        "end": date(2023, 6, 26),
    }
    r = {
        "primary": primary,
        "community": {("000002.SZ", date(2023, 6, 15)): {"raw_close": None}},
        "calendar": calendar,
        "daily_raw_checked": 6,
        "counts": {"synthetic": 6},
    }
    return (
        {
            "context": c,
            "reference": r,
            "events": events,
            "name_quality": {},
            "event_conflicts": [],
            "rejected_sources": [],
            "tools": [],
        },
        config,
        original,
    )


def test_daily_report_distinguishes_missing_price_missing_status_and_selected_event(prepared):
    p, _, _ = prepared
    before = deepcopy(p)
    rows = list(pipeline.daily_rows(p))
    q = pipeline.quality_report(p, rows)
    assert p == before
    assert len(rows) == 12
    assert q["classification_counts"]["full_day_halt_reference"] == 4
    assert q["classification_counts"]["partial_day_halt_reference"] == 1
    assert q["partial_day_with_positive_volume"][0]["source_volume_shares"] == 353426
    assert q["source_status_unknown_rows"] == 6
    assert q["community_price_missing_rows"] == 1
    unknown = [r for r in rows if r["symbol"] == "000002.SZ"]
    assert all(
        r["classification"] == "no_selected_halt_reference" and r["can_trade"] is None
        for r in unknown
    )
    assert q["coverage"][0]["name_unknown_days"] == 6
    assert len(q["coverage"][0]["days_without_selected_trading_evidence"]) == 6
    assert q["certified_state_rows"] == 0 and q["status"] == "unverified"


def source_stub(monkeypatch, p, config):
    source = {
        "commit": "synthetic-git-bound-in-other-tests",
        "dirty": False,
        "files_sha256": {config.name: digest(config)},
        "state_module": {"files": {config.name: {"sha256": digest(config)}}},
    }
    monkeypatch.setattr(pipeline, "execution_identity", lambda _: source)
    monkeypatch.setattr(pipeline, "prepare", lambda *args: p)


def test_candidate_roundtrip_and_formal_rejection_preserve_old_bytes(
    tmp_path, prepared, monkeypatch
):
    p, config, original = prepared
    expected = digest(original)
    source_stub(monkeypatch, p, config)
    output = tmp_path / "new-candidate"
    args = dict(
        description=config,
        input_root=tmp_path,
        pdftotext="tested separately",
        output=output,
        project_root=tmp_path,
    )
    result = pipeline.build(**args)
    assert result["status"] == "built_candidate" and result["research_eligible"] is False
    assert result["canonical_reader_rejected"] is True
    assert result["tables"]["daily_references"]["rows"] == 12
    assert result["inputs_unchanged"] is True and digest(original) == expected
    assert pq.read_table(output / "events.parquet").num_rows == 2
    assert all(t["duckdb_roundtrip"] and t["parquet_roundtrip"] for t in result["tables"].values())
    with pytest.raises(ContractError):
        read_dataset(output)
    with pytest.raises(ValueError, match="already exists"):
        pipeline.build(**args)


def test_inconsistent_candidate_preserves_failure_and_original(tmp_path, prepared, monkeypatch):
    p, config, original = prepared
    before = digest(original)
    p["reference"]["primary"][("688065.SH", date(2023, 6, 16))]["volume_shares"] = 1
    source_stub(monkeypatch, p, config)
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="consistency failed"):
        pipeline.build(
            description=config,
            input_root=tmp_path,
            pdftotext="unused",
            output=output,
            project_root=tmp_path,
        )
    assert read_json(output / "manifest.json")["status"] == "failed"
    assert read_json(output / "quality.json")["status"] == "fail"
    assert digest(original) == before and (output / "daily_references.parquet").exists()


def test_changed_old_input_prevents_success(tmp_path, prepared, monkeypatch):
    p, config, original = prepared
    source_stub(monkeypatch, p, config)
    original.write_bytes(b"changed")
    output = tmp_path / "changed-input-failure"
    with pytest.raises(ValueError, match="input changed"):
        pipeline.build(
            description=config,
            input_root=tmp_path,
            pdftotext="unused",
            output=output,
            project_root=tmp_path,
        )
    assert read_json(output / "manifest.json")["status"] == "failed"


def test_selected_config_must_be_committed(tmp_path, prepared, monkeypatch):
    p, config, _ = prepared
    source_stub(monkeypatch, p, config)
    config.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="selection config is not bound"):
        pipeline.build(
            description=config,
            input_root=tmp_path,
            pdftotext="unused",
            output=tmp_path / "out",
            project_root=tmp_path,
        )
    assert not (tmp_path / "out").exists()


@pytest.fixture
def source_checkout(tmp_path):
    # Independent tiny Git roots mimic main and its nested execution worktree without relying on cwd depth.
    outer = tmp_path / "main"
    root = outer / ".cache" / "worktrees" / "state"
    root.mkdir(parents=True)
    shutil.copytree(
        PROJECT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(outer)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "synthetic source identity",
        ],
        check=True,
        capture_output=True,
    )
    return outer, root


def identity_process(cwd, root, claimed):
    env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONIOENCODING="utf-8")
    script = "from ashare_lab.state_evidence.common import execution_identity; import json,sys; print(json.dumps(execution_identity(sys.argv[1])))"
    return subprocess.run(
        [sys.executable, "-c", script, str(claimed)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_main_cwd_wrong_project_root_rejected_before_code_identity(source_checkout):
    outer, root = source_checkout
    failed = identity_process(outer, root, outer)
    assert failed.returncode != 0 and "different project_root" in failed.stderr
    # Correct explicit root succeeds even while launched from main cwd.
    result = identity_process(outer, root, root)
    assert result.returncode == 0, result.stderr
    source = json.loads(result.stdout)
    assert source["state_module"]["path"] == str(root / "src" / "ashare_lab" / "state_evidence")
    assert "src/ashare_lab/state_evidence/pipeline.py" in source["state_module"]["files"]


def test_source_digest_must_match_commit_even_when_git_status_hides_edit(source_checkout):
    outer, root = source_checkout
    relative = "src/ashare_lab/state_evidence/pipeline.py"
    subprocess.run(
        ["git", "-C", str(root), "update-index", "--assume-unchanged", relative],
        check=True,
        capture_output=True,
    )
    with (root / relative).open("a", encoding="utf-8") as f:
        f.write("\n# uncommitted source\n")
    result = identity_process(outer, root, root)
    assert result.returncode != 0 and "source differs from commit" in result.stderr


def test_build_guard_does_not_inspect_inputs_on_wrong_root(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(pipeline, "prepare", lambda *a: called.append(True))
    with pytest.raises(ValueError, match="outside project_root"):
        pipeline.build(
            description="missing",
            input_root="missing",
            pdftotext="missing",
            output=tmp_path / "out",
            project_root=tmp_path,
        )
    assert called == [] and not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "change", ["valid", "duplicate", "source", "outside_year", "invented_status"]
)
def test_retained_candidates_are_read_only_references_with_validated_scope(tmp_path, change):
    d = date(2023, 6, 15)
    raw = tmp_path / "calendar.raw.json"
    save_json(
        raw,
        {
            "status": "complete",
            "error_code": "0",
            "source": "baostock",
            "sdk": "0.9.4",
            "api": "query_trade_dates",
            "fields": ["calendar_date", "is_trading_day"],
            "rows": [[str(d), "1"]],
        },
    )
    cal = tmp_path / "calendar.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "event_date": d,
                    "is_open": True,
                    "source": "baostock",
                    "sdk": "0.9.4",
                    "exchange_scope": "SSE_SZSE_provider_joint",
                    "raw_sha256": digest(raw),
                    "row_number": 0,
                }
            ]
        ),
        cal,
    )
    cm = tmp_path / "cal.manifest.json"
    save_json(
        cm,
        {
            "research_eligible": False,
            "data_kind": "real_candidate",
            "schema_version": "baostock-candidate-v2",
            "status": "complete_candidates_research_blocked",
            "tables": {"calendar": {"sha256": digest(cal)}},
        },
    )
    row = {
        "symbol": "000001.SZ",
        "event_date": d,
        "record_status": "candidate_execution_unknown",
        "observed_at_utc": None,
        "available_time": None,
        "source_trading_active": None,
        "provider": "eastmoney",
        "source_is_st": None,
        "volume_raw": "123",
        "volume_source_unit": "unknown",
        "raw_path": "fixture",
        "raw_sha256": "0" * 64,
        "row_number": 0,
        "raw_values_json": "synthetic",
        "query_id": "synthetic",
    }
    if change == "source":
        row["provider"] = "another"
    if change == "outside_year":
        row["event_date"] = date(2024, 1, 2)
    if change == "invented_status":
        row["source_trading_active"] = True
    bars = tmp_path / "bars.parquet"
    pq.write_table(pa.Table.from_pylist([row, row] if change == "duplicate" else [row]), bars)
    bm = tmp_path / "bars.manifest.json"
    save_json(
        bm,
        {
            "research_eligible": False,
            "data_kind": "real_candidate",
            "schema_version": "public-bars-candidate-v1",
            "files": {"bars.parquet": digest(bars)},
        },
    )
    context = {
        "paths": {"raw": raw, "cal": cal, "cm": cm, "bars": bars, "bm": bm},
        "symbols": ["000001.SZ"],
        "start": d,
        "end": d,
        "config": {
            "candidates": [{"table": "bars", "manifest": "bm", "kind": "eastmoney"}],
            "calendar": {"table": "cal", "manifest": "cm", "raw": "raw"},
            "daily_raw_checks": [],
        },
    }
    before = {p: digest(p) for p in context["paths"].values()}
    if change == "valid":
        result = load_references(context)
        assert result["primary"][("000001.SZ", d)]["source_trading_active"] is None
        assert result["missing_bao_symbols"] == {"000001.SZ"}
    else:
        with pytest.raises(ValueError):
            load_references(context)
    assert before == {p: digest(p) for p in context["paths"].values()}
