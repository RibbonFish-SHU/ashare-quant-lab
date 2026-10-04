"""Reconcile saved model diagnostics without retraining or changing parameters."""

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from audit_diagnostic_backtest import audit as audit_accounting
from run_diagnostic_backtest import digest, load_inputs, require, save_json


def array_hash(values):
    values = np.ascontiguousarray(values)
    result = hashlib.sha256(str((values.shape, values.dtype.str)).encode())
    result.update(values.tobytes())
    return result.hexdigest()


def same(actual, expected, name, *, rtol=1e-10, atol=1e-12):
    require(np.allclose(actual, expected, rtol=rtol, atol=atol, equal_nan=True), name)


def audit(output):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((output / "frozen_model_config.json").read_text(encoding="utf-8"))
    base_config = json.loads(
        (root / config["backtest_inputs_and_costs"]).read_text(encoding="utf-8")
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    require(
        manifest["status"] == "completed_development_diagnostic_not_blind_test", "run incomplete"
    )
    require(
        digest(output / "frozen_model_config.json") == manifest["frozen_config_sha256"],
        "config changed",
    )
    for name, ref in manifest["files"].items():
        require(digest(output / name) == ref["sha256"], f"artifact changed: {name}")
    dates, symbols, opens, closes, membership, halts, _ = load_inputs(root, base_config)
    saved = np.load(output / "predictions_and_labels.npz", allow_pickle=False)
    require(
        saved["dates"].tolist() == dates and saved["symbols"].tolist() == symbols, "axes differ"
    )
    predictions, labels = saved["predictions"], saved["labels"]
    masks = np.load(output / "training_masks.npz", allow_pickle=False)
    features = np.load(output / "features.npy", allow_pickle=False)
    evidence = json.loads((output / "training_evidence.json").read_text(encoding="utf-8"))
    feature_evidence = json.loads((output / "features_metadata.json").read_text(encoding="utf-8"))
    require(evidence["metadata"]["model_parameters"] == config["model"], "model parameters differ")
    require(features.shape == (*opens.shape, 158), "feature dimensions differ")
    require(len(feature_evidence["names"]) == 158, "feature names differ")
    raw_return = np.full(opens.shape, np.nan)
    raw_return[:-6] = opens[6:] / opens[1:-5] - 1
    raw_return[~membership] = np.nan
    expected_labels = (
        pd.DataFrame(raw_return).rank(axis=1, method="average", pct=True).to_numpy() - 0.5
    )
    same(labels, expected_labels, "t+1 to t+6 member-relative labels")
    same(saved["raw_returns"], raw_return, "raw forward returns")
    fields = ["raw_open", "raw_high", "raw_low", "raw_close", "raw_volume", "raw_vwap"]
    quote = (
        pq.read_table(
            root / base_config["inputs"]["quotes"]["path"],
            columns=["event_date", "symbol", *fields],
        )
        .to_pandas()
        .sort_values(["event_date", "symbol"])
    )
    raw = {name[4:]: quote[name].to_numpy().reshape(opens.shape) for name in fields}
    expected_features = {
        "OPEN0": raw["open"] / raw["close"],
        "HIGH0": raw["high"] / raw["close"],
        "LOW0": raw["low"] / raw["close"],
        "VWAP0": raw["vwap"] / raw["close"],
        "KMID": (raw["close"] - raw["open"]) / raw["open"],
    }
    roc = np.full(opens.shape, np.nan)
    roc[60:] = raw["close"][:-60] / raw["close"][60:]
    expected_features["ROC60"] = roc
    for name, values in expected_features.items():
        index = feature_evidence["names"].index(name)
        same(features[:, :, index], values, f"native feature {name}", rtol=1e-6, atol=1e-7)
    indices = np.arange(len(dates))[:, None]
    common = (indices >= 60) & membership & np.isfinite(closes)
    prediction_universe = np.zeros(membership.shape, dtype=bool)
    windows, importance = [], []
    for config_window, recorded in zip(config["windows"], evidence["windows"], strict=True):
        start, end = (dates.index(config_window[k]) for k in ("evaluation_start", "evaluation_end"))
        name = config_window["name"]
        training = common & (indices + 6 < start) & np.isfinite(expected_labels)
        forecast = common & (indices >= start) & (indices <= end)
        prediction_universe |= forecast
        require(np.array_equal(masks[name], training), f"training boundary changed: {name}")
        require(
            array_hash(training) == recorded["training_mask_sha256"], f"training mask hash: {name}"
        )
        require(
            array_hash(features[training]) == recorded["training_features_sha256"],
            f"training features: {name}",
        )
        require(
            array_hash(labels[training]) == recorded["training_labels_sha256"],
            f"training labels: {name}",
        )
        train_days = np.where(training)[0]
        require(train_days.max() + 6 < start, f"unmatured training label: {name}")
        require(recorded["fit_as_of"] == dates[start - 1], f"fit date changed: {name}")
        require(recorded["fit_evaluation_rows"] == 0, f"evaluation used for training: {name}")
        model_file = output / "models" / f"{name}.txt"
        text = model_file.read_text(encoding="utf-8")
        require(
            hashlib.sha256(text.encode()).hexdigest() == recorded["model_sha256"],
            f"model hash: {name}",
        )
        model = lgb.Booster(model_str=text)
        require(model.num_trees() == config["model"]["n_estimators"], f"tree count changed: {name}")
        reproduction = model.predict(features[forecast], num_threads=4)
        same(predictions[forecast], reproduction, f"persisted model reproduces predictions: {name}")
        gain = model.feature_importance(importance_type="gain")
        split = model.feature_importance(importance_type="split")
        for feature_name, feature_gain, split_count in zip(
            feature_evidence["names"], gain, split, strict=True
        ):
            importance.append(
                {
                    "window": name,
                    "feature": feature_name,
                    "training_gain": float(feature_gain),
                    "training_split_count": int(split_count),
                }
            )
        windows.append(
            {
                "name": name,
                "training_rows": int(training.sum()),
                "prediction_rows": int(forecast.sum()),
                "first_training_signal": dates[int(train_days.min())],
                "last_training_signal": dates[int(train_days.max())],
                "maximum_training_label_exit": dates[int(train_days.max()) + 6],
                "fit_as_of": dates[start - 1],
                "evaluation_start": dates[start],
                "evaluation_end": dates[end],
                "prediction_reproduction_max_abs_error": float(
                    np.max(np.abs(predictions[forecast] - reproduction))
                ),
                "training_mask_verified": True,
                "maturity_verified": True,
            }
        )
    require(
        np.array_equal(np.isfinite(predictions), prediction_universe), "prediction universe differs"
    )
    start = dates.index(config["portfolio"]["schedule_origin"])
    selected = np.load(output / "lightgbm_top30" / "diagnostic_inputs.npz", allow_pickle=False)[
        "membership"
    ]
    expected_selection = np.zeros(selected.shape, dtype=bool)
    for local, day in enumerate(range(start, len(dates))):
        candidates = [j for j in range(len(symbols)) if prediction_universe[day, j]]
        ranked = sorted(candidates, key=lambda j: (-predictions[day, j], symbols[j]))
        expected_selection[local, ranked[: config["portfolio"]["top_k"]]] = True
    require(
        np.array_equal(selected, expected_selection), "portfolio selection is not declared top30"
    )
    accounting = {}
    for variant in ("equal_weight", "lightgbm_top30"):
        variant_output = output / variant
        matrix = np.load(variant_output / "diagnostic_inputs.npz", allow_pickle=False)
        require(matrix["dates"].tolist() == dates[start:], f"comparison dates differ: {variant}")
        require(matrix["symbols"].tolist() == symbols, f"comparison symbols differ: {variant}")
        same(matrix["adjusted_open"], opens[start:], "comparison opening prices")
        same(matrix["adjusted_close"], closes[start:], "comparison closing prices")
        require(
            np.array_equal(matrix["full_day_suspended"], halts[start:]), "suspension mask differs"
        )
        if variant == "equal_weight":
            require(
                np.array_equal(matrix["membership"], membership[start:]), "control universe differs"
            )
        with redirect_stdout(io.StringIO()):
            audit_accounting(variant_output)
        accounting[variant] = json.loads(
            (variant_output / "independent_audit.json").read_text(encoding="utf-8")
        )
    ic = pq.read_table(output / "daily_ic.parquet").to_pylist()
    for row in ic:
        i = dates.index(row["signal_date"])
        valid = prediction_universe[i] & np.isfinite(raw_return[i])
        first = pd.Series(predictions[i, valid])
        second = pd.Series(raw_return[i, valid])
        same(row["ic"], first.corr(second), "IC")
        same(row["rank_ic"], first.rank().corr(second.rank()), "Rank IC")
        require(
            row["entry_date"] == dates[i + 1] and row["exit_date"] == dates[i + 6], "IC endpoints"
        )
    quarter_performance = {}
    for variant in ("equal_weight", "lightgbm_top30"):
        daily = pq.read_table(output / variant / "base" / "daily.parquet").to_pandas()
        nav = daily.set_index("date").nav
        quarter_performance[variant] = {}
        for window in config["windows"]:
            first = dates.index(window["evaluation_start"])
            initial = (
                config["portfolio"]["initial_cash"] if first == start else nav[dates[first - 1]]
            )
            part = nav.loc[window["evaluation_start"] : window["evaluation_end"]]
            curve = np.r_[initial, part.to_numpy()]
            quarter_performance[variant][window["name"]] = {
                "return": float(curve[-1] / curve[0] - 1),
                "drawdown_from_quarter_start": float(
                    (curve / np.maximum.accumulate(curve) - 1).min()
                ),
                "note": "Continuous portfolio, no reset or forced quarter-end liquidation.",
            }
    save_json(output / "training_feature_importance.json", importance)
    result = {
        "status": "passed",
        "windows": windows,
        "native_feature_checks": list(expected_features),
        "label_formula_verified": True,
        "same_day_top30_verified": True,
        "ic_days_verified": len(ic),
        "accounting": accounting,
        "quarter_performance_base_cost": quarter_performance,
        "model_fits_in_this_audit": 0,
    }
    save_json(output / "independent_model_audit.json", result)
    print(
        json.dumps(
            {"status": "passed", "windows": windows, "quarter_performance": quarter_performance},
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    audit(args.output.resolve())
