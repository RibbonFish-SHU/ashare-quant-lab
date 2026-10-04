"""Run the fixed CPU MLP baseline against the matched 2023 equal-weight control."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time
import traceback

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab import alpha158_diagnostic, mlp_diagnostic
from ashare_lab.diagnostic_backtest import run_diagnostic_backtest
from run_alpha158_diagnostic import information_coefficients, select_top_k
from run_diagnostic_backtest import (
    digest,
    export_result,
    load_inputs,
    require,
    save_json,
    source_identity,
)


def plot_comparison(output, results, capital):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, height_ratios=[2, 1])
    for variant, label, color in (
        ("equal_weight", "300-stock equal weight | base costs", "#64748b"),
        ("mlp_top30", "Alpha158 + fixed MLP top 30 | base costs", "#1769aa"),
    ):
        daily = pd.DataFrame(results[variant]["base"]["daily"])
        dates = pd.to_datetime(daily.date)
        nav = daily.nav / capital
        axes[0].plot(dates, nav, label=label, color=color, linewidth=1.7)
        axes[1].plot(dates, (nav / nav.cummax() - 1) * 100, color=color, linewidth=1.5)
    axes[0].axhline(1, color="#999999", linewidth=0.7)
    axes[0].set_ylabel("NAV / initial capital")
    axes[0].legend(frameon=False)
    axes[1].set_ylabel("Drawdown (%)")
    for axis in axes:
        axis.grid(alpha=0.2)
    figure.suptitle("2023 H2 fixed MLP diagnostic | same windows, execution and costs")
    figure.text(
        0.06,
        0.015,
        "Previously inspected development year; adjusted-unit candidates. Not a blind final test.",
        fontsize=8,
    )
    figure.tight_layout(rect=(0, 0.04, 1, 0.96))
    figure.savefig(output / "mlp_vs_equal_weight.png", dpi=180)
    plt.close(figure)


def run(config_path, output):
    root = Path(__file__).resolve().parents[1]
    config_path = config_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    require(config["research_eligible"] is False, "diagnostic cannot certify candidate data")
    base_path = root / config["backtest_inputs_and_costs"]
    normalized = base_path.read_bytes().replace(b"\r\n", b"\n")
    require(
        hashlib.sha256(normalized).hexdigest() == config["backtest_config_sha256_lf"],
        "equal-weight assumptions changed",
    )
    base_config = json.loads(normalized)
    require(config["model"] == mlp_diagnostic.MODEL_PARAMETERS, "MLP parameters changed")
    require(
        config["features"]["warmup_prior_sessions"] == mlp_diagnostic.WARMUP_SESSIONS,
        "warmup changed",
    )
    identity = source_identity(root, config_path)
    for module in (alpha158_diagnostic, mlp_diagnostic):
        path = Path(module.__file__).resolve()
        require(path.is_relative_to(root / "src"), "module loaded from another checkout")
        identity["files_sha256"][path.relative_to(root).as_posix()] = digest(path)
    identity["packages"].update(
        {name: importlib.metadata.version(name) for name in ("pyqlib", "torch")}
    )
    require(not output.exists(), "choose a new output directory")
    output.mkdir(parents=True)
    started = time.perf_counter()
    manifest = {
        "status": "running",
        "source": identity,
        "data_kind": "real_candidate",
        "research_eligible": False,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_config_sha256": digest(config_path),
        "online_requests": 0,
        "gpu_used": False,
        "sealed_2024_2025_prices_read": False,
        "planned_model_fits": len(config["windows"]),
    }
    (output / "frozen_model_config.json").write_bytes(config_path.read_bytes())
    save_json(output / "manifest.json", manifest)
    try:
        dates, symbols, opening, closing, members, halts, input_summary = load_inputs(
            root, base_config
        )
        save_json(output / "input_summary.json", input_summary)
        columns = [f"raw_{name}" for name in config["features"]["raw_fields"]]
        quote_path = root / base_config["inputs"]["quotes"]["path"]
        quote = pq.read_table(quote_path, columns=["event_date", "symbol", *columns]).to_pandas()
        quote["event_date"] = quote.event_date.map(lambda value: value.isoformat())
        quote = quote.set_index(["event_date", "symbol"]).sort_index()
        expected = pd.MultiIndex.from_product([dates, symbols], names=["event_date", "symbol"])
        require(quote.index.equals(expected), "MLP raw grid does not align")
        raw_fields = {
            field: quote[f"raw_{field}"].to_numpy(dtype=float).reshape(opening.shape)
            for field in config["features"]["raw_fields"]
        }
        print("Computing the same native 158 features for the fixed MLP.", flush=True)
        feature_started = time.perf_counter()
        feature_result = alpha158_diagnostic.build_alpha158_features(
            dates, symbols, raw_fields, provider_path=output / "qlib_candidate_provider"
        )
        features = feature_result["features"]
        manifest["feature_seconds"] = time.perf_counter() - feature_started
        require(features.shape == (*opening.shape, 158), "feature dimensions changed")
        save_json(
            output / "features_metadata.json",
            {
                "names": feature_result["feature_names"],
                "metadata": feature_result["metadata"],
                "shape": list(features.shape),
                "nan_fraction": float(np.isnan(features).mean()),
            },
        )
        np.save(output / "features.npy", features, allow_pickle=False)
        print("Fitting the two fixed CPU MLP windows; no tuning.", flush=True)
        model_started = time.perf_counter()
        fitted = mlp_diagnostic.train_mlp_diagnostic(
            dates,
            symbols,
            features,
            opening,
            closing,
            members,
            windows=config["windows"],
            model_dir=output / "models",
        )
        manifest["model_seconds"] = time.perf_counter() - model_started
        predictions = fitted["predictions"]
        require(fitted["metadata"]["model_parameters"] == config["model"], "fitted MLP differs")
        save_json(
            output / "training_evidence.json",
            {
                "windows": fitted["training_evidence"],
                "metadata": fitted["metadata"],
                "label_metadata": fitted["label_metadata"],
            },
        )
        np.savez_compressed(
            output / "predictions_labels_returns.npz",
            dates=np.array(dates),
            symbols=np.array(symbols),
            predictions=predictions,
            labels=fitted["labels"],
            raw_returns=fitted["raw_returns"],
        )
        np.savez_compressed(output / "training_masks.npz", **fitted["training_masks"])
        start = dates.index(config["portfolio"]["schedule_origin"])
        require(dates[start] == config["portfolio"]["schedule_origin"], "schedule origin changed")
        selected = select_top_k(
            predictions[start:],
            closing[start:],
            members[start:],
            symbols,
            config["portfolio"]["top_k"],
        )
        results = {}
        print(
            "Backtesting fixed MLP top30 and matched equal weight in three scenarios.", flush=True
        )
        for variant, mask in (("equal_weight", members[start:]), ("mlp_top30", selected)):
            variant_output = output / variant
            variant_output.mkdir()
            variant_config = {
                **base_config,
                "date_start": dates[start],
                "expected_sessions": len(dates) - start,
                "expected_members_per_day": int(mask[0].sum()),
                "target_mask_semantics": "conditional_members"
                if variant == "equal_weight"
                else "mlp_selected_top30",
            }
            save_json(variant_output / "frozen_config.json", variant_config)
            np.savez_compressed(
                variant_output / "diagnostic_inputs.npz",
                dates=np.array(dates[start:]),
                symbols=np.array(symbols),
                adjusted_open=opening[start:],
                adjusted_close=closing[start:],
                membership=mask,
                full_day_suspended=halts[start:],
            )
            results[variant] = {}
            for scenario in base_config["scenarios"]:
                name = scenario["scenario_name"]
                scenario_output = variant_output / name
                scenario_output.mkdir()
                result = run_diagnostic_backtest(
                    dates[start:],
                    symbols,
                    opening[start:],
                    closing[start:],
                    mask,
                    halts[start:],
                    config={**base_config["strategy"], **scenario},
                )
                export_result(scenario_output, result)
                results[variant][name] = result
        ic_rows, ic_stats = information_coefficients(
            dates, symbols, predictions, opening, members, config["windows"]
        )
        pq.write_table(
            pa.Table.from_pylist(ic_rows), output / "daily_ic.parquet", compression="zstd"
        )
        save_json(output / "ic_summary.json", ic_stats)
        plot_comparison(output, results, base_config["strategy"]["initial_cash"])
        comparison = {}
        for scenario in base_config["scenarios"]:
            name = scenario["scenario_name"]
            control, model = results["equal_weight"][name], results["mlp_top30"][name]
            comparison[name] = {
                "equal_weight": {"metrics": control["metrics"], "costs": control["costs"]},
                "mlp_top30": {"metrics": model["metrics"], "costs": model["costs"]},
                "return_difference_percentage_points": 100
                * (model["metrics"]["total_return"] - control["metrics"]["total_return"]),
            }
        save_json(output / "comparison.json", comparison)
        manifest.update(
            status="completed_development_diagnostic_not_blind_test",
            evaluation_start=dates[start],
            evaluation_end=dates[-1],
            evaluation_sessions=len(dates) - start,
            comparison=comparison,
            information_coefficients=ic_stats,
        )
        for spec in base_config["inputs"].values():
            require(digest(root / spec["path"]) == spec["sha256"], "candidate input changed")
        manifest["files"] = {
            p.relative_to(output).as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)}
            for p in sorted(output.rglob("*"))
            if p.is_file() and p.name != "manifest.json"
        }
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        (output / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        manifest["elapsed_seconds"] = time.perf_counter() - started
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {"output": str(output), "comparison": comparison, "ic": ic_stats}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/mlp-alpha158-2023-diagnostic.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.config, args.output.resolve())
