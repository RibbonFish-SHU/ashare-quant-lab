"""Validate retained release, archive and original request-plan bindings offline."""

from datetime import datetime
import json
from pathlib import Path

from ashare_lab.data.binding import validate_plan
from ashare_lab.data.queries import symbol
from .archive import iso_day, sha256_file


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_ref(path):
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def verified_file(root, locator, references):
    root = Path(root).resolve()
    path = (root / locator["path"]).resolve()
    require(
        path.is_relative_to(root) and path.is_file(), "reference path outside input root or absent"
    )
    observed = file_ref(path)
    require(observed["sha256"] == locator["sha256"], f"reference digest differs: {path}")
    if "bytes" in locator:
        require(observed["bytes"] == locator["bytes"], f"reference byte count differs: {path}")
    references.append(observed)
    return path


def planned_symbols(plan, start, end):
    require(
        start.year == end.year == 2023 and start <= end,
        "only explicit 2023 intervals are authorized",
    )
    queries = validate_plan(plan)
    bars = [q["parameters"] for q in queries if q["api"] == "query_history_k_data_plus"]
    codes = [p["code"] for p in bars]
    require(
        bool(codes) and len(set(codes)) == len(codes), "ambiguous or empty planned daily requests"
    )
    require(
        len(set(plan["codes"])) == len(plan["codes"]) and set(codes) == set(plan["codes"]),
        "plan code set differs from original daily requests",
    )
    require(
        all(iso_day(p["start_date"]) <= start <= end <= iso_day(p["end_date"]) for p in bars),
        "import interval outside original planned requests",
    )
    return sorted(symbol(code) for code in codes)


def load_source(description, root, archive, plan_path, start, end):
    config = read_json(description)
    require(config["schema_version"] == "community-import-source-v1", "unknown source description")
    require(config["research_eligible"] is False, "source cannot be research eligible")
    references = [file_ref(description)]
    documents = {}
    for role, locator in config["documents"].items():
        documents[role] = read_json(verified_file(root, locator, references))
    expected = config["release"]
    release, tags, download = (documents[x] for x in ("release", "tags", "download"))
    repository, tag = expected["repository"], expected["tag"]
    latest = iso_day(tag)
    require(tag == "2023-12-31", "only the pinned 2023-12-31 release tag is supported")
    require(
        latest.year == 2023 and end <= latest, "release is not the authorized historical vintage"
    )
    require(
        release["id"] == expected["release_id"]
        and release["tag_name"] == tag
        and release["published_at"] == expected["published_at"]
        and release["html_url"] == f"https://github.com/{repository}/releases/tag/{tag}"
        and release["draft"] is False
        and release["prerelease"] is False,
        "release identity or publication date differs",
    )
    matching_tags = [t for t in tags if t["ref"] == f"refs/tags/{tag}"]
    require(
        len(matching_tags) == 1
        and matching_tags[0]["object"]["type"] == "commit"
        and matching_tags[0]["object"]["sha"] == expected["tag_commit"],
        "tag commit differs",
    )
    assets = [a for a in release["assets"] if a["name"] == "qlib_bin.tar.gz"]
    require(len(assets) == 1, "ambiguous release asset")
    asset = assets[0]
    url = f"https://github.com/{repository}/releases/download/{tag}/qlib_bin.tar.gz"
    require(
        asset["id"] == expected["asset_id"]
        and asset["state"] == "uploaded"
        and asset["browser_download_url"] == url
        and asset["size"] == config["archive"]["bytes"]
        and asset["created_at"] == expected["asset_created_at"]
        and asset["updated_at"] == expected["asset_updated_at"],
        "release asset identity differs",
    )
    require(
        asset.get("digest") == expected["publisher_digest"] is None
        and expected["publisher_manifest_present"] is False,
        "historical publisher digest/manifest status differs",
    )
    require(
        download["complete"] is True
        and download["status"] == 200
        and not download.get("error")
        and not download.get("exception")
        and download["source"] == repository
        and download["tag"] == tag
        and download["release_id"] == expected["release_id"]
        and download["asset_id"] == expected["asset_id"]
        and download["url"] == url,
        "download status or identity differs",
    )
    require(
        all(
            int(download[k]) == asset["size"] for k in ("expected_bytes", "bytes", "content_length")
        )
        and download["sha256"] == config["archive"]["sha256"],
        "download size/digest differs",
    )
    began, observed = (datetime.fromisoformat(download[k]) for k in ("started_at", "completed_at"))
    published = datetime.fromisoformat(expected["published_at"].replace("Z", "+00:00"))
    require(
        began.tzinfo is not None and observed.tzinfo is not None and published <= began <= observed,
        "invalid actual observation timestamps",
    )
    archive_ref = file_ref(archive)
    require(
        archive_ref["bytes"] == config["archive"]["bytes"]
        and archive_ref["sha256"] == config["archive"]["sha256"],
        "archive size/digest differs",
    )
    plan_ref = file_ref(plan_path)
    require(
        plan_ref["sha256"] == config["plan"]["sha256"], "original securities plan digest differs"
    )
    plan = read_json(plan_path)
    symbols = planned_symbols(plan, start, end)
    references.extend([archive_ref, plan_ref])
    return {
        "config": config,
        "documents": documents,
        "references": references,
        "symbols": symbols,
        "start": start,
        "end": end,
        "archive": archive_ref,
        "observed_at_utc": observed,
        "release": expected,
        "release_latest": latest,
        "historical_publish_time": None,
        "available_time": None,
    }
