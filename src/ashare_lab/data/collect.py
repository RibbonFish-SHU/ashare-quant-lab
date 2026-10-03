"""Serial, resumable collection with persistent attempts and explicit failure states."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from ashare_lab.runtime import code_identity
from .queries import query_id, validate_query
from .raw import archive_attempt, cached_records, digest, save_json, utc_now


@contextmanager
def source_lock(directory):
    """Kernel-held lock, released on process exit; never infer safety from a stale PID."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "baostock.lock").open("a+b") as stream:
        stream.seek(0, 2)
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


def collect(
    plan_path,
    output,
    reuse_roots,
    *,
    project_root,
    access_state,
    sdk_wheel=None,
    offline=False,
    timeout=60,
    delay=0.25,
    budget=10000,
    retries=1,
):
    plan_path, output = Path(plan_path), Path(output).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    queries = [validate_query(q) for q in plan["queries"]]
    ids = [query_id(q) for q in queries]
    if len(set(ids)) != len(ids):
        raise ValueError("plan contains duplicate provider requests")
    if (
        not 10 <= timeout <= 120
        or not 0 <= retries <= 1
        or delay < 0.25
        or not 1 <= budget <= 10000
    ):
        raise ValueError("invalid bounded acquisition limits")
    if not offline and sdk_wheel is None:
        raise ValueError("network acquisition requires the pinned official SDK wheel")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "run.json"
    with source_lock(access_state):
        if path.exists():
            run = json.loads(path.read_text(encoding="utf-8"))
            if run["plan_sha256"] != digest(plan_path):
                raise ValueError("cannot resume with a different plan")
        else:
            run = {
                "schema_version": "source-run-v1",
                "data_kind": "real_raw",
                "research_eligible": False,
                "started_at_utc": utc_now(),
                "plan_sha256": digest(plan_path),
                "plan": plan,
                "queries": [],
                "access_rule_url": "https://www.baostock.com/blacklist",
                "limits": {
                    "serial_connections": 1,
                    "wire_delay_seconds": delay,
                    "daily_wire_budget": budget,
                    "query_timeout_seconds": timeout,
                    "transport_retries": retries,
                },
            }
        run.setdefault("executions", []).append(
            {
                "at": utc_now(),
                "source": code_identity(Path(project_root)),
                "offline": offline,
                "pid": os.getpid(),
                "limits": {
                    "timeout": timeout,
                    "delay": delay,
                    "budget": budget,
                    "retries": retries,
                },
            }
        )
        cache = cached_records([*reuse_roots, output])
        done = {r["query_id"]: r for r in run["queries"] if r["status"] in {"complete", "reused"}}
        run["status"] = "running"
        save_json(path, run)

        def log(event, **data):
            with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": utc_now(), "event": event, **data}) + "\n")

        for query, key in zip(queries, ids):
            if key in done:
                from .raw import read_reference

                read_reference(done[key]["raw_locator"])
                continue
            if key in cache:
                source = cache[key]
                entry = {**source, "status": "reused"}
                entry.pop("rows", None)
                entry["row_count"] = len(source["rows"])
                run["queries"].append(entry)
                log("reused", query_id=key, rows=entry["row_count"])
                save_json(path, run)
                continue
            if offline:
                run["queries"].append({**query, "query_id": key, "status": "cache_miss"})
                log("cache_miss", query_id=key)
                save_json(path, run)
                continue
            success = False
            for attempt in range(retries + 1):
                token = uuid.uuid4().hex
                task = output / "work" / token
                task.mkdir(parents=True)
                save_json(task / "request.json", query)
                command = [
                    sys.executable,
                    "-m",
                    "ashare_lab.data.worker",
                    "--request",
                    str(task / "request.json"),
                    "--progress",
                    str(task / "progress.json"),
                    "--sdk-wheel",
                    str(Path(sdk_wheel).resolve()),
                    "--ledger",
                    str(Path(access_state).resolve() / "wire-ledger.json"),
                    "--delay",
                    str(delay),
                    "--budget",
                    str(budget),
                ]
                log("request_started", query_id=key, attempt=attempt, command=command)
                timed_out = False
                with (task / "worker.log").open("wb") as stream:
                    try:
                        env = dict(os.environ)
                        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
                        result = subprocess.run(
                            command,
                            stdout=stream,
                            stderr=subprocess.STDOUT,
                            timeout=timeout,
                            cwd=project_root,
                            env=env,
                        )
                        exit_code = result.returncode
                    except subprocess.TimeoutExpired:
                        timed_out, exit_code = True, -1
                record = (
                    json.loads((task / "progress.json").read_text(encoding="utf-8"))
                    if (task / "progress.json").exists()
                    else {**query, "rows": [], "fields": []}
                )
                if timed_out or not record.get("status") or record.get("status") == "running":
                    record.update(
                        status="timeout" if timed_out else "worker_failure",
                        retryable=timed_out,
                        error_msg="incomplete response preserved",
                    )
                record.update(
                    query_id=key,
                    worker_exit_code=exit_code,
                    observed_at_utc=record.get("observed_at_utc", utc_now()),
                )
                if exit_code and record["status"] == "complete":
                    record["status"] = "worker_failure_after_response"
                archived = archive_attempt(record, output / "attempts" / key / token)
                entry = {k: v for k, v in record.items() if k != "rows"}
                entry.update(
                    raw_locator={"path": str(archived), "sha256": digest(archived)},
                    row_count=len(record["rows"]),
                )
                run["queries"].append(entry)
                log(
                    "request_finished",
                    query_id=key,
                    status=entry["status"],
                    rows=entry["row_count"],
                )
                save_json(path, run)
                if record["status"] == "complete":
                    success = True
                    break
                # Only a transport loss is retryable. Provider errors (including
                # access restrictions) end the batch; never switch endpoints.
                if not record.get("retryable", False):
                    break
                if attempt < retries:
                    time.sleep(2)
            if not success:
                run["status"] = "stopped_on_error"
                run["stopped_query_id"] = key
                save_json(path, run)
                break
        else:
            complete = {
                x["query_id"] for x in run["queries"] if x["status"] in {"complete", "reused"}
            }
            run["status"] = "complete" if set(ids) <= complete else "incomplete"
        run["finished_at_utc"] = utc_now()
        save_json(path, run)
    return run
