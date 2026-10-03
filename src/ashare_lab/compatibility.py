"""Tiny real library calls on synthetic arrays, never strategy training."""

from pathlib import Path

import numpy as np


def run_compatibility(output: Path, seed: int, device: str, tables):
    # Device visibility is configured by the caller BEFORE importing torch or Qlib.
    import torch
    from lightgbm import LGBMRegressor
    import qlib
    from qlib.data import D
    from qlib.contrib.data.loader import Alpha158DL

    torch.set_num_threads(1)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(64, 8)).astype(np.float32)
    y = (x[:, 0] * 0.1 - x[:, 1] * 0.2).astype(np.float32)
    model = LGBMRegressor(
        n_estimators=4,
        max_depth=2,
        num_leaves=4,
        min_child_samples=4,
        random_state=seed,
        n_jobs=1,
        deterministic=True,
        force_col_wise=True,
        verbosity=-1,
    )
    model.fit(x, y)
    predictions = model.booster_.predict(x)
    if predictions.shape != (64,) or not np.isfinite(predictions).all():
        raise RuntimeError("LightGBM smoke returned invalid predictions")
    # LightGBM's native file API cannot write this Windows workspace's Unicode path.
    (output / "lightgbm-smoke.txt").write_text(model.booster_.model_to_string(), encoding="utf-8")

    selected = "cpu" if device == "cpu" else "cuda:0"
    if selected != "cpu" and (not torch.cuda.is_available() or torch.cuda.device_count() != 1):
        raise RuntimeError("expected exactly one visible, usable CUDA device")
    net = torch.nn.Sequential(torch.nn.Linear(8, 4), torch.nn.ReLU(), torch.nn.Linear(4, 1)).to(
        selected
    )
    optimizer = torch.optim.SGD(net.parameters(), lr=0.01)
    tensor_x, tensor_y = torch.from_numpy(x).to(selected), torch.from_numpy(y).to(selected)
    losses = []
    for _ in range(3):
        optimizer.zero_grad()
        loss = torch.nn.functional.mse_loss(net(tensor_x).squeeze(1), tensor_y)
        loss.backward()
        optimizer.step()
        losses.append(float(loss.detach().cpu()))
    if not all(np.isfinite(losses)):
        raise RuntimeError("PyTorch smoke returned nonfinite loss")
    with (output / "torch-smoke.pt").open("wb") as stream:
        torch.save(
            {"state_dict": net.cpu().state_dict(), "seed": seed, "data_kind": "synthetic"}, stream
        )

    # Qlib's native provider is built exclusively from this run's fixture.
    provider = output / "qlib_synthetic"
    (provider / "calendars").mkdir(parents=True)
    (provider / "instruments").mkdir()
    instrument_dir = provider / "features" / "sh600000"
    instrument_dir.mkdir(parents=True)
    rows = sorted(
        (
            r
            for r in tables["bars"].to_pylist()
            if r["symbol"] == "600000.SH" and r["record_version"] == 1
        ),
        key=lambda r: r["event_date"],
    )
    dates = [row["event_date"].isoformat() for row in rows]
    (provider / "calendars" / "day.txt").write_text("\n".join(dates) + "\n", encoding="utf-8")
    (provider / "instruments" / "all.txt").write_text(
        f"SH600000\t{dates[0]}\t{dates[-1]}\n", encoding="utf-8"
    )
    values = np.array([0.0] + [r["close"] for r in rows], dtype="<f4")
    (instrument_dir / "close.day.bin").write_bytes(values.tobytes())
    qlib.init(
        provider_uri=str(provider),
        region="cn",
        kernels=1,
        expression_cache=None,
        dataset_cache=None,
        exp_manager={
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {
                "uri": (output / "mlruns").as_uri(),
                "default_exp_name": "synthetic-compatibility",
            },
        },
    )
    frame = D.features(
        ["SH600000"], ["$close", "Ref($close, 1)"], start_time=dates[0], end_time=dates[-1]
    )
    if len(frame) != len(rows) or not np.allclose(frame["$close"], values[1:]):
        raise RuntimeError("Qlib native provider round-trip mismatch")
    if not np.allclose(frame["Ref($close, 1)"].iloc[1:], values[1:-1]):
        raise RuntimeError("Qlib lag expression misalignment")
    fields, names = Alpha158DL.get_feature_config()
    if len(fields) != 158 or len(names) != 158:
        raise RuntimeError("Qlib Alpha158 configuration is not 158 features")
    return {
        "lightgbm": {"fit_rows": 64, "prediction_rows": len(predictions)},
        "torch": {
            "device": selected,
            "cuda_build": torch.version.cuda,
            "steps": 3,
            "losses_engineering_only": losses,
            "device_name": torch.cuda.get_device_name(0) if selected != "cpu" else "CPU",
        },
        "qlib": {
            "provider": "synthetic",
            "rows": len(frame),
            "lag_checked": True,
            "alpha158_expression_count": len(fields),
        },
    }
