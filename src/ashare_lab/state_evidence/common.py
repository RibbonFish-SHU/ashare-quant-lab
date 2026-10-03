"""Small validation helpers; timestamps are never inferred from dates."""

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from ashare_lab.runtime import code_identity


def require(condition, message):
    if not condition:
        raise ValueError(message)


def day(value):
    require(isinstance(value, (date, str)) and not isinstance(value, datetime), "invalid date")
    return date.fromisoformat(value) if isinstance(value, str) else value


def instant(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    require(isinstance(result, datetime) and result.utcoffset() is not None, "naive timestamp")
    return result.astimezone(timezone.utc)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def save_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path)}


def checked_file(root, reference):
    root = Path(root).resolve()
    relative = reference["path"].replace("\\", "/")
    path = (root / relative).resolve()
    require(path.is_relative_to(root) and path.is_file(), "input path outside root or absent")
    require(
        path.stat().st_size == reference["bytes"] and digest(path) == reference["sha256"],
        f"input bytes/digest differ: {relative}",
    )
    return path


def verify_unchanged(references):
    for reference in references:
        require(file_ref(reference["path"]) == reference, f"input changed: {reference['path']}")


def execution_identity(project_root):
    """Reject a main-cwd/worktree-PYTHONPATH mismatch before inspecting a claimed commit."""
    root = Path(project_root).resolve()
    module = Path(__file__).resolve()
    require(module.is_relative_to(root), "state module is outside project_root")
    actual_root = Path(
        subprocess.check_output(
            ["git", "-C", str(module.parent), "rev-parse", "--show-toplevel"],
            encoding="utf-8",
            timeout=15,
        ).strip()
    ).resolve()
    require(actual_root == root, "state module belongs to a different project_root")
    source = code_identity(root)
    require(not source["dirty"], "stable clean source commit required")
    for name, imported in tuple(sys.modules.items()):
        if name == "ashare_lab" or name.startswith("ashare_lab."):
            path = getattr(imported, "__file__", None)
            require(
                path and Path(path).resolve().is_relative_to(root / "src"),
                f"imported module outside project_root: {name}",
            )
    bound = {}
    selected_files = list((root / "src" / "ashare_lab").rglob("*.py"))
    selected_files.extend((root / "configs").glob("state-evidence-*.json"))
    for path in sorted(selected_files):
        relative = path.relative_to(root).as_posix()
        actual = path.read_bytes()
        require(
            hashlib.sha256(actual).hexdigest() == source["files_sha256"].get(relative),
            f"source digest differs: {relative}",
        )
        committed = subprocess.check_output(
            ["git", "-C", str(root), "show", f"{source['commit']}:{relative}"], timeout=15
        )
        require(
            actual.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"),
            f"source differs from commit: {relative}",
        )
        bound[relative] = {
            "sha256": source["files_sha256"][relative],
            "commit_blob_sha256": hashlib.sha256(committed).hexdigest(),
        }
    source["state_module"] = {
        "path": str(module.parent),
        "commit": source["commit"],
        "files": bound,
        "comparison": "exact bytes after CRLF-to-LF normalization only",
    }
    return source
