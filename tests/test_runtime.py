from dataclasses import replace
import json
from pathlib import Path

import pytest

from ashare_lab.cli import main, smoke
from ashare_lab.config import ConfigError, load_config
from ashare_lab.devices import select_device

ROOT = Path(__file__).resolve().parents[1]


def test_config_resolves_from_project_not_shell_directory():
    config = load_config(ROOT / "configs/smoke.toml", project_root=ROOT)
    assert config.output_root == ROOT / "artifacts/experiments"


@pytest.mark.parametrize(
    "replacement,match",
    [
        ("seed = -1", "seed"),
        ('output_root = "../escape"', "artifacts"),
        ('device = "cuda:0"', "UUID"),
        ('data_kind = "real"', "synthetic"),
    ],
)
def test_bad_config_is_specific(tmp_path, replacement, match):
    content = (ROOT / "configs/smoke.toml").read_text(encoding="utf-8")
    key = replacement.split(" = ")[0]
    content = "\n".join(
        replacement if line.startswith(key + " = ") else line for line in content.splitlines()
    )
    file = tmp_path / "bad.toml"
    file.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=match):
        load_config(file, project_root=ROOT)


def test_cli_config_error_exit(tmp_path, capsys):
    config = tmp_path / "bad.toml"
    config.write_text("unexpected = 1", encoding="utf-8")
    assert main(["smoke", "--config", str(config)]) == 1
    assert "configuration missing=" in capsys.readouterr().err


def test_gpu_gate_checks_permission_before_any_inventory(monkeypatch):
    def forbidden():
        pytest.fail("inventory must not run before allocation is confirmed")

    monkeypatch.setattr("ashare_lab.devices.inventory", forbidden)
    with pytest.raises(RuntimeError, match="allocation/booking"):
        select_device("GPU-example")


def test_busy_gpu_rejected_and_uuid_selected(monkeypatch):
    state = {
        "devices": [{"uuid": "GPU-test", "utilization_pct": "10", "memory_used_mib": "9"}],
        "compute_processes": [],
    }
    monkeypatch.setattr("ashare_lab.devices.inventory", lambda: state)
    with pytest.raises(RuntimeError, match="busy"):
        select_device("GPU-test", allocation_confirmed=True)
    state["devices"][0]["utilization_pct"] = "0"
    assert select_device("GPU-test", allocation_confirmed=True)["selected"] == "GPU-test"
    select_device("cpu")


def test_failed_run_preserves_source_config_and_traceback(tmp_path):
    config = load_config(ROOT / "configs/smoke.toml", project_root=ROOT)
    config = replace(config, output_root=tmp_path, device="GPU-example")
    with pytest.raises(RuntimeError, match="allocation/booking"):
        smoke(config, ROOT)
    manifests = list(tmp_path.glob("synthetic/*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert len(manifest["source"]["commit"]) == 40
    assert len(manifest["source"]["source_sha256"]) == 64
    assert (manifests[0].parent / "traceback.txt").exists()
