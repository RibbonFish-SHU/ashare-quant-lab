"""Serial, resumable collection; interpreter reuse never means concurrent connections."""

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
from .raw import archive_attempt, cached_records, digest, read_reference, save_json, utc_now


@contextmanager
def source_lock(directory):
    """Kernel-held lock; released on process exit, never inferred from a stale PID."""
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


def launch_batch(
    queries, output, *, project_root, sdk_wheel, access_state, timeout, delay, budget, log
):
    token = uuid.uuid4().hex
    task = output / "work" / token
    task.mkdir(parents=True)
    tasks = []
    for index, query in enumerate(queries):
        request, progress = task / f"request-{index}.json", task / f"progress-{index}.json"
        save_json(request, query)
        tasks.append({"request": str(request), "progress": str(progress)})
    common = [
        "--sdk-wheel",
        str(Path(sdk_wheel).resolve()),
        "--ledger",
        str(Path(access_state).resolve() / "wire-ledger.json"),
        "--delay",
        str(delay),
        "--budget",
        str(budget),
    ]
    if len(tasks) == 1:
        command = [
            sys.executable,
            "-m",
            "ashare_lab.data.worker",
            "--request",
            tasks[0]["request"],
            "--progress",
            tasks[0]["progress"],
            *common,
        ]
    else:
        save_json(task / "tasks.json", tasks)
        command = [
            sys.executable,
            "-m",
            "ashare_lab.data.batch_worker",
            "--tasks",
            str(task / "tasks.json"),
            "--receipt",
            str(task / "receipt.json"),
            *common,
        ]
    log("batch_started", query_ids=[query_id(q) for q in queries], command=command)
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
    receipt = (
        json.loads((task / "receipt.json").read_text(encoding="utf-8"))
        if (task / "receipt.json").exists()
        else {"finished": [], "running_index": 0}
    )
    finished = {r["index"]: r["exit_code"] for r in receipt["finished"]}
    entries = []
    for index, (query, paths) in enumerate(zip(queries, tasks)):
        progress = Path(paths["progress"])
        if not progress.exists() and index != receipt["running_index"]:
            break  # Never turn unattempted queries into empty responses.
        record = (
            json.loads(progress.read_text(encoding="utf-8"))
            if progress.exists()
            else {**query, "rows": [], "fields": []}
        )
        query_exit = exit_code if len(tasks) == 1 else finished.get(index)
        failed_provider = record.get("error_code") not in {None, "0"}
        if (
            query_exit is None
            or record.get("status") in {None, "running"}
            or (timed_out and len(tasks) == 1 and not failed_provider)
        ):
            if not failed_provider:
                record.update(
                    status="timeout" if timed_out else "worker_failure",
                    retryable=timed_out,
                    error_msg="incomplete response preserved",
                )
        if query_exit and record.get("status") == "complete":
            record.update(status="worker_failure_after_response", retryable=False)
        record.update(
            query_id=query_id(query),
            worker_exit_code=query_exit,
            batch_worker_exit_code=exit_code,
            observed_at_utc=record.get("observed_at_utc", utc_now()),
        )
        archived = archive_attempt(record, output / "attempts" / record["query_id"] / token)
        entry = {k: v for k, v in record.items() if k != "rows"}
        entry.update(
            raw_locator={"path": str(archived), "sha256": digest(archived)},
            row_count=len(record["rows"]),
        )
        entries.append(entry)
        log(
            "request_finished",
            query_id=entry["query_id"],
            status=entry["status"],
            rows=entry["row_count"],
        )
        if record["status"] != "complete":
            break
    return entries


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
    batch_size=1,
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
        or not 1 <= batch_size <= 20
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
                "access_state": str(Path(access_state).resolve()),
                "limits": {
                    "timeout": timeout,
                    "timeout_scope": "whole_worker_batch",
                    "delay": delay,
                    "budget": budget,
                    "retries": retries,
                    "batch_size": batch_size,
                },
            }
        )
        cache = cached_records([*reuse_roots, output])
        done = {r["query_id"] for r in run["queries"] if r["status"] in {"complete", "reused"}}
        for entry in run["queries"]:
            if entry["status"] in {"complete", "reused"}:
                read_reference(entry["raw_locator"])
        run["status"] = "running"

        def log(event, **data):
            with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": utc_now(), "event": event, **data}) + "\n")

        for query, key in zip(queries, ids):
            if key in done:
                continue
            if key in cache:
                source = cache[key]
                entry = {k: v for k, v in source.items() if k != "rows"}
                entry.update(status="reused", row_count=len(source["rows"]))
                run["queries"].append(entry)
                done.add(key)
                log("reused", query_id=key, rows=entry["row_count"])
            elif offline:
                run["queries"].append({**query, "query_id": key, "status": "cache_miss"})
        save_json(path, run)
        attempts = {}
        while not offline and set(ids) - done:
            restriction_path = Path(access_state) / "source-restriction.json"
            if restriction_path.exists():
                run["status"] = "stopped_on_source_restriction"
                run["source_restriction"] = json.loads(restriction_path.read_text(encoding="utf-8"))
                log("source_restriction_preserved", path=str(restriction_path))
                break
            pending = [q for q in queries if query_id(q) not in done]
            size = 1 if attempts.get(query_id(pending[0]), 0) else batch_size
            entries = launch_batch(
                pending[:size],
                output,
                project_root=project_root,
                sdk_wheel=sdk_wheel,
                access_state=access_state,
                timeout=timeout,
                delay=delay,
                budget=budget,
                log=log,
            )
            run["queries"].extend(entries)
            for entry in entries:
                key = entry["query_id"]
                attempts[key] = attempts.get(key, 0) + 1
                if entry["status"] == "complete":
                    done.add(key)
            save_json(path, run)
            failure = next((e for e in entries if e["status"] != "complete"), None)
            if failure and failure.get("error_code") == "10001011":
                restriction = {
                    "source": "baostock",
                    "recorded_at_utc": utc_now(),
                    "first_observed_at_utc": failure["observed_at_utc"],
                    "error_code": failure["error_code"],
                    "error_msg": failure.get("error_msg"),
                    "query_id": failure["query_id"],
                    "raw_locator": failure["raw_locator"],
                    "state": "restricted_until_service_restoration_is_established",
                    "automatic_retry": False,
                }
                save_json(restriction_path, restriction)
                run["source_restriction"] = restriction
            if not entries or (
                failure
                and (not failure.get("retryable", False) or attempts[failure["query_id"]] > retries)
            ):
                run["status"] = "stopped_on_error"
                run["stopped_query_id"] = failure["query_id"] if failure else query_id(pending[0])
                break
            if failure:
                time.sleep(2)
        else:
            run["status"] = "complete" if set(ids) <= done else "incomplete"
        run["finished_at_utc"] = utc_now()
        save_json(path, run)
    return run
