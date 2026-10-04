"""Native feature causality and fixed chronological training on small test fixtures."""

from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ashare_lab.alpha158_diagnostic import (
    Alpha158DiagnosticError,
    MODEL_PARAMETERS,
    build_alpha158_features,
    build_open_to_open_labels,
    train_alpha158_diagnostic,
)


def dates(count):
    return list(pd.bdate_range("2023-01-03", periods=count).date)


def feature_fixture():
    days = dates(90)
    t = np.arange(len(days))[:, None]
    j = np.arange(2)[None, :]
    close = 10 + 0.07 * t + j + 0.4 * np.sin(t * 0.23 + j)
    return (
        days,
        ["000001.SZ", "600001.SH"],
        {
            "open": close * (1 + 0.001 * np.cos(t)),
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": 1000 + t * (1 + j) + np.sin(t) * 20,
            "vwap": close * 1.001,
        },
    )


def test_native_158_features_are_causal_and_preserve_missing_source_values(tmp_path):
    days, symbols, raw = feature_fixture()
    for values in raw.values():
        values[25, 1] = np.nan
    before = deepcopy(raw)
    first = build_alpha158_features(days, symbols, raw, provider_path=tmp_path / "provider-a")
    altered = deepcopy(raw)
    for values in altered.values():
        values[76:, :] *= 3.5
    second = build_alpha158_features(days, symbols, altered, provider_path=tmp_path / "provider-b")
    assert first["features"].shape == (90, 2, 158)
    assert len(set(first["feature_names"])) == 158
    np.testing.assert_array_equal(first["features"][:76], second["features"][:76])
    roc60 = first["feature_names"].index("ROC60")
    assert np.isnan(first["features"][59, 0, roc60])
    assert first["features"][60, 0, roc60] == pytest.approx(
        raw["close"][0, 0] / raw["close"][60, 0]
    )
    assert not np.isclose(first["features"][80, 0, roc60], second["features"][80, 0, roc60])
    open0 = first["feature_names"].index("OPEN0")
    assert np.isnan(first["features"][25, 1, open0])
    assert not np.isinf(first["features"]).any()
    assert first["metadata"]["kernels"] == 1
    assert first["metadata"]["engine"].endswith("Alpha158DL.get_feature_config")
    for name in raw:
        np.testing.assert_array_equal(raw[name], before[name])
    binary = np.fromfile(
        tmp_path / "provider-a" / "features" / "sh600001" / "close.day.bin", dtype="<f4"
    )
    assert binary[0] == 0 and np.isnan(binary[26])


def test_open_label_offsets_rank_ties_and_signal_day_membership():
    days = dates(10)
    opens = np.full((10, 4), 10.0)
    opens[0] = 999  # The signal close/open itself must not enter the future label.
    opens[1] = [20, 10, 10, 10]
    opens[5] = [900, 1, 900, 900]  # t+5 is not the exit endpoint.
    opens[6] = [22, 12, 12, 90]
    members = np.ones_like(opens, dtype=bool)
    members[0, 3] = False
    members[1, 0] = False  # Future membership cannot remove a signal-date member label.
    result = build_open_to_open_labels(days, opens, members)
    np.testing.assert_allclose(result["raw_returns"][0, :3], [0.1, 0.2, 0.2])
    np.testing.assert_allclose(result["labels"][0, :3], [-1 / 6, 1 / 3, 1 / 3])
    assert np.isnan(result["labels"][0, 3])
    assert np.isnan(result["labels"][-6:]).all()
    assert result["metadata"]["per_signal_date"][0]["valid_labels"] == 3
    assert result["metadata"]["per_signal_date"][-1]["beyond_input_horizon"] == 4


def test_missing_label_endpoint_is_excluded_not_zero_or_cross_section_member():
    opens = np.full((8, 3), 10.0)
    opens[1, 0] = np.nan
    opens[6, 1] = np.nan
    result = build_open_to_open_labels(dates(8), opens, np.ones_like(opens, dtype=bool))
    assert np.isnan(result["labels"][0, :2]).all()
    assert result["raw_returns"][0, 2] == 0
    assert result["labels"][0, 2] == 0.5  # pandas percentile convention for a singleton.
    counts = result["metadata"]["per_signal_date"][0]
    assert (counts["missing_entry"], counts["missing_exit"], counts["valid_labels"]) == (1, 1, 1)


def training_fixture():
    rng = np.random.default_rng(8)
    n, k = 120, 40
    days = dates(n)
    symbols = [f"{i + 1:06d}.SZ" for i in range(k)]
    features = rng.normal(size=(n, k, 158)).astype(np.float32)
    features[61, 1, 0] = np.nan
    features[62, 2, 1] = np.inf
    # Stable cross-sectional slopes allow the fixed 200-row leaf minimum to split.
    slopes = np.linspace(-0.001, 0.002, k)
    opens = 10 * np.exp(np.arange(n)[:, None] * slopes[None, :])
    features[:, :, 0] = slopes * 1000
    features[61, 1, 0] = np.nan
    close = opens.copy()
    members = np.ones((n, k), dtype=bool)
    members[60, 0] = False
    members[85, 0] = False
    members[105, 0] = False
    close[63, 3] = np.nan
    close[86, 1] = np.nan
    opens[67, 4] = np.nan
    windows = [
        {"name": "first", "evaluation_start": days[80], "evaluation_end": days[99]},
        {"name": "second", "evaluation_start": days[100], "evaluation_end": days[-1]},
    ]
    return {
        "dates": days,
        "symbols": symbols,
        "features": features,
        "raw_open": opens,
        "raw_close": close,
        "membership": members,
        "windows": windows,
    }


@pytest.fixture(scope="module")
def trained_pair(tmp_path_factory):
    root = tmp_path_factory.mktemp("alpha158-training")
    inputs = training_fixture()
    pristine = deepcopy(inputs)
    base = train_alpha158_diagnostic(**inputs, model_dir=root / "models-base")
    changed = deepcopy(inputs)
    # Change held-out label endpoints, not the first window's training or prediction features.
    changed["raw_open"][80:] *= np.linspace(1.3, 0.8, 40)[None, :] ** np.arange(1, 41)[:, None]
    altered = train_alpha158_diagnostic(**changed, model_dir=root / "models-future-perturbed")
    return inputs, pristine, base, altered


def test_maturity_cutoff_and_feature_warmup_are_strict(trained_pair):
    inputs, _, result, _ = trained_pair
    first, second = result["training_masks"]["first"], result["training_masks"]["second"]
    assert not first[:60].any()
    assert first[60, 1]
    assert first[73].any()  # exit 79 is visible at fit-as-of index 79.
    assert not first[74:].any()  # exit 80 is the first evaluation day and must be excluded.
    assert second[93].any() and not second[94:].any()
    assert not first[60, 0] and not first[63, 3]
    assert not first[61, 4] and not first[66, 4]  # Missing exit/entry from open[67].
    for evidence, start in zip(result["training_evidence"], [80, 100], strict=True):
        assert evidence["maximum_label_exit_index"] == start - 1
        assert evidence["fit_as_of"] == inputs["dates"][start - 1].isoformat()
        assert evidence["fit_evaluation_rows"] == 0 and evidence["early_stopping"] is False
        assert evidence["training_rows"] == int(result["training_masks"][evidence["window"]].sum())
        assert evidence["missing_label_past_rows_excluded"] > 0


def test_future_eval_labels_cannot_change_first_fit_or_signals(trained_pair):
    _, _, base, altered = trained_pair
    first_before, first_after = base["training_evidence"][0], altered["training_evidence"][0]
    for key in ("training_features_sha256", "training_labels_sha256", "model_sha256"):
        assert first_before[key] == first_after[key]
    np.testing.assert_array_equal(base["predictions"][80:100], altered["predictions"][80:100])
    assert not np.allclose(
        base["raw_returns"][80:100], altered["raw_returns"][80:100], equal_nan=True
    )
    # The predeclared second expanding fit can legitimately use matured first-window labels.
    assert (
        base["training_evidence"][1]["training_labels_sha256"]
        != altered["training_evidence"][1]["training_labels_sha256"]
    )


def test_prediction_membership_nan_features_and_native_model_files(trained_pair):
    import lightgbm as lgb

    inputs, pristine, result, _ = trained_pair
    predicted = result["predictions"]
    assert np.isnan(predicted[:80]).all()
    assert np.isnan(predicted[85, 0]) and np.isnan(predicted[86, 1])
    assert np.isnan(predicted[105, 0])
    assert np.isfinite(predicted[80, 1]) and np.isfinite(predicted[-1, 1])
    assert result["metadata"]["model_parameters"] == MODEL_PARAMETERS
    assert MODEL_PARAMETERS["n_estimators"] == 150 and MODEL_PARAMETERS["min_child_samples"] == 200
    assert result["metadata"]["not_blind_test"] is True
    assert result["metadata"]["research_eligible"] is False
    for model_path, evidence in zip(
        result["model_paths"], result["training_evidence"], strict=True
    ):
        model = lgb.Booster(model_str=Path(model_path).read_text(encoding="utf-8"))
        assert model.num_feature() == 158 and model.num_trees() == 150
        assert evidence["actual_trees"] == 150
    for key in ("features", "raw_open", "raw_close", "membership"):
        np.testing.assert_array_equal(inputs[key], pristine[key])
    json.dumps(result["metadata"], allow_nan=False)
    json.dumps(result["training_evidence"], allow_nan=False)


def test_2024_inputs_are_rejected_before_provider_creation(tmp_path):
    _, symbols, raw = feature_fixture()
    outside = list(pd.bdate_range("2024-01-03", periods=90).date)
    destination = tmp_path / "must-not-be-created"
    with pytest.raises(Alpha158DiagnosticError, match="only 2023"):
        build_alpha158_features(outside, symbols, raw, provider_path=destination)
    assert not destination.exists()
