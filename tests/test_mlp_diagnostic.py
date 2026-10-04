"""Fixed MLP boundary and missing-feature checks."""

from datetime import date, timedelta

import numpy as np

from ashare_lab.mlp_diagnostic import MODEL_PARAMETERS, _clean_features, train_mlp_diagnostic


def days(count):
    return [date(2023, 1, 3) + timedelta(days=i) for i in range(count)]


def test_clean_features_has_declared_zero_missing_policy():
    values = np.zeros((2, 3, 158), dtype=np.float32)
    values[0, 0, 0] = np.nan
    values[1, 1, 1] = np.inf
    values[1, 2, 2] = -np.inf
    cleaned = _clean_features(values, (2, 3))
    assert np.isfinite(cleaned).all()
    assert cleaned[0, 0, 0] == cleaned[1, 1, 1] == cleaned[1, 2, 2] == 0


def test_mlp_uses_only_mature_labels_and_predicts_only_declared_window(tmp_path):
    n, k = 90, 40
    dates_input = days(n)
    symbols = [f"{i + 1:06d}.SZ" for i in range(k)]
    rng = np.random.default_rng(17)
    features = rng.normal(size=(n, k, 158)).astype(np.float32)
    raw_open = 10 * np.exp(np.arange(n)[:, None] * np.linspace(-0.002, 0.002, k)[None, :])
    raw_close = raw_open.copy()
    membership = np.ones((n, k), dtype=bool)
    windows = [
        {
            "name": "first",
            "evaluation_start": dates_input[70].isoformat(),
            "evaluation_end": dates_input[78].isoformat(),
        },
        {
            "name": "second",
            "evaluation_start": dates_input[79].isoformat(),
            "evaluation_end": dates_input[89].isoformat(),
        },
    ]
    result = train_mlp_diagnostic(
        dates_input,
        symbols,
        features,
        raw_open,
        raw_close,
        membership,
        windows=windows,
        model_dir=tmp_path / "models",
    )
    assert result["metadata"]["model_parameters"] == MODEL_PARAMETERS
    assert np.isnan(result["predictions"][:70]).all()
    assert np.isfinite(result["predictions"][70:]).all()
    for evidence in result["training_evidence"]:
        assert evidence["maximum_label_exit_index"] < evidence["evaluation_start_index"]
        assert evidence["fit_evaluation_rows"] == 0
        assert evidence["loss_last"] <= evidence["loss_first"]
