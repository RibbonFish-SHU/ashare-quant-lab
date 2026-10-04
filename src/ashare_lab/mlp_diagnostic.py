"""Fixed CPU MLP baseline for the 2023 Alpha158 development diagnostic."""

import hashlib
from pathlib import Path
import re

import numpy as np

from .alpha158_diagnostic import (
    WARMUP_SESSIONS,
    _axes,
    _digest_array,
    _membership,
    _prices,
    build_open_to_open_labels,
)


MODEL_PARAMETERS = {
    "architecture": [158, 64, 32, 1],
    "activation": "ReLU",
    "loss": "MSELoss",
    "optimizer": "Adam",
    "epochs": 40,
    "batch_size": 2048,
    "learning_rate": 0.001,
    "weight_decay": 0.0001,
    "shuffle": False,
    "seed": 20261005,
    "cpu_threads": 1,
    "early_stopping": False,
    "parameter_search": False,
}


class MLPDiagnosticError(ValueError):
    """A fixed MLP diagnostic boundary or input is invalid."""


def _require(condition, message):
    if not condition:
        raise MLPDiagnosticError(message)


def _clean_features(features, shape):
    values = np.asarray(features, dtype=np.float32)
    _require(values.shape == (*shape, 158), "expected [day, security, 158] features")
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)


def _model():
    import torch

    return torch.nn.Sequential(
        torch.nn.Linear(158, 64),
        torch.nn.ReLU(),
        torch.nn.Linear(64, 32),
        torch.nn.ReLU(),
        torch.nn.Linear(32, 1),
    )


def _torch_setup():
    import torch

    torch.set_num_threads(MODEL_PARAMETERS["cpu_threads"])
    torch.set_num_interop_threads(1)
    torch.manual_seed(MODEL_PARAMETERS["seed"])
    torch.use_deterministic_algorithms(True)
    return torch


def train_mlp_diagnostic(
    dates, symbols, features, raw_open, raw_close, membership, *, windows, model_dir
):
    """Fit two fixed expanding-window CPU MLPs with strictly mature labels."""
    torch = _torch_setup()
    days, symbols = _axes(dates, symbols)
    shape = (len(days), len(symbols))
    opens = _prices(raw_open, shape, "raw_open")
    closes = _prices(raw_close, shape, "raw_close")
    members = _membership(membership, shape)
    clean = _clean_features(features, shape)
    _require(isinstance(windows, (list, tuple)) and len(windows) == 2, "two windows required")
    labels = build_open_to_open_labels(days, opens, members)
    positions = {day.isoformat(): i for i, day in enumerate(days)}
    grid = np.arange(len(days))[:, None]
    common = (grid >= WARMUP_SESSIONS) & members & np.isfinite(closes)
    predictions = np.full(shape, np.nan)
    output = Path(model_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    evidence, model_paths, training_masks = [], [], {}
    last_end = -1
    for window in windows:
        _require(
            isinstance(window, dict)
            and set(window) == {"name", "evaluation_start", "evaluation_end"},
            "invalid window",
        )
        name = window["name"]
        _require(
            isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9_-]+", name)
            and name not in training_masks,
            "invalid or repeated window name",
        )
        start_text, end_text = window["evaluation_start"], window["evaluation_end"]
        _require(start_text in positions and end_text in positions, "window endpoint absent")
        start, end = positions[start_text], positions[end_text]
        _require(WARMUP_SESSIONS < start <= end and start > last_end, "overlapping or early window")
        training_mask = common & (grid + 6 < start) & np.isfinite(labels["labels"])
        prediction_mask = common & (grid >= start) & (grid <= end)
        train_indices = np.argwhere(training_mask)
        prediction_indices = np.argwhere(prediction_mask)
        _require(len(train_indices) > 0 and len(prediction_indices) > 0, "empty MLP fit window")
        x_train = clean[training_mask]
        y_train = labels["labels"][training_mask].astype(np.float32)
        model = _model()
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=MODEL_PARAMETERS["learning_rate"],
            weight_decay=MODEL_PARAMETERS["weight_decay"],
        )
        loss_fn = torch.nn.MSELoss()
        x_tensor = torch.from_numpy(x_train)
        y_tensor = torch.from_numpy(y_train[:, None])
        losses = []
        for _ in range(MODEL_PARAMETERS["epochs"]):
            optimizer.zero_grad(set_to_none=True)
            prediction = model(x_tensor)
            loss = loss_fn(prediction, y_tensor)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        with torch.no_grad():
            predicted = model(torch.from_numpy(clean[prediction_mask])).squeeze(1).numpy()
        _require(np.isfinite(predicted).all(), "nonfinite MLP predictions")
        predictions[prediction_mask] = predicted
        model_path = output / f"{name}.pt"
        torch.save(
            {
                "state_dict": model.state_dict(),
                "model_parameters": MODEL_PARAMETERS,
                "feature_cleaning": "nan/positive-infinity/negative-infinity -> 0",
                "window": name,
            },
            model_path,
        )
        model_paths.append(str(model_path))
        training_masks[name] = training_mask.copy()
        train_days = train_indices[:, 0]
        eligible_past = common & (grid < start)
        model_bytes = model_path.read_bytes()
        evidence.append(
            {
                "window": name,
                "fit_as_of": days[start - 1].isoformat(),
                "train_signal_start": days[int(train_days.min())].isoformat(),
                "train_signal_end": days[int(train_days.max())].isoformat(),
                "maximum_label_exit": days[int(train_days.max()) + 6].isoformat(),
                "maximum_label_exit_index": int(train_days.max()) + 6,
                "evaluation_start_index": start,
                "maturity_rule": "label_exit_index < evaluation_start_index",
                "training_rows": len(train_indices),
                "prediction_rows": len(prediction_indices),
                "prediction_start": start_text,
                "prediction_end": end_text,
                "training_mask_sha256": _digest_array(training_mask),
                "training_features_sha256": _digest_array(x_train),
                "training_labels_sha256": _digest_array(y_train),
                "model_sha256": hashlib.sha256(model_bytes).hexdigest(),
                "model_path": str(model_path),
                "loss_first": losses[0],
                "loss_last": losses[-1],
                "losses": losses,
                "fit_evaluation_rows": 0,
                "early_stopping": False,
                "unmatured_past_rows_excluded": int((eligible_past & (grid + 6 >= start)).sum()),
                "missing_label_past_rows_excluded": int(
                    (eligible_past & (grid + 6 < start) & ~np.isfinite(labels["labels"])).sum()
                ),
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
            "feature_cleaning": "nan/positive-infinity/negative-infinity -> 0",
            "label_formula": "open[t+6] / open[t+1] - 1",
            "training_boundary": "signal_index >= 60 and label_exit_index < evaluation_start_index",
            "prediction_universe": "conditional member with positive same-day raw close",
            "not_blind_test": True,
            "parameter_search": False,
        },
    }
