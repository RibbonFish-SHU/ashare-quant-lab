"""Strict local evidence readers; no fetching and no inferred availability."""

from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re


class DividendEvidenceError(ValueError):
    """Evidence cannot establish the claimed identity or content."""


def require(condition, message):
    if not condition:
        raise DividendEvidenceError(message)


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, f"{label}: invalid integer")
    return value


def text(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"{label}: empty/non-string text")
    return value


def instant(value, label):
    text(value, label)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DividendEvidenceError(f"{label}: invalid timestamp") from exc
    require(parsed.utcoffset() is not None, f"{label}: naive timestamp")
    return parsed.astimezone(timezone.utc)


def day(value, label):
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value),
        f"{label}: invalid ISO date",
    )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DividendEvidenceError(f"{label}: invalid date") from exc


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    )


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise DividendEvidenceError(f"non-finite JSON number: {value}")

    def floating(value):
        result = float(value)
        require(math.isfinite(result), "non-finite JSON number")
        return result

    try:
        return json.loads(
            raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating
        )
    except (ValueError, UnicodeError) as exc:
        raise DividendEvidenceError(f"invalid strict JSON: {exc}") from exc


def failures(value):
    return {
        key: value[key]
        for key in ("error", "exception", "failure")
        if key in value and value[key] is not None
    }


class EvidenceReader:
    def __init__(self, root):
        self.root = Path(root).resolve()
        require(self.root.is_dir(), "evidence root is absent")
        self.references = {}

    def path(self, value):
        path = Path(value)
        path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        require(path.is_relative_to(self.root), "input path escapes evidence root")
        return path

    def read(self, value):
        path = self.path(value)
        require(path.is_file(), f"input absent: {path}")
        require(path.stat().st_size <= 32_000_000, "evidence file exceeds local reader limit")
        raw = path.read_bytes()
        ref = {
            "path": path.relative_to(self.root).as_posix(),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        previous = self.references.get(ref["path"])
        require(previous is None or previous == ref, f"input changed during audit: {path}")
        self.references[ref["path"]] = ref
        return raw, ref

    def reference(self, reference):
        require(
            isinstance(reference, dict) and set(reference) == {"path", "bytes", "sha256"},
            "invalid file reference",
        )
        relative = text(reference["path"], "reference path")
        require(
            not re.search(r"[:\\]", relative)
            and not relative.startswith("/")
            and all(part not in {"", ".", ".."} for part in relative.split("/")),
            "reference path must be a confined relative path",
        )
        integer(reference["bytes"], "reference bytes")
        require(
            isinstance(reference["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]),
            "invalid SHA256",
        )
        raw, actual = self.read(relative)
        require(actual == reference, f"file bytes/digest differ: {relative}")
        return raw

    def json(self, value):
        raw = self.reference(value) if isinstance(value, dict) else self.read(value)[0]
        return strict_json(raw)

    def unchanged(self):
        for ref in list(self.references.values()):
            self.reference(ref)
