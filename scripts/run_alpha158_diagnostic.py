"""One frozen Alpha158/LightGBM development comparison on 2023 candidates."""

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

from ashare_lab import alpha158_diagnostic
from ashare_lab.diagnostic_backtest import run_diagnostic_backtest
from run_diagnostic_backtest import (
    digest,
    export_result,
    load_inputs,
    require,
    save_json,
    source_identity,
)


def information_coefficients(dates, symbols, predictions, opening, membership, windows):
    """Evaluate only after all predetermined fits and predictions are complete."""
    future_returns = np.full(opening.shape, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        future_returns[:-6] = opening[6:] / opening[1:-5] - 1
    rows = []
    for i, day in enumerate(dates):
        valid = membership[i] & np.isfinite(predictions[i]) & np.isfinite(future_returns[i])
        if valid.sum() < 3:
            continue
        score = pd.Series(predictions[i, valid])
        label = pd.Series(future_returns[i, valid])
        if score.nunique() <= 1 or label.nunique() <= 1:
            continue
        rows.append(
            {
                "signal_date": day,
                "entry_date": dates[i + 1],
                "exit_date": dates[i + 6],
                "rows": int(valid.sum()),
                "ic": float(score.corr(label)),
                "rank_ic": float(score.rank().corr(label.rank())),
            }
        )
    frame = pd.DataFrame(rows)
    stats = {}
    for name, selected in [
        ("all", frame),
        *[
            (
                window["name"],
                frame[
                    frame.signal_date.between(window["evaluation_start"], window["evaluation_end"])
                ],
            )
            for window in windows
        ],
    ]:
        stats[name] = {
            "days": len(selected),
            "mean_ic": float(selected.ic.mean()),
            "mean_rank_ic": float(selected.rank_ic.mean()),
            "rank_ic_std": float(selected.rank_ic.std(ddof=1)),
            "positive_rank_ic_fraction": float((selected.rank_ic > 0).mean()),
        }
    return rows, stats


def select_top_k(scores, closing, membership, symbols, top_k):
    require(type(top_k) is int and top_k > 0, "top_k must be a positive integer")
    selection = np.zeros(membership.shape, dtype=bool)
    symbols = np.array(symbols)
    for i in range(len(scores)):
        eligible = np.flatnonzero(membership[i] & np.isfinite(scores[i]) & np.isfinite(closing[i]))
        require(len(eligible) >= top_k, f"insufficient current-day predictions at row {i}")
        ranked = np.lexsort((symbols[eligible], -scores[i, eligible]))
        selection[i, eligible[ranked[:top_k]]] = True
    return selection


def plot_comparison(output, results, capital):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True, height_ratios=[2, 1])
    for variant, label, color in (
        ("equal_weight", "300-stock equal weight | base costs", "#64748b"),
        ("lightgbm_top30", "Alpha158 + LightGBM top 30 | base costs", "#1769aa"),
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
    figure.suptitle(
        "2023 H2 development diagnostic | frozen quarterly fits | same execution and costs"
    )
    figure.text(
        0.06,
        0.015,
        "Previously inspected development year; conditional members and adjusted-unit fills. "
        "Not a blind final test or certified executable return.",
        fontsize=8,
    )
    figure.tight_layout(rect=(0, 0.04, 1, 0.96))
    figure.savefig(output / "model_vs_equal_weight.png", dpi=180)
    plt.close(figure)


def run(config_path, output):
    root = Path(__file__).resolve().parents[1]
    config_path = config_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    require(config["research_eligible"] is False, "diagnostic cannot certify historical inputs")
    base_path = root / config["backtest_inputs_and_costs"]
    normalized = base_path.read_bytes().replace(b"\r\n", b"\n")
    require(
        hashlib.sha256(normalized).hexdigest() == config["backtest_config_sha256_lf"],
        "original input, strategy or cost assumptions changed",
    )
    base_config = json.loads(normalized)
    require(config["model"] == alpha158_diagnostic.MODEL_PARAMETERS, "model parameters changed")
    require(
        config["features"]["warmup_prior_sessions"] == alpha158_diagnostic.WARMUP_SESSIONS,
        "feature warmup changed",
    )
    for key in ("initial_cash", "invest_fraction", "rebalance_every"):
        require(
            config["portfolio"][key] == base_config["strategy"][key], f"portfolio {key} differs"
        )
    identity = source_identity(root, config_path)
    model_module = Path(alpha158_diagnostic.__file__).resolve()
    require(model_module.is_relative_to(root / "src"), "model module belongs to another checkout")
    for path in (Path(__file__).resolve(), model_module, base_path):
        identity["files_sha256"][path.relative_to(root).as_posix()] = digest(path)
    identity["packages"].update(
        {name: importlib.metadata.version(name) for name in ("pyqlib", "lightgbm")}
    )
    require(not output.exists(), "keep previous and failed runs; choose a new output directory")
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
        "planned_hyperparameter_sets": 1,
    }
    (output / "frozen_model_config.json").write_bytes(config_path.read_bytes())
    save_json(output / "manifest.json", manifest)
    try:
        dates, symbols, opening, closing, members, halts, input_summary = load_inputs(
            root, base_config
        )
        require(
            dates[config["features"]["warmup_prior_sessions"]]
            == config["features"]["first_training_signal"],
            "training warmup calendar changed",
        )
        save_json(output / "input_summary.json", input_summary)
        columns = [f"raw_{name}" for name in config["features"]["raw_fields"]]
        frame = pq.read_table(
            root / base_config["inputs"]["quotes"]["path"],
            columns=["event_date", "symbol", *columns],
        ).to_pandas()
        frame["event_date"] = frame.event_date.map(lambda value: value.isoformat())
        frame = frame.set_index(["event_date", "symbol"]).sort_index()
        expected_index = pd.MultiIndex.from_product(
            [dates, symbols], names=["event_date", "symbol"]
        )
        require(frame.index.equals(expected_index), "Alpha158 input grid does not align")
        raw_fields = {
            field: frame[f"raw_{field}"].to_numpy(dtype=float).reshape(opening.shape)
            for field in config["features"]["raw_fields"]
        }
        print("Computing Qlib native Alpha158 features on 2023 only.", flush=True)
        stage_started = time.perf_counter()
        feature_result = alpha158_diagnostic.build_alpha158_features(
            dates, symbols, raw_fields, provider_path=output / "qlib_candidate_provider"
        )
        manifest["feature_seconds"] = time.perf_counter() - stage_started
        features = feature_result["features"]
        require(features.shape == (*opening.shape, 158), "expected exactly 158 native features")
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
        print("Fitting the two frozen quarterly models; no parameter search.", flush=True)
        stage_started = time.perf_counter()
        fitted = alpha158_diagnostic.train_alpha158_diagnostic(
            dates,
            symbols,
            features,
            opening,
            closing,
            members,
            windows=config["windows"],
            model_dir=output / "models",
        )
        manifest["model_seconds"] = time.perf_counter() - stage_started
        predictions = fitted["predictions"]
        require(predictions.shape == opening.shape, "prediction shape mismatch")
        require(
            fitted["metadata"]["model_parameters"] == config["model"], "fitted parameters changed"
        )
        save_json(
            output / "training_evidence.json",
            {
                "windows": fitted["training_evidence"],
                "metadata": fitted["metadata"],
                "label_metadata": fitted["label_metadata"],
            },
        )
        np.savez_compressed(
            output / "predictions_and_labels.npz",
            dates=np.array(dates),
            symbols=np.array(symbols),
            predictions=predictions,
            labels=fitted["labels"],
            raw_returns=fitted["raw_returns"],
        )
        np.savez_compressed(output / "training_masks.npz", **fitted["training_masks"])
        start = dates.index(config["portfolio"]["schedule_origin"])
        require(dates[-1] == config["windows"][-1]["evaluation_end"], "evaluation end mismatch")
        selected = select_top_k(
            predictions[start:],
            closing[start:],
            members[start:],
            symbols,
            config["portfolio"]["top_k"],
        )
        results = {}
        print(
            "Backtesting model and matched equal weight with all three cost scenarios.", flush=True
        )
        for variant, mask in (("equal_weight", members[start:]), ("lightgbm_top30", selected)):
            variant_output = output / variant
            variant_output.mkdir()
            variant_config = {
                **base_config,
                "date_start": dates[start],
                "expected_sessions": len(dates) - start,
                "expected_members_per_day": int(mask[0].sum()),
                "target_mask_semantics": "conditional_members"
                if variant == "equal_weight"
                else "model_selected_top30",
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
            control, model = results["equal_weight"][name], results["lightgbm_top30"][name]
            comparison[name] = {
                "equal_weight": {"metrics": control["metrics"], "costs": control["costs"]},
                "lightgbm_top30": {"metrics": model["metrics"], "costs": model["costs"]},
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
            {"output": str(output), "comparison": manifest["comparison"], "ic": ic_stats},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/alpha158-lightgbm-2023-diagnostic.json")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.config, args.output.resolve())
