"""One bounded SDK connection. Parent enforces a wall timeout; no credentials."""

import argparse
import contextlib
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import sys
import time
from zoneinfo import ZoneInfo

from .queries import SDK_SHA256, SDK_VERSION, source_url, validate_query
from .raw import raw_table, save_json, utc_now


def failure(exc):
    transport = isinstance(exc, (ConnectionError, TimeoutError, socket.timeout))
    return {
        "status": "transport_error" if transport else "parse_or_local_error",
        "retryable": transport,
        "exception": f"{type(exc).__name__}: {exc}",
    }


def fetch(query, sdk, progress):
    """A failed pagination request must never be interpreted as clean EOF."""
    record = {
        **validate_query(query),
        "source": "baostock",
        "sdk": SDK_VERSION,
        "source_url": source_url(query["api"]),
        "started_at_utc": utc_now(),
        "observation_precision": "request_start_and_finish",
        "rows": [],
        "fields": [],
        "status": "running",
        "transport_integrity": "checked_every_send",
    }
    try:
        if query["api"] in {"query_st_stocks", "query_suspended_stocks"}:
            from baostock.security import sectorinfo

            method = getattr(sectorinfo, query["api"])
        else:
            method = getattr(sdk, query["api"])
        result = method(**query["parameters"])
        record.update(
            fields=result.fields, error_code=result.error_code, error_msg=result.error_msg
        )
        save_json(progress, record)
        while result.error_code == "0" and result.next():
            record["rows"].append(result.get_row_data())
            if len(record["rows"]) % 50 == 0:
                save_json(progress, record)
            if len(record["rows"]) > 20000:
                raise ValueError("query exceeds bounded pilot row limit")
        record.update(
            error_code=result.error_code,
            error_msg=result.error_msg,
            status="complete" if result.error_code == "0" else "provider_error",
        )
        if record["status"] == "complete":
            # SDK 0.9.4 otherwise returns False without changing error_code
            # when the next-page cursor is malformed.
            if (
                getattr(result, "data", None)
                and len(result.data) == int(result.per_page_count)
                and not str(result.cur_page_num).isdigit()
            ):
                raise ValueError("SDK stopped on an invalid pagination cursor")
            raw_table(record)
    except Exception as exc:
        record.update(failure(exc))
    record["observed_at_utc"] = utc_now()
    save_json(progress, record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--delay", type=float, required=True)
    parser.add_argument("--budget", type=int, required=True)
    args = parser.parse_args(argv)
    if hashlib.sha256(args.sdk_wheel.read_bytes()).hexdigest() != SDK_SHA256:
        raise ValueError("BaoStock official wheel hash mismatch")
    if args.delay < 0.25 or not 1 <= args.budget <= 10000:
        raise ValueError("invalid conservative source access limits")
    sys.path.insert(0, str(args.sdk_wheel.resolve()))
    socket.setdefaulttimeout(10)
    import baostock as bs
    from baostock.common import context
    import baostock.util.socketutil as sdk_socket

    original_send = sdk_socket.send_msg

    def checked_send(message):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        ledger = json.loads(args.ledger.read_text(encoding="utf-8")) if args.ledger.exists() else {}
        count = ledger.setdefault("wire_requests_by_china_date", {}).get(today, 0)
        if count >= args.budget:
            raise RuntimeError("local daily wire-request budget exhausted")
        time.sleep(max(0, args.delay - (time.time() - ledger.get("last_send_epoch", 0))))
        ledger["wire_requests_by_china_date"][today] = count + 1
        ledger["timezone"] = "Asia/Shanghai"
        ledger["last_send_epoch"] = time.time()
        save_json(args.ledger, ledger)
        response = original_send(message)
        if not response or not response.strip():
            raise ConnectionError("SDK returned no transport response; not a valid empty dataset")
        return response

    sdk_socket.send_msg = checked_send
    query = json.loads(args.request.read_text(encoding="utf-8"))
    record = {
        **validate_query(query),
        "rows": [],
        "fields": [],
        "status": "login_failed",
        "observed_at_utc": utc_now(),
        "source": "baostock",
        "sdk": SDK_VERSION,
    }
    save_json(args.progress, record)
    try:
        login = bs.login()
        if login.error_code != "0":
            record.update(error_code=login.error_code, error_msg=login.error_msg)
            save_json(args.progress, record)
            return 1
        record = fetch(query, bs, args.progress)
        return 0 if record["status"] == "complete" else 1
    except Exception as exc:
        record.update(failure(exc))
        save_json(args.progress, record)
        return 1
    finally:
        # Closing this worker's one socket also handles failed logins and avoids
        # sending another request after a restriction/error response.
        with contextlib.suppress(Exception):
            context.default_socket.close()
        sdk_socket.send_msg = original_send


if __name__ == "__main__":
    raise SystemExit(main())
