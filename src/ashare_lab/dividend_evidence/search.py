"""Audit the frozen 95-symbol CNINFO search batch entirely from retained bytes."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .common import (
    DividendEvidenceError,
    EvidenceReader,
    canonical,
    day,
    failures,
    instant,
    integer,
    require,
    strict_json,
    text,
)


SEARCH_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
SHANGHAI = ZoneInfo("Asia/Shanghai")
PLAN_SCHEMA = "cninfo-dividend-search-plan-v1"
RUN_SCHEMA = "cninfo-dividend-search-run-v1"


def _mapping(value, label):
    require(isinstance(value, dict), f"{label}: expected object")
    return value


def _symbols(value, label):
    require(
        isinstance(value, list) and all(isinstance(s, str) for s in value),
        f"{label}: invalid symbols",
    )
    require(value == sorted(set(value)), f"{label}: duplicate/unsorted symbols")
    require(
        all(re.fullmatch(r"[0-9]{6}\.(SH|SZ)", s) for s in value), f"{label}: invalid security code"
    )
    return value


def _scope(reader, plan):
    require(
        plan.get("schema_version") == PLAN_SCHEMA and plan.get("research_eligible") is False,
        "invalid dividend search plan",
    )
    require(not failures(plan), "search plan contains a retained failure")
    fixed = {
        "year": 2023,
        "keyword": "权益分派",
        "category": "",
        "page_size": 30,
        "max_pages_per_symbol": 2,
        "max_requests": 190,
    }
    for key, expected in fixed.items():
        require(
            type(plan.get(key)) is type(expected) and plan[key] == expected,
            f"plan {key} differs from the authorized batch",
        )
    symbols = _symbols(plan.get("symbols"), "plan")
    require(
        len(symbols) == 95 and all(s.endswith(".SZ") for s in symbols), "expected 95 SZ symbols"
    )
    previous = _mapping(reader.json(plan["prior_readiness"]), "prior readiness")
    require(
        previous.get("schema_version") == "retained-dividend-readiness-v1"
        and previous.get("research_eligible") is False
        and previous.get("formal_action_coverage_certified") is False,
        "prior readiness identity differs",
    )
    groups = {}
    for key, expected_size in (
        ("original_plan_scope", 336),
        ("conditional_union_diagnostic_only", 323),
    ):
        group = _mapping(previous.get(key), key)
        require(integer(group.get("symbols"), key) == expected_size, "prior scope size differs")
        lists = [
            _symbols(group.get(name), name)
            for name in (
                "missing_query_symbols",
                "successful_empty_query_symbols",
                "successful_nonempty_query_symbols",
            )
        ]
        combined = [s for rows in lists for s in rows]
        require(
            len(combined) == len(set(combined)) == expected_size, "prior query groups overlap/gap"
        )
        groups[key] = set(combined)
    require(
        groups["conditional_union_diagnostic_only"] <= groups["original_plan_scope"],
        "conditional scope outside original scope",
    )
    require(
        symbols == previous["conditional_union_diagnostic_only"]["missing_query_symbols"]
        and set(symbols) <= set(previous["original_plan_scope"]["missing_query_symbols"]),
        "plan differs from retained 95-query gap list",
    )
    for key in (
        "missing_query_symbols",
        "successful_empty_query_symbols",
        "successful_nonempty_query_symbols",
    ):
        require(
            set(previous["conditional_union_diagnostic_only"][key])
            <= set(previous["original_plan_scope"][key]),
            "conditional query status changes original status",
        )
    routing = _mapping(reader.json(plan["routing_map"]), "routing map")
    require(isinstance(routing.get("stockList"), list), "routing stockList absent")
    routes = {}
    for symbol in symbols:
        found = [
            r for r in routing["stockList"] if isinstance(r, dict) and r.get("code") == symbol[:6]
        ]
        require(len(found) == 1, f"missing/ambiguous route: {symbol}")
        org_id = text(found[0].get("orgId"), "route orgId")
        require(re.fullmatch(r"[A-Za-z0-9]+", org_id), "invalid route orgId")
        routes[symbol] = org_id
    reader.reference(plan["script"])
    return symbols, routes


def _payload(symbol, org_id, page):
    return {
        "pageNum": str(page),
        "pageSize": "30",
        "column": "szse",
        "tabName": "fulltext",
        "plate": "",
        "stock": symbol[:6] + "," + org_id,
        "searchkey": "权益分派",
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": "2023-01-01~2023-12-31",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "false",
    }


def _record(row, symbol, org_id):
    _mapping(row, "announcement")
    require(
        row.get("secCode") == symbol[:6] and row.get("orgId") == org_id,
        "announcement security/orgId differs from request",
    )
    identity = text(row.get("announcementId"), "announcementId")
    require(re.fullmatch(r"[0-9]+", identity), "invalid announcementId")
    text(row.get("announcementTitle"), "announcement title")
    text(row.get("secName"), "announcement security name")
    stamp = integer(row.get("announcementTime"), "announcementTime")
    try:
        timestamp = datetime.fromtimestamp(stamp / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise DividendEvidenceError("invalid announcement timestamp") from exc
    local = timestamp.astimezone(SHANGHAI)
    require(local.year == 2023, "announcement outside requested Shanghai year")
    url = row.get("adjunctUrl")
    require(isinstance(url, str), "missing attachment URL")
    match = re.fullmatch(
        r"finalpage/(2023-[0-9]{2}-[0-9]{2})/" + re.escape(identity) + r"\.[Pp][Dd][Ff]", url
    )
    require(match is not None, "attachment path/announcementId differs")
    attachment_day = day(match[1], "attachment date")
    require(
        isinstance(row.get("adjunctType"), str) and row["adjunctType"].upper() == "PDF",
        "non-PDF announcement attachment",
    )
    if "adjunctSize" in row:
        integer(row["adjunctSize"], "adjunctSize")
    return {
        "source_timestamp_utc": timestamp.isoformat(),
        "source_date_shanghai": local.date().isoformat(),
        "source_timestamp_precision": "date_label"
        if local.time().isoformat() == "00:00:00"
        else "timestamp_label_unverified",
        "attachment_date_differs": attachment_day != local.date(),
    }


def _page(reader, raw_path, meta_path, receipt, symbol, org_id, number, started, finished):
    if receipt is not None:
        require(
            reader.path(receipt["raw"]["path"]) == raw_path
            and reader.path(receipt["metadata"]["path"]) == meta_path,
            "receipt points outside its canonical query/page files",
        )
        meta = _mapping(reader.json(receipt["metadata"]), "metadata")
    else:
        meta = _mapping(reader.json(meta_path), "metadata")
    require(
        meta.get("url") == SEARCH_URL and meta.get("method") == "POST", "query URL/method differs"
    )
    require(
        canonical(meta.get("payload")) == canonical(_payload(symbol, org_id, number)),
        "query payload differs from fixed plan/route/page",
    )
    require(integer(meta.get("retries"), "capture retries") == 0, "capture was retried")
    begin, end = (
        instant(meta.get("started_at_utc"), "capture start"),
        instant(meta.get("completed_at_utc"), "capture end"),
    )
    require(
        started <= begin <= end and (finished is None or end <= finished),
        "capture/run times disagree",
    )
    require(
        type(meta.get("complete")) is bool and type(meta.get("transfer_complete")) is bool,
        "invalid completion flags",
    )
    status = meta.get("status")
    if status is not None:
        require(100 <= integer(status, "HTTP status") <= 599, "invalid HTTP status")
    problem = failures(meta)
    successful = meta["complete"] and meta["transfer_complete"] and status == 200 and not problem
    require(
        not meta["complete"] or successful, "capture completion contradicts failure/HTTP/transfer"
    )
    raw = None
    if raw_path.exists():
        raw = reader.reference(receipt["raw"]) if receipt is not None else reader.read(raw_path)[0]
        require(set(("path", "bytes", "sha256")) <= set(meta), "capture lacks raw binding")
        bound = {key: meta[key] for key in ("path", "bytes", "sha256")}
        require(reader.path(bound["path"]) == raw_path, "metadata points to another raw capture")
        require(reader.reference(bound) == raw, "metadata/raw binding differs")
    else:
        require(
            receipt is None
            and not successful
            and not any(key in meta for key in ("path", "bytes", "sha256")),
            "missing claimed raw capture",
        )
    result = {
        "page": number,
        "binding": "run_receipt" if receipt else "unindexed_capture",
        "metadata": reader.references[meta_path.relative_to(reader.root).as_posix()],
        "raw": reader.references.get(raw_path.relative_to(reader.root).as_posix()),
        "started_at_utc": begin.isoformat(),
        "completed_at_utc": end.isoformat(),
        "successful": successful,
        "failure": problem or None,
    }
    if not successful:
        result["failure"] = problem or {"incomplete": "HTTP or transfer did not complete"}
        return result, None
    require(raw is not None and len(raw) <= 2_000_000, "response missing or oversized")
    require(meta.get("final_url") == SEARCH_URL, "redirect or different response origin")
    require(
        isinstance(meta.get("content_type"), str)
        and meta["content_type"].split(";", 1)[0].strip().lower() == "application/json",
        "non-JSON response content type",
    )
    require("content_length" in meta, "Content-Length observation missing")
    length = meta["content_length"]
    if length is not None:
        require(
            isinstance(length, str) and re.fullmatch(r"[0-9]+", length), "invalid Content-Length"
        )
        require(int(length) == len(raw), "Content-Length differs from captured bytes")
    data = _mapping(strict_json(raw), "query response")
    require(not failures(data), "query response contains a failure")
    total = integer(data.get("totalAnnouncement"), "totalAnnouncement")
    require(type(data.get("hasMore")) is bool, "hasMore is not boolean")
    if "totalRecordNum" in data:
        require(
            integer(data["totalRecordNum"], "totalRecordNum") == total, "response totals differ"
        )
    if "totalpages" in data:
        integer(data["totalpages"], "totalpages")  # Observed zero even for nonempty first pages.
    rows = data.get("announcements")
    if rows is None:
        require("announcements" in data and total == 0, "nonempty response has null/missing rows")
        rows = []
    require(isinstance(rows, list) and len(rows) <= 30, "invalid/oversized announcement page")
    for row in rows:
        _record(row, symbol, org_id)
    result.update(
        total=total,
        has_more=data["hasMore"],
        rows=len(rows),
        reported_totalpages=data.get("totalpages"),
    )
    return result, rows


def _query(reader, folder, symbol, org_id, entry, started, finished):
    result = {
        "symbol": symbol,
        "org_id": org_id,
        "status": "not_requested",
        "query_complete": False,
        "research_eligible": False,
        "available_time": None,
        "expected_total": None,
        "pages": [],
        "announcements": [],
        "issues": [],
    }
    receipts = {}
    if entry is not None:
        require(not failures(entry), "query entry contains a retained failure")
        require(
            type(entry.get("query_complete")) is bool and isinstance(entry.get("rows"), list),
            "invalid run query summary",
        )
        source = entry.get("receipts")
        require(isinstance(source, list) and len(source) <= 2, "invalid query receipts")
        for index, receipt in enumerate(source, 1):
            _mapping(receipt, "receipt")
            require(
                integer(receipt.get("page"), "receipt page", 1) == index,
                "non-contiguous receipt pages",
            )
            receipts[index] = receipt
    rows, ids = [], set()
    last_more, last_end, seen_gap = None, None, False
    for number in (1, 2):
        raw_path, meta_path = (
            folder / f"{symbol[:6]}-page{number}.json",
            folder / f"{symbol[:6]}-page{number}.metadata.json",
        )
        if not (raw_path.exists() or meta_path.exists() or number in receipts):
            seen_gap = True
            continue
        try:
            require(not seen_gap and last_more is not False, "gap/page after query already closed")
            if not meta_path.exists():
                reader.read(raw_path)
                result["pages"].append(
                    {
                        "page": number,
                        "binding": "unindexed_capture",
                        "successful": False,
                        "failure": {"incomplete": "metadata absent"},
                    }
                )
                result["status"] = "incomplete"
                break
            page_result, page_rows = _page(
                reader,
                raw_path,
                meta_path,
                receipts.get(number),
                symbol,
                org_id,
                number,
                started,
                finished,
            )
            result["pages"].append(page_result)
            begin = instant(page_result["started_at_utc"], "page start")
            end = instant(page_result["completed_at_utc"], "page end")
            require(last_end is None or last_end <= begin, "pages overlap or run backwards")
            last_end = end
            if not page_result["successful"]:
                result["status"] = "failed"
                break
            if result["expected_total"] is None:
                result["expected_total"] = page_result["total"]
            require(result["expected_total"] == page_result["total"], "pagination total changed")
            for index, row in enumerate(page_rows):
                require(row["announcementId"] not in ids, "duplicate announcement across pages")
                ids.add(row["announcementId"])
                rows.append(row)
                result["announcements"].append(
                    {
                        "symbol": symbol,
                        "page": number,
                        "row_index": index,
                        "record": row,
                        "raw": page_result["raw"],
                        "metadata": page_result["metadata"],
                        **_record(row, symbol, org_id),
                        "research_eligible": False,
                        "available_time": None,
                    }
                )
            last_more = page_result["has_more"]
            require(len(rows) <= result["expected_total"], "rows exceed announced total")
            require(
                last_more == (len(rows) < result["expected_total"]),
                "hasMore/total/row count disagree",
            )
            require(not last_more or len(page_rows) > 0, "empty page claims more rows")
            result["status"] = "incomplete"
        except (DividendEvidenceError, KeyError, TypeError) as exc:
            result["status"] = "invalid"
            result["issues"].append(str(exc))
            break
    if entry is not None and result["status"] != "invalid":
        try:
            expected = entry.get("expected_total")
            require(
                expected is None or type(expected) is int and expected >= 0,
                "invalid run expected_total",
            )
            require(expected == result["expected_total"], "run expected_total differs from raw")
            require(
                canonical(entry["rows"]) == canonical(rows),
                "run rows differ from exact raw rows/order",
            )
            closed = last_more is False and result["status"] != "failed"
            closed = closed and len(result["pages"]) == len(receipts) and bool(receipts)
            require(
                entry["query_complete"] is closed, "run query_complete contradicts raw pagination"
            )
            if closed:
                result["status"] = "complete_nonempty" if rows else "complete_zero"
                result["query_complete"] = True
        except DividendEvidenceError as exc:
            result["status"] = "invalid"
            result["issues"].append(str(exc))
    if result["status"] == "incomplete" and entry is None and result["pages"]:
        result["issues"].append("capture absent from run receipts; retained for diagnosis only")
    return result


def audit_search_run(plan_path, manifest_path, evidence_root):
    """Rebuild selected-query coverage; never certify annual actions or historical availability.

    Invalid plan/manifest structure raises DividendEvidenceError. Per-query failures and
    corrupt captures remain in the returned audit with non-passing status; absent queries
    are not zero results. Unindexed attempts are diagnostic only, never promoted.
    """
    reader = EvidenceReader(evidence_root)
    plan_path, manifest_path = reader.path(plan_path), reader.path(manifest_path)
    plan = _mapping(reader.json(plan_path), "plan")
    run = _mapping(reader.json(manifest_path), "run")
    symbols, routes = _scope(reader, plan)
    require(
        run.get("schema_version") == RUN_SCHEMA and run.get("research_eligible") is False,
        "invalid search run identity",
    )
    require(reader.path(run["plan"]["path"]) == plan_path, "run refers to another plan")
    reader.reference(run["plan"])
    require(plan_path.parent == manifest_path.parent, "plan/run directories differ")
    status = run.get("status")
    require(
        status in {"running", "failed", "partial_selected_queries", "complete_selected_queries"},
        "unknown search run status",
    )
    request_count = integer(run.get("requests"), "run requests")
    require(
        request_count <= 190 and integer(run.get("retries"), "run retries") == 0,
        "request/retry budget differs",
    )
    started = instant(run.get("started_at_utc"), "run start")
    finished = instant(run["finished_at_utc"], "run end") if run.get("finished_at_utc") else None
    require(
        finished is None and status == "running" or finished is not None and started <= finished,
        "run completion time absent/reversed",
    )
    entries = run.get("entries")
    require(isinstance(entries, list), "run entries not a list")
    names = [entry.get("symbol") for entry in entries if isinstance(entry, dict)]
    require(
        len(names) == len(entries) and names == symbols[: len(names)],
        "run entries differ from ordered plan prefix",
    )
    by_symbol = {entry["symbol"]: entry for entry in entries}
    queries = []
    for symbol in symbols:
        try:
            queries.append(
                _query(
                    reader,
                    manifest_path.parent,
                    symbol,
                    routes[symbol],
                    by_symbol.get(symbol),
                    started,
                    finished,
                )
            )
        except (DividendEvidenceError, KeyError, TypeError) as exc:
            queries.append(
                {
                    "symbol": symbol,
                    "org_id": routes[symbol],
                    "status": "invalid",
                    "query_complete": False,
                    "research_eligible": False,
                    "available_time": None,
                    "expected_total": None,
                    "pages": [],
                    "announcements": [],
                    "issues": [str(exc)],
                }
            )
    issues = []
    previous_end = None
    stopped_on_failure = False
    for query in queries:
        for page in query["pages"]:
            if stopped_on_failure:
                issues.append("capture continued after a failed request")
            if "started_at_utc" in page:
                begin = instant(page["started_at_utc"], "capture start")
                end = instant(page["completed_at_utc"], "capture end")
                if previous_end is not None and begin - previous_end < timedelta(seconds=1):
                    issues.append(
                        "captures overlap, run backwards, or violate the one-second interval"
                    )
                previous_end = end
            stopped_on_failure = stopped_on_failure or not page["successful"]
    attempted = set()
    for path in manifest_path.parent.glob("*-page*.*"):
        match = re.fullmatch(r"([0-9]{6})-page([0-9]+)\.(?:metadata\.)?json", path.name)
        if not match or match[1] + ".SZ" not in routes or match[2] not in {"1", "2"}:
            issues.append(f"unexpected query capture file: {path.name}")
        else:
            attempted.add((match[1] + ".SZ", int(match[2])))
    missing_attempt = request_count - len(attempted)
    if missing_attempt == 1 and status in {"running", "failed"}:
        pending = next(
            (q for q in queries if not q["query_complete"] and len(q["pages"]) < 2), None
        )
        if pending is not None and pending["status"] != "invalid":
            pending["status"] = "failed" if status == "failed" else "incomplete"
            pending["issues"].append("counted attempt has no retained receipt/response")
        else:
            issues.append("counted attempt cannot be assigned to the next pending query")
    elif missing_attempt != 0:
        issues.append("run requests differ from retained capture attempts")
    root_failures = failures(run)
    if root_failures and status != "failed":
        issues.append("run completion status contradicts retained failure")
    if status == "failed" and not root_failures:
        issues.append("failed run lacks a retained failure reason")
    all_complete = all(q["query_complete"] for q in queries)
    if status == "complete_selected_queries" and not all_complete:
        issues.append("run claims complete but selected queries are not verified complete")
    if status == "partial_selected_queries" and all_complete:
        issues.append("partial run status contradicts verified complete queries")
    counts = Counter(q["status"] for q in queries)
    invalid = bool(issues or counts["invalid"])
    accepted = (
        all_complete and status == "complete_selected_queries" and not invalid and not root_failures
    )
    reader.unchanged()
    return {
        "schema_version": "cninfo-dividend-search-audit-v1",
        "status": "invalid_evidence"
        if invalid
        else "complete_selected_queries"
        if accepted
        else "partial_selected_queries",
        "selected_queries_complete": accepted,
        "research_eligible": False,
        "available_time": None,
        "formal_action_coverage_certified": False,
        "source_run_status": status,
        "source_run_failure": root_failures or None,
        "network_requests": 0,
        "counts": {
            "planned_queries": len(symbols),
            "manifest_requests": request_count,
            "retained_capture_attempts": len(attempted),
            "complete_queries": counts["complete_zero"] + counts["complete_nonempty"],
            "nonempty_queries": counts["complete_nonempty"],
            "zero_queries": counts["complete_zero"],
            "failed_queries": counts["failed"],
            "incomplete_queries": counts["incomplete"],
            "not_requested_queries": counts["not_requested"],
            "invalid_queries": counts["invalid"],
            "validated_rows": sum(len(q["announcements"]) for q in queries),
        },
        "issues": issues,
        "queries": queries,
        "inputs": list(reader.references.values()),
        "module_file": str(Path(__file__).resolve()),
        "limits": [
            "Coverage is only the exact 95 selected 2023 announcement-date queries, not annual corporate actions.",
            "Zero matches never establish no dividends or no corporate actions; titles do not establish implementation.",
            "Current orgId mapping is routing only. Original 336-symbol and BaoStock failure records remain unchanged.",
            "Date/timestamp labels and ingestion times do not establish historical available_time.",
            "totalpages is retained but not a pagination authority; existing nonempty captures report zero.",
            "Unindexed captures are diagnostic only. Hashes bind retained files, not provider signatures.",
        ],
    }
