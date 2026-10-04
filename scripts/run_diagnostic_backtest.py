"""Run the frozen 2023 real-candidate diagnostic; never promote candidate eligibility."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import time
import traceback

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab import diagnostic_backtest


def save_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], encoding="utf-8", timeout=15
    ).strip()


def source_identity(root, config_path):
    require(not git(root, "status", "--porcelain"), "commit the stable batch before running")
    module = Path(diagnostic_backtest.__file__).resolve()
    require(module.is_relative_to(root / "src"), "diagnostic module loaded from another checkout")
    files = [Path(__file__).resolve(), module, config_path, root / "uv.lock"]
    tracked = set(git(root, "ls-files").splitlines())
    for path in files:
        require(path.relative_to(root).as_posix() in tracked, f"untracked source/config: {path}")
    return {
        "commit": git(root, "rev-parse", "HEAD"),
        "dirty": False,
        "files_sha256": {p.relative_to(root).as_posix(): digest(p) for p in files},
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "pandas", "pyarrow", "matplotlib")
        },
    }


def load_inputs(root, config):
    require(config["data_kind"] == "real_candidate", "only explicit real candidates allowed")
    require(config["research_eligible"] is False, "diagnostic cannot certify research eligibility")
    require(config["date_start"] == "2023-01-03", "this runner is restricted to frozen 2023")
    require(config["date_end"] == "2023-12-29", "this runner cannot read the sealed interval")
    require(config["open_column"] == "raw_open", "expected source-native adjusted opening price")
    require(config["close_column"] == "raw_close", "expected source-native adjusted closing price")
    columns = {
        "quotes": [
            "symbol",
            "event_date",
            "raw_open",
            "raw_close",
            "available_time",
            "research_eligible",
        ],
        "membership": [
            "symbol",
            "event_date",
            "conditional_member",
            "available_time",
            "research_eligible",
        ],
        "states": ["symbol", "event_date", "classification", "research_eligible"],
    }
    frames, references = {}, {}
    for name, spec in config["inputs"].items():
        path = (root / spec["path"]).resolve()
        require(path.is_relative_to(root), "input path outside the project")
        actual = digest(path)
        require(actual == spec["sha256"], f"input changed: {name}")
        frame = pq.read_table(path, columns=columns[name]).to_pandas()
        frame["event_date"] = frame.event_date.map(lambda value: value.isoformat())
        require(not frame.duplicated(["event_date", "symbol"]).any(), f"duplicate keys: {name}")
        require(frame.research_eligible.eq(False).all(), f"unexpected eligibility: {name}")
        require(
            frame.event_date.between(config["date_start"], config["date_end"]).all(),
            f"date outside frozen diagnostic interval: {name}",
        )
        if "available_time" in frame:
            require(frame.available_time.isna().all(), f"changed availability semantics: {name}")
        frames[name] = frame.set_index(["event_date", "symbol"]).sort_index()
        references[name] = {**spec, "bytes": path.stat().st_size, "rows": len(frame)}
    quotes, members, states = (frames[name] for name in ("quotes", "membership", "states"))
    dates = sorted(quotes.index.get_level_values(0).unique())
    symbols = sorted(quotes.index.get_level_values(1).unique())
    index = pd.MultiIndex.from_product([dates, symbols], names=["event_date", "symbol"])
    require(len(dates) == config["expected_sessions"], "session count changed")
    require(len(symbols) == config["expected_symbols"], "symbol count changed")
    require(
        dates[0] == config["date_start"] and dates[-1] == config["date_end"], "date bounds changed"
    )
    require(quotes.index.equals(index), "quotes must cover the full symbol/session grid")
    require(states.index.equals(index), "states must align exactly with the quote grid")
    require(members.index.isin(index).all(), "membership contains an unpriced date/security")
    require(members.conditional_member.eq(True).all(), "expected member-only candidate rows")
    count = members.groupby(level=0).size().reindex(dates)
    require(count.eq(config["expected_members_per_day"]).all(), "daily member count changed")
    shape = (len(dates), len(symbols))
    member_mask = (
        members.conditional_member.reindex(index, fill_value=False).to_numpy().reshape(shape)
    )
    halt_mask = states.classification.eq("full_day_halt_reference").to_numpy().reshape(shape)
    opening = quotes.raw_open.to_numpy(dtype=float).reshape(shape)
    closing = quotes.raw_close.to_numpy(dtype=float).reshape(shape)
    summary = {
        "references": references,
        "first_date": dates[0],
        "last_date": dates[-1],
        "sessions": len(dates),
        "symbols": len(symbols),
        "conditional_member_union": int(member_mask.any(axis=0).sum()),
        "conditional_member_days": int(member_mask.sum()),
        "missing_open": int(np.isnan(opening).sum()),
        "missing_close": int(np.isnan(closing).sum()),
        "referenced_full_day_halts": int(halt_mask.sum()),
        "member_halt_days": int((member_mask & halt_mask).sum()),
        "classification_counts": {
            k: int(v) for k, v in states.classification.value_counts().items()
        },
        "data_kind": "real_candidate",
        "research_eligible": False,
        "available_time": None,
        "raw_candidate_files_modified": False,
    }
    return dates, symbols, opening, closing, member_mask, halt_mask, summary


def export_result(output, result):
    summary = {}
    for key, value in result.items():
        if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
            pq.write_table(
                pa.Table.from_pylist(value), output / f"{key}.parquet", compression="zstd"
            )
            summary[f"{key}_rows"] = len(value)
        else:
            summary[key] = value
    save_json(output / "summary.json", summary)
    return summary


def plot_results(output, results, initial_cash):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, height_ratios=[2, 1])
    colors = {"gross": "#64748b", "base": "#1769aa", "higher": "#ca4c31"}
    labels = {"gross": "Zero cost", "base": "Base costs", "higher": "Higher costs"}
    for name, result in results.items():
        daily = pd.DataFrame(result["daily"])
        dates = pd.to_datetime(daily.date)
        nav = daily.nav / initial_cash
        peak = np.maximum.accumulate(np.maximum(nav.to_numpy(), 1.0))
        drawdown = nav / peak - 1
        axes[0].plot(
            dates, nav, label=labels.get(name, name), color=colors.get(name), linewidth=1.7
        )
        axes[1].plot(dates, drawdown * 100, color=colors.get(name), linewidth=1.4)
    axes[0].axhline(1, color="#999999", linewidth=0.7)
    axes[0].set_ylabel("NAV / initial capital")
    axes[0].legend(frameon=False)
    axes[1].set_ylabel("Drawdown (%)")
    for axis in axes:
        axis.grid(alpha=0.2)
    figure.suptitle("2023 conditional equal-weight diagnostic | next open | every 5 sessions")
    figure.text(
        0.06,
        0.015,
        "Real candidate adjusted prices; unverified historical membership/availability. "
        "Simplified fills; no certified CSI300 benchmark.",
        fontsize=8,
    )
    figure.tight_layout(rect=(0, 0.04, 1, 0.96))
    figure.savefig(output / "nav_and_drawdown.png", dpi=180)
    plt.close(figure)


def run(config_path, output):
    root = Path(__file__).resolve().parents[1]
    config_path = config_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    identity = source_identity(root, config_path)
    require(not output.exists(), "output already exists; keep previous and failed runs")
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    manifest = {
        "status": "running",
        "data_kind": "real_candidate",
        "research_eligible": False,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": identity,
        "config_sha256": digest(config_path),
        "config_frozen_before_returns": True,
        "online_requests": 0,
        "gpu_used": False,
        "output": str(output),
    }
    save_json(output / "manifest.json", manifest)
    (output / "frozen_config.json").write_bytes(config_path.read_bytes())
    try:
        dates, symbols, opening, closing, members, halts, input_summary = load_inputs(root, config)
        save_json(output / "input_summary.json", input_summary)
        np.savez_compressed(
            output / "diagnostic_inputs.npz",
            dates=np.array(dates),
            symbols=np.array(symbols),
            adjusted_open=opening,
            adjusted_close=closing,
            membership=members,
            full_day_suspended=halts,
        )
        results, summaries = {}, {}
        for scenario in config["scenarios"]:
            name = scenario["scenario_name"]
            require(name in {"gross", "base", "higher"} and name not in results, "invalid scenario")
            scenario_output = output / name
            scenario_output.mkdir()
            result = diagnostic_backtest.run_diagnostic_backtest(
                dates,
                symbols,
                opening,
                closing,
                members,
                halts,
                config={**config["strategy"], **scenario},
            )
            results[name] = result
            summaries[name] = export_result(scenario_output, result)
        plot_results(output, results, config["strategy"]["initial_cash"])
        manifest.update(
            status="completed_diagnostic_not_certified_research",
            metrics={name: result["metrics"] for name, result in results.items()},
            input_summary=input_summary,
        )
        save_json(output / "scenario_summaries.json", summaries)
        # Confirm the immutable candidates still have their exact recorded contents.
        for spec in config["inputs"].values():
            require(digest(root / spec["path"]) == spec["sha256"], "input changed during execution")
        manifest["files"] = {
            path.relative_to(output).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": digest(path),
            }
            for path in sorted(output.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        }
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        (output / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        manifest["elapsed_seconds"] = time.perf_counter() - started
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(output / "manifest.json", manifest)
    print(json.dumps({"output": str(output), "metrics": manifest["metrics"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/diagnostic-equal-weight-2023.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.config, args.output.resolve())
