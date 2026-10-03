from dataclasses import asdict, dataclass
from pathlib import Path
import re
import tomllib


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RunConfig:
    seed: int
    output_root: Path
    data_kind: str
    data_version: str
    feature_version: str
    model_version: str
    device: str

    def as_dict(self):
        result = asdict(self)
        result["output_root"] = str(self.output_root)
        return result


def load_config(path: Path, *, project_root: Path) -> RunConfig:
    try:
        values = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load configuration {path}: {exc}") from exc
    expected = set(RunConfig.__dataclass_fields__)
    missing, extra = expected - values.keys(), values.keys() - expected
    if missing or extra:
        raise ConfigError(f"configuration missing={sorted(missing)}, unknown={sorted(extra)}")
    if type(values["seed"]) is not int or not 0 <= values["seed"] < 2**32:
        raise ConfigError("seed must be an integer in [0, 2**32)")
    if values["data_kind"] != "synthetic":
        raise ConfigError("Phase 0 smoke accepts data_kind='synthetic' only")
    for key in ("data_version", "feature_version", "model_version"):
        if not isinstance(values[key], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", values[key]
        ):
            raise ConfigError(f"{key} must be a nonempty version identifier")
    if not values["data_version"].startswith("synthetic-"):
        raise ConfigError("smoke data_version must start with synthetic-")
    device = values["device"]
    if not isinstance(device, str) or not (
        device == "cpu" or re.fullmatch(r"GPU-[a-fA-F0-9-]+", device)
    ):
        raise ConfigError("device must be cpu or an explicit NVIDIA GPU UUID")
    if not isinstance(values["output_root"], str) or not values["output_root"].strip():
        raise ConfigError("output_root must be a nonempty path")
    root = project_root.resolve()
    output = (root / values["output_root"]).resolve()
    allowed = root / "artifacts"
    if not output.is_relative_to(allowed):
        raise ConfigError("output_root must be inside this project's artifacts directory")
    values["output_root"] = output
    return RunConfig(**values)
