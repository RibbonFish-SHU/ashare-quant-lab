"""The execution smoke only succeeds when its ledger checks pass, preserving failure evidence."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def smoke_script():
    path = Path(__file__).resolve().parents[1] / "scripts/run_execution_smoke.py"
    spec = importlib.util.spec_from_file_location("execution_smoke_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_run(output_root):
    runs = list(output_root.iterdir())
    assert len(runs) == 1
    output = runs[0]
    return output, json.loads((output / "manifest.json").read_text(encoding="utf-8"))


def test_smoke_persists_explicit_inputs_and_verifiable_ledger(smoke_script, tmp_path):
    assert smoke_script.main(["--output-root", str(tmp_path)]) == 0
    output, manifest = read_run(tmp_path)
    assert manifest["status"] == "passed" and manifest["research_eligible"] is False
    assert manifest["data_kind"] == "synthetic"
    assert manifest["source"]["files_sha256"]["src/ashare_lab/execution/core.py"]
    assert set(manifest["artifacts_sha256"]) == {
        "inputs.json",
        "orders.json",
        "ledger.json",
        "result.json",
    }
    for name, expected in manifest["artifacts_sha256"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == expected
    inputs = json.loads((output / "inputs.json").read_text(encoding="utf-8"))
    assert inputs["costs"]["transfer_fee_rate"] == "0.00001"
    assert inputs["action_guard"]["known_no_actions"] is True
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["ledger_count"] == 3 and result["cash"] == "2481.56"
    assert result["second_session_rejections"][0]["filled_shares"] == 0


def test_smoke_failure_cannot_be_reported_as_passed(smoke_script, monkeypatch, tmp_path):
    def broken_fixture(output):
        (output / "ledger.json").write_text("[]\n", encoding="utf-8")
        raise AssertionError("cash does not reconcile")

    monkeypatch.setattr(smoke_script, "run_fixture", broken_fixture)
    assert smoke_script.main(["--output-root", str(tmp_path)]) == 1
    output, manifest = read_run(tmp_path)
    assert manifest["status"] == "failed"
    assert manifest["error"] == "AssertionError: cash does not reconcile"
    assert manifest["research_eligible"] is False
    assert "traceback.txt" in manifest["artifacts_sha256"]
    assert (output / "ledger.json").exists() and not (output / "result.json").exists()


def test_smoke_rejects_identity_from_another_worktree(smoke_script, tmp_path):
    output_root = tmp_path / "runs"
    assert (
        smoke_script.main(["--project-root", str(tmp_path), "--output-root", str(output_root)]) == 1
    )
    output, manifest = read_run(output_root)
    assert manifest["status"] == "failed"
    assert "project-root differs" in manifest["error"]
    assert not (output / "inputs.json").exists()
