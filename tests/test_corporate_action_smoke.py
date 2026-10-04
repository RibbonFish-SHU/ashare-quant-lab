"""Persist successes/failures and bind the synthetic fixture to its actual source."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from ashare_lab.runtime import code_identity
import ashare_lab.state_evidence.common as identity_module


@pytest.fixture
def smoke():
    path = Path(__file__).resolve().parents[1] / "scripts/run_corporate_action_smoke.py"
    spec = importlib.util.spec_from_file_location("corporate_action_smoke_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_run(root):
    directories = list(root.iterdir())
    assert len(directories) == 1
    output = directories[0]
    return output, json.loads((output / "manifest.json").read_text(encoding="utf-8"))


def test_smoke_persists_inputs_journal_checkpoint_and_source(smoke, tmp_path, monkeypatch):
    # This unit test permits in-progress source; the real entry point requires a clean commit.
    monkeypatch.setattr(smoke, "source_identity", code_identity)
    assert smoke.main(["--output-root", str(tmp_path)]) == 0
    output, manifest = read_run(tmp_path)
    assert manifest["status"] == "passed" and manifest["research_eligible"] is False
    assert manifest["complete_portfolio_valuation_available"] is False
    assert manifest["source"]["files_sha256"]["src/ashare_lab/corporate_actions/core.py"]
    assert set(manifest["artifacts_sha256"]) == {
        "inputs.json",
        "trace.json",
        "checkpoint.json",
        "checkpoint.sha256",
        "journal.json",
        "final_state.json",
        "result.json",
    }
    for name, digest in manifest["artifacts_sha256"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["cash"] == "3125.00" and result["tax_remitted"] == "25.00"
    assert result["holdings"] == {} and result["journal_entries"] == 7
    assert result["trace_entries"] == 11
    inputs = json.loads((output / "inputs.json").read_text(encoding="utf-8"))
    assert inputs["action"]["cash"]["gross_cash_per_eligible_share"] == "1.25"
    assert inputs["action"]["cash"]["ex_price_deduction_per_total_share"] == "1.20"


def test_failure_keeps_partial_trace_and_error(smoke, tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "source_identity", code_identity)

    def broken(output):
        (output / "trace.json").write_text('[{"event":"before_failure"}]', encoding="utf-8")
        raise AssertionError("cash mismatch")

    monkeypatch.setattr(smoke, "run_fixture", broken)
    assert smoke.main(["--output-root", str(tmp_path)]) == 1
    output, manifest = read_run(tmp_path)
    assert manifest["status"] == "failed" and manifest["error"] == "AssertionError: cash mismatch"
    assert manifest["research_eligible"] is False
    assert set(manifest["artifacts_sha256"]) == {"trace.json", "traceback.txt"}
    assert not (output / "result.json").exists()


def test_wrong_project_root_fails_before_inputs(smoke, tmp_path):
    runs = tmp_path / "runs"
    assert smoke.main(["--project-root", str(tmp_path), "--output-root", str(runs)]) == 1
    output, manifest = read_run(runs)
    assert "another project-root/worktree" in manifest["error"]
    assert not (output / "inputs.json").exists()


def test_wrong_loaded_module_root_fails_before_inputs(smoke, tmp_path, monkeypatch):
    monkeypatch.setattr(identity_module, "__file__", str(tmp_path / "foreign-source/common.py"))
    assert smoke.main(["--output-root", str(tmp_path)]) == 1
    output, manifest = read_run(tmp_path)
    assert "state module is outside project_root" in manifest["error"]
    assert not (output / "inputs.json").exists()


def test_dirty_source_is_rejected(smoke, tmp_path, monkeypatch):
    monkeypatch.setattr(identity_module, "code_identity", lambda _: {"dirty": True})
    assert smoke.main(["--output-root", str(tmp_path)]) == 1
    output, manifest = read_run(tmp_path)
    assert "stable clean source commit required" in manifest["error"]
    assert not (output / "inputs.json").exists()


def test_source_changed_during_run_fails_and_preserves_result(smoke, tmp_path, monkeypatch):
    versions = iter([{"commit": "before"}, {"commit": "after"}])
    monkeypatch.setattr(smoke, "source_identity", lambda _: next(versions))
    assert smoke.main(["--output-root", str(tmp_path)]) == 1
    output, manifest = read_run(tmp_path)
    assert manifest["status"] == "failed" and "source identity changed" in manifest["error"]
    assert (output / "result.json").exists() and (output / "traceback.txt").exists()


def test_repeated_smokes_never_overwrite_previous_runs(smoke, tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "source_identity", code_identity)
    assert smoke.main(["--output-root", str(tmp_path)]) == 0
    original = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert smoke.main(["--output-root", str(tmp_path)]) == 0
    assert len(list(tmp_path.iterdir())) == 2
    assert all((tmp_path / path).read_bytes() == content for path, content in original.items())
