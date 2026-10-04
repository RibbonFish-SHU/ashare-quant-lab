"""Native Qlib Alpha158 and frozen expanding-window LightGBM, 2023 diagnostics only."""

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd


WARMUP_SESSIONS = 60
LABEL_FORMULA = "open[t+6] / open[t+1] - 1"
RANK_FORMULA = (
    "average_rank(pct=True) - 0.5 within signal-date conditional members with valid endpoints"
)
MODEL_PARAMETERS = {
    "objective": "regression",
    "n_estimators": 150,
    "learning_rate": 0.05,
    "num_leaves": 15,
    "max_depth": -1,
    "min_child_samples": 200,
    "colsample_bytree": 1.0,
    "subsample": 1.0,
    "reg_alpha": 0.0,
    "reg_lambda": 1.0,
    "n_jobs": 4,
    "random_state": 20261004,
    "deterministic": True,
    "force_col_wise": True,
    "verbosity": -1,
}
RAW_FIELDS = ("open", "high", "low", "close", "volume", "vwap")


class Alpha158DiagnosticError(ValueError):
    """A declared native-feature or time-split boundary cannot be established."""


def _require(condition, message):
    if not condition:
        raise Alpha158DiagnosticError(message)


def _date(value):
    _require(not isinstance(value, datetime), "dates must not be timestamps")
    try:
        result = value if type(value) is date else date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise Alpha158DiagnosticError("expected date or ISO date") from exc
    _require(result.year == 2023, "this development diagnostic accepts only 2023 dates")
    return result


def _axes(dates, symbols):
    days = tuple(_date(day) for day in dates)
    symbols = tuple(symbols)
    _require(bool(days) and list(days) == sorted(set(days)), "dates must be unique and ascending")
    _require(
        bool(symbols)
        and all(isinstance(s, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", s) for s in symbols),
        "invalid security symbols",
    )
    _require(list(symbols) == sorted(set(symbols)), "symbols must be unique and sorted")
    return days, symbols


def _prices(values, shape, name, *, volume=False):
    values = np.asarray(values, dtype=float)
    _require(values.shape == shape, f"{name} shape mismatch")
    valid = np.isfinite(values) & (values >= 0 if volume else values > 0)
    _require(np.all(valid | np.isnan(values)), f"invalid {name}; use NaN for missing values")
    return values


def _membership(values, shape):
    result = np.asarray(values)
    _require(result.shape == shape and result.dtype == bool, "membership must be a bool matrix")
    return result


def _digest_array(values):
    values = np.ascontiguousarray(values)
    digest = hashlib.sha256(str((values.shape, values.dtype.str)).encode())
    digest.update(values.tobytes())
    return digest.hexdigest()


def build_alpha158_features(dates, symbols, raw_fields, *, provider_path):
    """Write an isolated native provider and evaluate all 158 original expressions.

    The provider is new and local; no data downloads, fills, adjusted-factor
    inference, fitted normalization or future-year reads occur. Qlib's native
    binary values use float32, with NaN preserved. Its global provider/cache
    configuration is initialized for this task, so callers must run sequentially.
    """
    import qlib
    from qlib.contrib.data.loader import Alpha158DL
    from qlib.data import D

    days, symbols = _axes(dates, symbols)
    shape = (len(days), len(symbols))
    _require(
        isinstance(raw_fields, dict) and set(raw_fields) == set(RAW_FIELDS),
        "raw_fields must contain open/high/low/close/volume/vwap",
    )
    raw = {
        name: _prices(raw_fields[name], shape, name, volume=name == "volume") for name in RAW_FIELDS
    }
    provider = Path(provider_path).resolve()
    provider.mkdir(parents=True, exist_ok=False)
    (provider / "calendars").mkdir()
    (provider / "instruments").mkdir()
    day_text = [day.isoformat() for day in days]
    qlib_symbols = [symbol[7:] + symbol[:6] for symbol in symbols]
    (provider / "calendars" / "day.txt").write_text("\n".join(day_text) + "\n", encoding="utf-8")
    (provider / "instruments" / "all.txt").write_text(
        "".join(f"{symbol}\t{day_text[0]}\t{day_text[-1]}\n" for symbol in qlib_symbols),
        encoding="utf-8",
    )
    for j, symbol in enumerate(qlib_symbols):
        directory = provider / "features" / symbol.lower()
        directory.mkdir(parents=True)
        for name, values in raw.items():
            payload = np.concatenate(([0.0], values[:, j])).astype("<f4")
            (directory / f"{name}.day.bin").write_bytes(payload.tobytes())
    qlib.init(
        provider_uri=str(provider),
        region="cn",
        kernels=1,
        expression_cache=None,
        dataset_cache=None,
        clear_mem_cache=True,
        exp_manager={
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {
                "uri": (provider / "mlruns").as_uri(),
                "default_exp_name": "alpha158-2023-diagnostic",
            },
        },
    )
    expressions, names = Alpha158DL.get_feature_config()
    _require(len(expressions) == len(names) == 158, "native Alpha158 expression count changed")
    frame = D.features(
        qlib_symbols, expressions + ["$close"], start_time=day_text[0], end_time=day_text[-1]
    )
    index = pd.MultiIndex.from_product(
        [qlib_symbols, pd.DatetimeIndex(days)], names=["instrument", "datetime"]
    )
    values = (
        frame.reindex(index)
        .to_numpy(dtype=np.float32)
        .reshape(len(symbols), len(days), 159)
        .transpose(1, 0, 2)
    )
    _require(
        np.allclose(values[:, :, -1], raw["close"].astype(np.float32), equal_nan=True),
        "native provider close round-trip mismatch",
    )
    features = values[:, :, :158].copy()
    infinity_count = int(np.isinf(features).sum())
    features[~np.isfinite(features)] = np.nan
    return {
        "features": features,
        "feature_names": names,
        "metadata": {
            "data_kind": "real_diagnostic",
            "research_eligible": False,
            "engine": "qlib.contrib.data.loader.Alpha158DL.get_feature_config",
            "qlib_version": qlib.__version__,
            "expressions": expressions,
            "expression_sha256": hashlib.sha256(json.dumps(expressions).encode()).hexdigest(),
            "provider_path": str(provider),
            "provider_value_dtype": "little-endian float32",
            "shape": list(features.shape),
            "warmup_sessions": WARMUP_SESSIONS,
            "feature_fit_boundary": "No fitted preprocessing; native expressions use only current/past bars.",
            "missing_policy": "Preserve raw NaN, convert feature infinity to NaN, never forward/backfill.",
            "infinity_to_nan_count": infinity_count,
            "nan_count": int(np.isnan(features).sum()),
            "kernels": 1,
            "source_field_sha256": {name: _digest_array(raw[name]) for name in RAW_FIELDS},
        },
    }


def build_open_to_open_labels(dates, raw_open, membership):
    """Return raw future returns and member-only centered percentile rank targets.

    All signal dates are represented; the trainer separately applies the 60-day
    feature warmup, valid signal close and window-specific label maturity mask.
    """
    days = tuple(_date(day) for day in dates)
    _require(bool(days) and list(days) == sorted(set(days)), "dates must be unique and ascending")
    opens = np.asarray(raw_open, dtype=float)
    _require(opens.ndim == 2 and opens.shape[0] == len(days), "raw_open shape mismatch")
    opens = _prices(opens, opens.shape, "raw_open")
    members = _membership(membership, opens.shape)
    raw_returns = np.full(opens.shape, np.nan)
    labels = np.full(opens.shape, np.nan)
    counts = []
    for i, day in enumerate(days):
        member_count = int(members[i].sum())
        entry_missing = exit_missing = 0
        if i + 6 < len(days):
            entry, exit_price = opens[i + 1], opens[i + 6]
            valid = members[i] & np.isfinite(entry) & np.isfinite(exit_price)
            entry_missing = int((members[i] & ~np.isfinite(entry)).sum())
            exit_missing = int((members[i] & ~np.isfinite(exit_price)).sum())
            raw_returns[i, valid] = exit_price[valid] / entry[valid] - 1
            labels[i, valid] = (
                pd.Series(raw_returns[i, valid]).rank(method="average", pct=True).to_numpy() - 0.5
            )
        counts.append(
            {
                "signal_date": day.isoformat(),
                "members": member_count,
                "valid_labels": int(np.isfinite(labels[i]).sum()),
                "missing_entry": entry_missing,
                "missing_exit": exit_missing,
                "beyond_input_horizon": member_count if i + 6 >= len(days) else 0,
            }
        )
    return {
        "labels": labels,
        "raw_returns": raw_returns,
        "metadata": {
            "label_formula": LABEL_FORMULA,
            "rank_formula": RANK_FORMULA,
            "entry_offset": 1,
            "exit_offset": 6,
            "valid_label_count": int(np.isfinite(labels).sum()),
            "excluded_member_count": int(members.sum() - np.isfinite(labels).sum()),
            "per_signal_date": counts,
        },
    }


def train_alpha158_diagnostic(
    dates, symbols, features, raw_open, raw_close, membership, *, windows, model_dir
):
    """Fit each frozen expanding window using strictly matured training labels.

    Labels are rank targets; raw_returns are also returned for independent
    evaluation. Neither held-out labels nor evaluation features enter model.fit.
    The evaluation is 2023 development diagnostics, not a sealed/blind test.
    """
    from lightgbm import LGBMRegressor
    from qlib.contrib.data.loader import Alpha158DL

    days, symbols = _axes(dates, symbols)
    shape = (len(days), len(symbols))
    opens = _prices(raw_open, shape, "raw_open")
    closes = _prices(raw_close, shape, "raw_close")
    members = _membership(membership, shape)
    feature_values = np.asarray(features, dtype=np.float32)
    _require(feature_values.shape == (*shape, 158), "expected [day, security, 158] features")
    clean_features = np.where(np.isfinite(feature_values), feature_values, np.nan)
    _require(
        isinstance(windows, (list, tuple)) and len(windows) == 2,
        "two explicit evaluation windows required",
    )
    labels = build_open_to_open_labels(days, opens, members)
    predictions = np.full(shape, np.nan)
    output = Path(model_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    positions = {day.isoformat(): i for i, day in enumerate(days)}
    index_grid = np.arange(len(days))[:, None]
    common = (index_grid >= WARMUP_SESSIONS) & members & np.isfinite(closes)
    feature_names = Alpha158DL.get_feature_config()[1]
    evidence, model_paths, training_masks = [], [], {}
    last_end = -1
    for window in windows:
        _require(
            isinstance(window, dict)
            and set(window) == {"name", "evaluation_start", "evaluation_end"},
            "invalid window definition",
        )
        name = window["name"]
        _require(
            isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9_-]+", name)
            and name not in training_masks,
            "invalid or repeated window name",
        )
        start_text, end_text = (
            _date(window["evaluation_start"]).isoformat(),
            _date(window["evaluation_end"]).isoformat(),
        )
        _require(
            start_text in positions and end_text in positions,
            "window endpoint absent from calendar",
        )
        start, end = positions[start_text], positions[end_text]
        _require(
            WARMUP_SESSIONS < start <= end and start > last_end,
            "windows overlap or lack feature warmup",
        )
        train_mask = common & (index_grid + 6 < start) & np.isfinite(labels["labels"])
        predict_mask = common & (index_grid >= start) & (index_grid <= end)
        train_indices = np.argwhere(train_mask)
        predict_indices = np.argwhere(predict_mask)
        _require(
            len(train_indices) > 0 and len(predict_indices) > 0,
            "empty training or prediction window",
        )
        x_train, y_train = clean_features[train_mask], labels["labels"][train_mask]
        model = LGBMRegressor(**MODEL_PARAMETERS)
        model.fit(x_train, y_train, feature_name=feature_names)
        predicted = model.booster_.predict(clean_features[predict_mask], num_threads=4)
        _require(np.isfinite(predicted).all(), "nonfinite LightGBM predictions")
        predictions[predict_mask] = predicted
        model_text = model.booster_.model_to_string()
        model_path = output / f"{name}.txt"
        model_path.write_text(model_text, encoding="utf-8")
        model_paths.append(str(model_path))
        training_masks[name] = train_mask.copy()
        training_dates = train_indices[:, 0]
        eligible_past = common & (index_grid < start)
        evidence.append(
            {
                "window": name,
                "fit_as_of": days[start - 1].isoformat(),
                "train_signal_start": days[int(training_dates.min())].isoformat(),
                "train_signal_end": days[int(training_dates.max())].isoformat(),
                "maximum_label_exit": days[int(training_dates.max()) + 6].isoformat(),
                "maximum_label_exit_index": int(training_dates.max()) + 6,
                "evaluation_start_index": start,
                "maturity_rule": "label_exit_index < evaluation_start_index",
                "training_rows": len(train_indices),
                "prediction_rows": len(predict_indices),
                "prediction_start": start_text,
                "prediction_end": end_text,
                "warmup_member_rows_excluded": int(
                    ((index_grid < WARMUP_SESSIONS) & members).sum()
                ),
                "unmatured_past_rows_excluded": int(
                    (eligible_past & (index_grid + 6 >= start)).sum()
                ),
                "missing_label_past_rows_excluded": int(
                    (
                        eligible_past & (index_grid + 6 < start) & ~np.isfinite(labels["labels"])
                    ).sum()
                ),
                "missing_close_past_rows_excluded": int(
                    (
                        (index_grid >= WARMUP_SESSIONS)
                        & (index_grid < start)
                        & members
                        & ~np.isfinite(closes)
                    ).sum()
                ),
                "training_mask_sha256": _digest_array(train_mask),
                "training_features_sha256": _digest_array(x_train),
                "training_labels_sha256": _digest_array(y_train),
                "model_sha256": hashlib.sha256(model_text.encode("utf-8")).hexdigest(),
                "model_path": str(model_path),
                "actual_trees": model.booster_.num_trees(),
                "fit_evaluation_rows": 0,
                "early_stopping": False,
            }
        )
        last_end = end
    return {
        "predictions": predictions,
        "labels": labels["labels"],
        "raw_returns": labels["raw_returns"],
        "label_metadata": labels["metadata"],
        "training_evidence": evidence,
        "training_masks": training_masks,
        "model_paths": model_paths,
        "metadata": {
            "data_kind": "real_diagnostic",
            "research_eligible": False,
            "model_parameters": dict(MODEL_PARAMETERS),
            "label_formula": LABEL_FORMULA,
            "rank_formula": RANK_FORMULA,
            "warmup_sessions": WARMUP_SESSIONS,
            "feature_fit_boundary": "No fitted preprocessing; infinity becomes NaN, remaining NaN goes to LightGBM.",
            "training_boundary": "signal_index >= 60 and label_exit_index < evaluation_start_index",
            "prediction_universe": "conditional member with positive same-day raw close",
            "not_blind_test": True,
            "parameter_search": False,
        },
    }
