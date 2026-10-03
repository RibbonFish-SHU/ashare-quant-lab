"""At most ten missing 2023 securities, one bounded Eastmoney connection at a time."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from ashare_lab.data.cli import access_directory
from ashare_lab.data.pipeline import load_run
from ashare_lab.data.queries import query_id
from ashare_lab.data.raw import digest, save_json, utc_now
from ashare_lab.runtime import code_identity
from .pipeline import load_capture
from .protocol import SourceError, decode_response, eastmoney_request, request_identity, request_url


def plan_missing(run_path, original_plan, limit=10):
    if not 1 <= limit <= 10:
        raise ValueError("authorized pilot is at most ten missing securities")
    successful, _, run = load_run(run_path, original_plans=[original_plan])
    done = {r["query_id"] for r in successful}
    missing = [
        q
        for q in run["plan"]["queries"]
        if q["api"] == "query_history_k_data_plus" and query_id(q) not in done
    ]
    for query in missing:
        if (query["parameters"]["start_date"], query["parameters"]["end_date"]) != (
            "2023-01-01",
            "2023-12-31",
        ):
            raise ValueError("pilot origin must be full-year 2023 daily requests")
    codes = sorted({q["parameters"]["code"] for q in missing})
    return {
        "schema_version": "fallback-pilot-plan-v1",
        "provider": "eastmoney",
        "research_eligible": False,
        "origin_run": {"path": str(Path(run_path).resolve()), "sha256": digest(run_path)},
        "origin_plan": {
            "path": str(Path(original_plan).resolve()),
            "sha256": digest(original_plan),
        },
        "missing_baostock_bar_queries": len(missing),
        "missing_codes": codes,
        "selection": "first codes in lexical order; no price/performance selection",
        "max_securities": limit,
        "requests": [eastmoney_request(code) for code in codes[:limit]],
        "limits": {
            "serial": True,
            "min_start_interval_seconds": 1,
            "worker_timeout_seconds": 30,
            "socket_timeout_seconds": 15,
            "transport_retries": 1,
            "max_response_bytes": 2097152,
        },
    }


def verify_plan(plan):
    if (
        plan.get("schema_version") != "fallback-pilot-plan-v1"
        or plan.get("provider") != "eastmoney"
    ):
        raise ValueError("unsupported fallback pilot plan")
    for name in ("origin_run", "origin_plan"):
        if digest(plan[name]["path"]) != plan[name]["sha256"]:
            raise ValueError("fallback origin digest differs")
    expected = plan_missing(
        plan["origin_run"]["path"], plan["origin_plan"]["path"], plan["max_securities"]
    )
    if expected != plan or not 1 <= len(plan["requests"]) <= 10:
        raise ValueError("pilot plan differs from verified missing-code selection")


@contextmanager
def collector_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "collector.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def fetch(request, directory):
    """Worker: archive body bytes and HTTP metadata, including failed/partial responses."""
    identity = request_identity(request)
    if identity["provider"] != "eastmoney":
        raise ValueError("Tencent is offline-only in this batch")
    directory = Path(directory)
    url = request_url(request)
    metadata = {
        "provider": "eastmoney",
        "parameters": request["parameters"],
        "query_id": identity["query_id"],
        "url": url,
        "final_url": url,
        "started_at_utc": utc_now(),
        "status": None,
        "transfer_complete": False,
        "classification": "running",
        "worker_pid": os.getpid(),
        "research_eligible": False,
        "headers": {},
    }
    save_json(directory / "metadata.json", metadata)
    body_path = directory / "response.bin"
    body = bytearray()
    try:
        opener = urllib.request.build_opener(NoRedirect())
        outgoing = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ashare-quant-lab/0.1 data-validation",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        try:
            response = opener.open(outgoing, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response, body_path.open("xb") as stream:
            metadata.update(
                status=response.status, final_url=response.url, headers=dict(response.headers)
            )
            while True:
                block = response.read(min(65536, 2097153 - len(body)))
                if not block:
                    metadata["transfer_complete"] = True
                    break
                body.extend(block)
                stream.write(block)
                stream.flush()
                if len(body) > 2097152:
                    raise SourceError("response_too_large", "response exceeds pilot byte cap")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) != len(body):
            metadata["transfer_complete"] = False
            raise SourceError(
                "incomplete_transfer", "HTTP Content-Length differs from received bytes"
            )
        if metadata["status"] != 200:
            raise SourceError(
                "restricted" if metadata["status"] in {401, 403, 429} else "http_error",
                f"HTTP {metadata['status']}; no redirect or endpoint retry",
            )
        decode_response(bytes(body), request)
        metadata["classification"] = "complete"
    except SourceError as exc:
        metadata.update(classification=exc.status, error=str(exc))
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        metadata.update(classification="transport_error", exception=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        metadata.update(classification="protocol_error", exception=f"{type(exc).__name__}: {exc}")
    if not body_path.exists():
        body_path.write_bytes(bytes(body))
    metadata.update(
        captured_at_utc=utc_now(), bytes=body_path.stat().st_size, sha256=digest(body_path)
    )
    save_json(directory / "metadata.json", metadata)
    return metadata


def load_collection(path):
    path = Path(path)
    run = json.loads(path.read_text(encoding="utf-8"))
    if run.get("schema_version") != "public-http-run-v1":
        raise ValueError("unsupported HTTP run")
    plan_path = path.parent / "plan.original.json"
    if digest(plan_path) != run["plan_sha256"]:
        raise ValueError("HTTP original plan digest differs")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan != run["plan"]:
        raise ValueError("embedded HTTP plan differs")
    verify_plan(plan)
    expected = {request_identity(r)["query_id"]: r for r in plan["requests"]}
    successful = {}
    for entry in run["attempts"]:
        if entry["query_id"] not in expected:
            raise ValueError("unexpected HTTP attempt")
        if entry["status"] != "complete":
            continue
        record = load_capture(
            entry["metadata_path"],
            entry["response_path"],
            metadata_sha256=entry["metadata_sha256"],
            response_sha256=entry["response_sha256"],
        )
        if (
            record["query_id"] != entry["query_id"]
            or record["parameters"] != expected[entry["query_id"]]["parameters"]
        ):
            raise ValueError("HTTP raw request differs from run/plan")
        if entry["query_id"] in successful:
            raise ValueError("conflicting successful HTTP attempt versions")
        successful[entry["query_id"]] = record
    return list(successful.values()), run


def collect(plan_path, output, *, project_root):
    plan_path, output = Path(plan_path), Path(output).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    verify_plan(plan)
    access = access_directory(project_root).with_name("eastmoney-source-access")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "run.json"
    with collector_lock(access):
        if path.exists():
            prior, run = load_collection(path)
            if run["plan_sha256"] != digest(plan_path):
                raise ValueError("cannot change HTTP plan while resuming")
        else:
            prior = []
            with (output / "plan.original.json").open("xb") as stream:
                stream.write(plan_path.read_bytes())
            run = {
                "schema_version": "public-http-run-v1",
                "plan": plan,
                "plan_sha256": digest(plan_path),
                "research_eligible": False,
                "started_at_utc": utc_now(),
                "attempts": [],
                "executions": [],
            }
        run["executions"].append(
            {
                "at": utc_now(),
                "pid": os.getpid(),
                "source": code_identity(Path(project_root)),
                "access_state": str(access),
            }
        )
        done = {r["query_id"] for r in prior}
        run["status"] = "running"
        save_json(path, run)
        ledger_path = access / "wire-ledger.json"
        ledger = (
            json.loads(ledger_path.read_text(encoding="utf-8"))
            if ledger_path.exists()
            else {"requests": []}
        )
        stop = False
        for request in plan["requests"]:
            key = request_identity(request)["query_id"]
            if key in done:
                continue
            attempts = sum(e["query_id"] == key for e in run["attempts"])
            while attempts < 2:
                restriction = access / "source-restriction.json"
                if restriction.exists():
                    run["status"], stop = "stopped_on_source_restriction", True
                    break
                time.sleep(max(0, 1 - (time.time() - ledger.get("last_request_start_epoch", 0))))
                token = uuid.uuid4().hex
                directory = output / "attempts" / key / token
                directory.mkdir(parents=True)
                save_json(directory / "request.json", request)
                ledger["last_request_start_epoch"] = time.time()
                ledger["requests"].append(
                    {"at": utc_now(), "query_id": key, "run": str(path), "attempt": token}
                )
                save_json(ledger_path, ledger)
                command = [
                    sys.executable,
                    "-m",
                    "ashare_lab.fallback.collect",
                    str(directory / "request.json"),
                    str(directory),
                ]
                env = dict(os.environ)
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
                timed_out = False
                with (directory / "worker.log").open("wb") as log:
                    try:
                        result = subprocess.run(
                            command,
                            cwd=project_root,
                            env=env,
                            timeout=30,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                        )
                        exit_code = result.returncode
                    except subprocess.TimeoutExpired:
                        timed_out, exit_code = True, -1
                metadata_path, response_path = (
                    directory / "metadata.json",
                    directory / "response.bin",
                )
                metadata = (
                    json.loads(metadata_path.read_text(encoding="utf-8"))
                    if metadata_path.exists()
                    else {}
                )
                status = (
                    "worker_timeout"
                    if timed_out
                    else metadata.get("classification", "worker_failure")
                )
                if status == "complete":
                    if exit_code != 0:
                        status = "worker_failure_after_response"
                    else:
                        try:
                            record = load_capture(metadata_path, response_path)
                            if record["query_id"] != key:
                                raise ValueError("worker returned another request")
                        except (ValueError, KeyError, OSError, TypeError) as exc:
                            status = "invalid_complete_metadata"
                            metadata["error"] = f"{type(exc).__name__}: {exc}"
                entry = {
                    "query_id": key,
                    "status": status,
                    "worker_exit": exit_code,
                    "metadata_path": str(metadata_path),
                    "response_path": str(response_path),
                    "metadata_sha256": digest(metadata_path) if metadata_path.exists() else None,
                    "response_sha256": digest(response_path) if response_path.exists() else None,
                    "at": utc_now(),
                    "error": metadata.get("error", metadata.get("exception")),
                }
                run["attempts"].append(entry)
                attempts += 1
                save_json(path, run)
                print(
                    json.dumps(
                        {
                            "query_id": key,
                            "status": status,
                            "completed": len(done) + (status == "complete"),
                        }
                    ),
                    flush=True,
                )
                if status == "complete":
                    done.add(key)
                    break
                if status not in {"transport_error", "worker_timeout"}:
                    save_json(
                        restriction,
                        {
                            "provider": "eastmoney",
                            "status": status,
                            "at": utc_now(),
                            "attempt": entry,
                            "automatic_retry": False,
                        },
                    )
                if status not in {"transport_error", "worker_timeout"} or attempts == 2:
                    run["status"], stop = "stopped_on_error", True
                    break
            if attempts >= 2 and key not in done:
                run["status"], stop = "stopped_on_retry_limit", True
            if stop:
                break
        else:
            run["status"] = "complete" if len(done) == len(plan["requests"]) else "incomplete"
        run["finished_at_utc"] = utc_now()
        save_json(path, run)
    return run


if __name__ == "__main__":
    request_file, destination = map(Path, sys.argv[1:])
    result = fetch(json.loads(request_file.read_text(encoding="utf-8")), destination)
    raise SystemExit(0 if result["classification"] == "complete" else 2)
