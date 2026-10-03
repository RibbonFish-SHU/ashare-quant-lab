import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

from .config import load_config
from .contracts import ContractError, as_of, members_at, require_feature_available
from .devices import inventory, select_device
from .runtime import Run, seed_process
from .storage import query_as_of, read_dataset, write_dataset
from .synthetic import at, fixture, sessions
from .temporal import next_open_window


def smoke(config, root, *, allocation_confirmed=False):
    run = Run(config, root)
    print(json.dumps({"run_directory": str(run.path)}, ensure_ascii=False), flush=True)
    try:
        seed_process(config.seed)
        device = select_device(config.device, allocation_confirmed=allocation_confirmed)
        run.log("device_selected", **device)
        tables = fixture(config.seed)
        path = write_dataset(run.path / "data", config.data_version, tables, data_kind="synthetic")
        restored = read_dataset(path)
        for name, table in tables.items():
            if not table.equals(restored[name]):
                raise RuntimeError(f"Parquet round-trip mismatch: {name}")
        cutoff = at(sessions()[0])
        point_in_time = query_as_of(path, "bars", cutoff)
        expected = as_of("bars", tables["bars"], cutoff)
        if sorted(point_in_time.to_pylist(), key=lambda r: r["symbol"]) != sorted(
            expected.to_pylist(), key=lambda r: r["symbol"]
        ):
            raise RuntimeError("DuckDB point-in-time query differs from reference")
        require_feature_available(point_in_time, cutoff)
        try:
            require_feature_available(tables["bars"], cutoff)
        except ContractError:
            future_rejected = True
        else:
            raise RuntimeError("future observation was accepted as a feature")
        run.log("storage_checked", rows={name: len(table) for name, table in restored.items()})
        window = next_open_window(
            tables["calendar"], sessions()[0], cutoff, at(sessions()[6], 15, 10)
        )
        from .compatibility import run_compatibility

        compatibility = run_compatibility(run.path, config.seed, config.device, tables)
        result = {
            "data_kind": "synthetic",
            "dataset": str(path),
            "future_rejected": future_rejected,
            "parquet_roundtrip": True,
            "duckdb_asof": True,
            "rows": {name: len(table) for name, table in tables.items()},
            "initial_members": members_at(tables["membership"], sessions()[0], cutoff),
            "label_boundary_example": asdict(window),
            "compatibility": compatibility,
            "real_data_acceptance": "pending provider and history range",
            "formal_research": "not run",
        }
        run.finish(result=result)
        return run.path
    except Exception as exc:
        run.finish(error=exc)
        raise


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Phase 0 synthetic engineering tools")
    sub = parser.add_subparsers(dest="command", required=True)
    smoke_parser = sub.add_parser("smoke")
    smoke_parser.add_argument("--config", type=Path, default=Path("configs/smoke.toml"))
    smoke_parser.add_argument("--project-root", type=Path, default=Path.cwd())
    smoke_parser.add_argument("--gpu", help="explicit GPU UUID; no automatic device allocation")
    smoke_parser.add_argument(
        "--allocation-confirmed",
        action="store_true",
        help="use only after resource owner confirms allocation/booking",
    )
    sub.add_parser("devices", help="read-only inventory; does not establish resource permission")
    args = parser.parse_args(argv)
    try:
        if args.command == "devices":
            print(json.dumps(inventory(), indent=2))
            return 0
        root = args.project_root.resolve()
        config_path = args.config if args.config.is_absolute() else root / args.config
        config = load_config(config_path, project_root=root)
        if args.gpu:
            config = replace(config, device=args.gpu)
        run = smoke(config, root, allocation_confirmed=args.allocation_confirmed)
        print(json.dumps({"status": "passed", "run_directory": str(run)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
