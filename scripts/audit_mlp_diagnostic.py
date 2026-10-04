"""Independent audit for saved fixed MLP predictions and accounting outputs."""

import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from ashare_lab import mlp_diagnostic
from audit_diagnostic_backtest import audit as audit_accounting
from run_alpha158_diagnostic import select_top_k
from run_diagnostic_backtest import digest, load_inputs, require, save_json


def same(actual, expected, name, *, rtol=1e-8, atol=1e-10):
    require(np.allclose(actual, expected, rtol=rtol, atol=atol, equal_nan=True), name)


def audit(output):
    import torch

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
    require(config["model"] == mlp_diagnostic.MODEL_PARAMETERS, "model parameters differ")
    dates, symbols, opens, closes, membership, halts, _ = load_inputs(root, base_config)
    saved = np.load(output / "predictions_labels_returns.npz", allow_pickle=False)
    predictions, labels, raw_returns = saved["predictions"], saved["labels"], saved["raw_returns"]
    require(
        saved["dates"].tolist() == dates and saved["symbols"].tolist() == symbols, "axes differ"
    )
    raw_return_expected = np.full(opens.shape, np.nan)
    raw_return_expected[:-6] = opens[6:] / opens[1:-5] - 1
    raw_return_expected[~membership] = np.nan
    expected_labels = (
        pd.DataFrame(raw_return_expected).rank(axis=1, method="average", pct=True).to_numpy() - 0.5
    )
    same(labels, expected_labels, "rank labels")
    same(raw_returns, raw_return_expected, "future open returns")
    features = np.load(output / "features.npy", allow_pickle=False)
    require(features.shape == (*opens.shape, 158), "feature shape differs")
    feature_meta = json.loads((output / "features_metadata.json").read_text(encoding="utf-8"))
    require(len(feature_meta["names"]) == 158, "feature names differ")
    # Check three independent native-expression anchors, including the 60-session warmup.
    quote = (
        pq.read_table(
            root / base_config["inputs"]["quotes"]["path"],
            columns=[
                "event_date",
                "symbol",
                "raw_open",
                "raw_close",
                "raw_high",
                "raw_low",
                "raw_vwap",
            ],
        )
        .to_pandas()
        .sort_values(["event_date", "symbol"])
    )
    raw = {
        name[4:]: quote[name].to_numpy().reshape(opens.shape)
        for name in ("raw_open", "raw_close", "raw_high", "raw_low", "raw_vwap")
    }
    for name, expected in {
        "OPEN0": raw["open"] / raw["close"],
        "HIGH0": raw["high"] / raw["close"],
        "LOW0": raw["low"] / raw["close"],
    }.items():
        same(
            features[:, :, feature_meta["names"].index(name)], expected, name, rtol=1e-6, atol=1e-7
        )
    roc = np.full(opens.shape, np.nan)
    roc[60:] = raw["close"][:-60] / raw["close"][60:]
    same(features[:, :, feature_meta["names"].index("ROC60")], roc, "ROC60", rtol=1e-6, atol=1e-7)
    masks = np.load(output / "training_masks.npz", allow_pickle=False)
    evidence = json.loads((output / "training_evidence.json").read_text(encoding="utf-8"))
    grid = np.arange(len(dates))[:, None]
    common = (grid >= 60) & membership & np.isfinite(closes)
    torch.set_num_threads(1)
    model_checks = []
    prediction_universe = np.zeros(membership.shape, dtype=bool)
    for window, recorded in zip(config["windows"], evidence["windows"], strict=True):
        start, end = dates.index(window["evaluation_start"]), dates.index(window["evaluation_end"])
        training = common & (grid + 6 < start) & np.isfinite(labels)
        forecast = common & (grid >= start) & (grid <= end)
        require(
            np.array_equal(masks[window["name"]], training),
            f"training mask differs: {window['name']}",
        )
        train_days = np.where(training)[0]
        require(train_days.max() + 6 < start, "unmatured label entered fit")
        require(recorded["fit_as_of"] == dates[start - 1], "fit as-of changed")
        require(recorded["fit_evaluation_rows"] == 0, "evaluation rows entered fit")
        model_path = output / "models" / f"{window['name']}.pt"
        model_bytes = model_path.read_bytes()
        require(
            hashlib.sha256(model_bytes).hexdigest() == recorded["model_sha256"],
            "model hash differs",
        )
        saved_model = torch.load(model_path, map_location="cpu", weights_only=False)
        require(saved_model["model_parameters"] == config["model"], "serialized parameters differ")
        model = mlp_diagnostic._model()
        model.load_state_dict(saved_model["state_dict"])
        model.eval()
        clean = mlp_diagnostic._clean_features(features, opens.shape)
        with torch.no_grad():
            reproduced = model(torch.from_numpy(clean[forecast])).squeeze(1).numpy()
        same(predictions[forecast], reproduced, f"prediction reproduction: {window['name']}")
        prediction_universe |= forecast
        model_checks.append(
            {
                "window": window["name"],
                "training_rows": int(training.sum()),
                "prediction_rows": int(forecast.sum()),
                "fit_as_of": dates[start - 1],
                "maximum_training_label_exit": dates[int(train_days.max()) + 6],
                "prediction_reproduction_max_abs_error": float(
                    np.max(np.abs(predictions[forecast] - reproduced))
                ),
                "maturity_verified": True,
            }
        )
    require(
        np.array_equal(np.isfinite(predictions), prediction_universe), "prediction universe differs"
    )
    start = dates.index(config["portfolio"]["schedule_origin"])
    selected = np.load(output / "mlp_top30" / "diagnostic_inputs.npz", allow_pickle=False)[
        "membership"
    ]
    expected_selected = select_top_k(
        predictions[start:],
        closes[start:],
        membership[start:],
        symbols,
        config["portfolio"]["top_k"],
    )
    require(np.array_equal(selected, expected_selected), "top30 selection differs")
    accounting = {}
    for variant in ("equal_weight", "mlp_top30"):
        variant_output = output / variant
        with redirect_stdout(io.StringIO()):
            audit_accounting(variant_output)
        accounting[variant] = json.loads(
            (variant_output / "independent_audit.json").read_text(encoding="utf-8")
        )
    ic_rows = pq.read_table(output / "daily_ic.parquet").to_pylist()
    for row in ic_rows:
        i = dates.index(row["signal_date"])
        valid = prediction_universe[i] & np.isfinite(raw_return_expected[i])
        score, label = pd.Series(predictions[i, valid]), pd.Series(raw_return_expected[i, valid])
        same(row["ic"], score.corr(label), "IC")
        same(row["rank_ic"], score.rank().corr(label.rank()), "Rank IC")
        require(
            row["entry_date"] == dates[i + 1] and row["exit_date"] == dates[i + 6], "IC endpoints"
        )
    result = {
        "status": "passed",
        "model_windows": model_checks,
        "native_feature_checks": ["OPEN0", "HIGH0", "LOW0", "ROC60"],
        "label_formula_verified": True,
        "top30_selection_verified": True,
        "ic_days_verified": len(ic_rows),
        "accounting": accounting,
        "model_fits_in_this_audit": 0,
    }
    save_json(output / "independent_mlp_audit.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    audit(args.output.resolve())
