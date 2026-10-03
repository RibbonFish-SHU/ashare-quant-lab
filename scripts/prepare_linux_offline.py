"""Select locked Linux wheels on a connected host, verify and measure before transfer.

Requires packaging from the project environment. Generate the input with:
uv export --frozen --extra dev --no-emit-project --output-file <export>
This command only downloads artifacts named and hashed in uv.lock.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tomllib
from urllib.parse import unquote, urlsplit
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def select_artifacts(lock_path, exported):
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    environment = default_environment()
    environment.update(
        sys_platform="linux",
        platform_system="Linux",
        os_name="posix",
        platform_machine="x86_64",
        python_version="3.11",
        python_full_version="3.11.16",
        implementation_name="cpython",
        implementation_version="3.11.16",
    )
    platforms = [f"manylinux_2_{v}_x86_64" for v in range(31, 4, -1)] + [
        "manylinux2014_x86_64",
        "manylinux2010_x86_64",
        "manylinux1_x86_64",
        "linux_x86_64",
    ]
    tags = list(cpython_tags((3, 11), abis=["cp311"], platforms=platforms)) + list(
        compatible_tags((3, 11), interpreter="cp311", platforms=platforms)
    )
    ranks = {tag: rank for rank, tag in enumerate(tags)}
    artifacts = []
    for line in exported.read_text(encoding="utf-8").replace("\\\n", " ").splitlines():
        line = line.split(" --hash=")[0].strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        packages = [
            p
            for p in lock["package"]
            if canonicalize_name(p["name"]) == canonicalize_name(requirement.name)
            and p["version"] in requirement.specifier
        ]
        if len(packages) != 1:
            raise ValueError(f"ambiguous locked requirement: {requirement}")
        package = packages[0]
        candidates = []
        for wheel in package.get("wheels", []):
            filename = unquote(urlsplit(wheel["url"]).path.rsplit("/", 1)[-1])
            matches = [ranks[tag] for tag in parse_wheel_filename(filename)[3] if tag in ranks]
            if matches:
                candidates.append((min(matches), wheel))
        if candidates:
            distribution = min(candidates, key=lambda item: item[0])[1]
            kind = "wheel"
        else:
            distribution = package["sdist"]
            kind = "sdist"
        filename = unquote(urlsplit(distribution["url"]).path.rsplit("/", 1)[-1])
        if Path(filename).name != filename:
            raise ValueError("artifact filename must be a single path component")
        artifacts.append(
            {
                "name": package["name"],
                "version": package["version"],
                "kind": kind,
                "filename": filename,
                **distribution,
            }
        )
    return {
        "python": "3.11.16",
        "platform": "Linux x86_64 glibc 2.31",
        # Git stores LF, while the Windows checkout may have CRLF.
        "lock_sha256": hashlib.sha256(lock_path.read_text(encoding="utf-8").encode()).hexdigest(),
        "artifacts": artifacts,
    }


def download(artifact, directory):
    expected = artifact["hash"].removeprefix("sha256:")
    path = directory / artifact["filename"]
    retrieval_url = artifact["url"].replace(
        "https://download-r2.pytorch.org/", "https://download.pytorch.org/"
    )
    if not path.exists() or digest(path) != expected:
        partial = path.with_name(path.name + ".partial")
        # curl detects truncated HTTP bodies; retry only failed artifacts.
        # Some large responses ended early without urllib raising IncompleteRead.
        subprocess.run(
            [
                "curl",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--retry",
                "3",
                "--retry-all-errors",
                "--connect-timeout",
                "20",
                "--max-time",
                "300",
                "--speed-limit",
                "1024",
                "--speed-time",
                "30",
                "--output",
                str(partial),
                retrieval_url,
            ],
            check=True,
            timeout=900,
        )
        if digest(partial) != expected:
            raise ValueError(f"official lock hash mismatch: {path.name}")
        partial.replace(path)
    size = path.stat().st_size
    if "size" in artifact and size != artifact["size"]:
        raise ValueError(f"locked artifact size mismatch: {path.name}")
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            unpacked = sum(item.file_size for item in archive.infolist())
            members = len(archive.infolist())
    else:
        with tarfile.open(path) as archive:
            unpacked = sum(item.size for item in archive.getmembers())
            members = len(archive.getmembers())
    return {
        **artifact,
        "compressed_bytes": size,
        "unpacked_bytes": unpacked,
        "archive_members": members,
        "verified_sha256": expected,
        "retrieval_url": retrieval_url,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    plan = select_artifacts(args.lock, args.export)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.download:
        distribution_dir = args.output / "distributions"
        distribution_dir.mkdir(exist_ok=True)
        measured = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(download, item, distribution_dir) for item in plan["artifacts"]
            ]
            for future in as_completed(futures):
                measured.append(future.result())
                if len(measured) % 20 == 0:
                    print(f"verified {len(measured)}/{len(futures)} artifacts", flush=True)
        plan["artifacts"] = sorted(measured, key=lambda item: item["name"])
        plan["compressed_bytes"] = sum(item["compressed_bytes"] for item in measured)
        plan["unpacked_bytes"] = sum(item["unpacked_bytes"] for item in measured)
        plan["archive_members"] = sum(item["archive_members"] for item in measured)
    (args.output / "manifest.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in plan.items() if key != "artifacts"}))


if __name__ == "__main__":
    main()
