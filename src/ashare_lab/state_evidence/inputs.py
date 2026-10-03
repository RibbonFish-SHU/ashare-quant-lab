"""Pinned inputs and retained daily observations, never upgraded to certified state."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pyarrow.parquet as pq

from ashare_lab.community.source import planned_symbols
from .common import checked_file, day, digest, file_ref, instant, read_json, require


def load_inputs(description, root):
    config = read_json(description)
    require(
        config.get("schema_version") == "state-evidence-input-v1"
        and config.get("research_eligible") is False,
        "invalid state input config",
    )
    start, end = day(config["start"]), day(config["end"])
    require(start.year == end.year == 2023 and start <= end, "only 2023 state scope is authorized")
    require(
        config["availability_policy"] == "exclude_unknown", "real evidence availability unknown"
    )
    paths, references = {}, [file_ref(description)]
    for reference in config["files"]:
        key = reference["path"].replace("\\", "/")
        require(key not in paths, "duplicate selected input path")
        paths[key] = checked_file(root, reference)
        references.append(file_ref(paths[key]))
    plan = read_json(paths[config["original_plan"]])
    symbols = planned_symbols(plan, start, end)
    return {
        "config": config,
        "paths": paths,
        "references": references,
        "start": start,
        "end": end,
        "symbols": symbols,
    }


def calendar_rows(rows, start, end):
    rows = [r for r in rows if start <= r["event_date"] <= end]
    require(bool(rows), "empty selected calendar")
    identities = {(r["source"], r["sdk"], r["exchange_scope"]) for r in rows}
    require(
        identities == {("baostock", "0.9.4", "SSE_SZSE_provider_joint")},
        "mixed or unsupported calendar source",
    )
    result = {}
    for row in rows:
        d = row["event_date"]
        require(d not in result and type(row["is_open"]) is bool, "duplicate/invalid calendar day")
        result[d] = row
    expected = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    require(set(result) == expected, "calendar has missing civil days")
    return result


def _candidate(path, manifest, kind):
    m = read_json(manifest)
    require(
        m.get("research_eligible") is False and m.get("data_kind") == "real_candidate",
        "retained input must remain a candidate",
    )
    if kind == "baostock":
        require(
            m["schema_version"] == "baostock-candidate-v2"
            and m["status"] == "complete_candidates_research_blocked",
            "Bao candidate status",
        )
        expected = m["tables"][path.stem]["sha256"]
    elif kind == "community":
        require(
            m["schema_version"] == "community-quote-candidate-v1"
            and m["status"] == "built_candidate",
            "community candidate status",
        )
        expected = m["tables"][path.stem]["sha256"]
    else:
        require(m["schema_version"] == "public-bars-candidate-v1", "fallback candidate version")
        expected = m["files"][path.name]
    require(digest(path) == expected, "candidate/manifest table digest differs")


def _raw_success(raw):
    require(
        raw.get("status") == "complete"
        and raw.get("error_code") == "0"
        and raw.get("source") == "baostock"
        and raw.get("sdk") == "0.9.4"
        and not raw.get("error")
        and not raw.get("exception")
        and raw.get("worker_exit_code", 0) == 0,
        "retained Bao raw source/status differs",
    )


def verify_daily_raw(path, rows):
    raw = read_json(path)
    _raw_success(raw)
    require(
        raw["api"] == "query_history_k_data_plus"
        and raw["parameters"]["frequency"] == "d"
        and raw["parameters"]["adjustflag"] == "3",
        "wrong daily raw query",
    )
    raw_sha256 = digest(path)
    selected = [r for r in rows if r["raw_sha256"] == raw_sha256]
    require(
        bool(selected) and len(selected) == len(raw["rows"]), "daily raw/candidate coverage differs"
    )
    for row in selected:
        index = row["row_number"]
        require(0 <= index < len(raw["rows"]), "daily raw row out of bounds")
        values = dict(zip(raw["fields"], raw["rows"][index], strict=True))
        require(
            values["tradestatus"] in {"0", "1"} and values["isST"] in {"0", "1"},
            "unknown daily raw status spelling",
        )
        require(values["code"] == raw["parameters"]["code"], "daily raw stock differs")
        require(
            row["symbol"] == values["code"][3:] + "." + values["code"][:2].upper()
            and row["event_date"] == day(values["date"])
            and row["source_trading_active"] is (values["tradestatus"] == "1")
            and row["source_is_st"] is (values["isST"] == "1")
            and row["volume_shares"] == int(values["volume"])
            and row["query_id"] == raw["query_id"],
            "daily raw fields differ from candidate",
        )
    return len(selected)


def load_references(context):
    paths, config = context["paths"], context["config"]
    primary, community, bao_rows = {}, {}, []
    counts = {}
    for spec in config["candidates"]:
        path, kind = paths[spec["table"]], spec["kind"]
        require(
            kind in {"baostock", "eastmoney", "community"}, "unsupported retained candidate kind"
        )
        _candidate(path, paths[spec["manifest"]], kind)
        common = [
            "symbol",
            "event_date",
            "record_status",
            "observed_at_utc",
            "available_time",
            "source_trading_active",
        ]
        if kind == "community":
            columns = common + [
                "raw_close",
                "archive_sha256",
                "source_repository",
                "source_version",
            ]
        elif kind == "baostock":
            columns = common + [
                "source",
                "sdk",
                "source_is_st",
                "volume_shares",
                "raw_path",
                "raw_sha256",
                "row_number",
                "raw_values_json",
                "query_id",
            ]
        else:
            columns = common + [
                "provider",
                "source_is_st",
                "volume_raw",
                "volume_source_unit",
                "raw_path",
                "raw_sha256",
                "row_number",
                "raw_values_json",
                "query_id",
            ]
        rows = pq.read_table(path, columns=columns).to_pylist()
        counts[spec["table"]] = len(rows)
        table_sha256 = digest(path)
        for index, row in enumerate(rows):
            require(
                row["symbol"] in context["symbols"]
                and context["start"] <= row["event_date"] <= context["end"],
                "retained quote outside authorized symbol/date scope",
            )
            require(row["available_time"] is None, "unexpected certified candidate availability")
            row["table_locator"] = {"path": str(path), "sha256": table_sha256, "row": index}
            key = row["symbol"], row["event_date"]
            target = community if kind == "community" else primary
            require(key not in target, "duplicate/overlapping candidate observation")
            target[key] = row
            if kind == "baostock":
                require(
                    row["source"] == "baostock" and row["sdk"] == "0.9.4", "daily source differs"
                )
                bao_rows.append(row)
            elif kind == "eastmoney":
                require(
                    row["provider"] == "eastmoney"
                    and row["source_trading_active"] is None
                    and row["source_is_st"] is None,
                    "fallback cannot certify state",
                )
            else:
                require(
                    row["source_repository"] == "chenditc/investment_data"
                    and row["source_version"] == "2023-12-31"
                    and row["source_trading_active"] is None,
                    "community source/state differs",
                )
    cal_spec = config["calendar"]
    cal_path = paths[cal_spec["table"]]
    _candidate(cal_path, paths[cal_spec["manifest"]], "baostock")
    calendar = calendar_rows(pq.read_table(cal_path).to_pylist(), context["start"], context["end"])
    cal_raw = read_json(paths[cal_spec["raw"]])
    _raw_success(cal_raw)
    require(
        cal_raw["api"] == "query_trade_dates"
        and cal_raw["fields"] == ["calendar_date", "is_trading_day"],
        "calendar query differs",
    )
    calendar_raw_sha256 = digest(paths[cal_spec["raw"]])
    for d, row in calendar.items():
        require(
            row["raw_sha256"] == calendar_raw_sha256
            and 0 <= row["row_number"] < len(cal_raw["rows"])
            and cal_raw["rows"][row["row_number"]]
            == [d.isoformat(), "1" if row["is_open"] else "0"],
            "calendar raw/candidate differs",
        )
    raw_rows = sum(verify_daily_raw(paths[p], bao_rows) for p in config["daily_raw_checks"])
    missing_symbols = set(context["symbols"]) - {r["symbol"] for r in bao_rows}
    return {
        "primary": primary,
        "community": community,
        "calendar": calendar,
        "missing_bao_symbols": missing_symbols,
        "counts": counts,
        "daily_raw_checked": raw_rows,
    }


def rejected_state_sources(context):
    """Saved API failures and out-of-period rows are diagnostics, never trading events."""
    paths, spec = context["paths"], context["config"]["rejected_eastmoney"]
    manifest = read_json(paths[spec["manifest"]])
    results = []
    for record in manifest["records"]:
        key = record["path"].replace("\\", "/")
        raw_path = paths[key]
        require(
            record["sha256"] == digest(raw_path) and record["bytes"] == raw_path.stat().st_size,
            "rejected raw manifest binding differs",
        )
        parsed = urlparse(record["url"])
        require(
            parsed.hostname == "datacenter-web.eastmoney.com"
            and parsed.path == "/api/data/v1/get"
            and parse_qs(parsed.query)["reportName"] == ["RPT_CUSTOM_SUSPEND_DATA_INTERFACE"]
            and record["status"] == 200,
            "rejected response source differs",
        )
        data = read_json(raw_path)
        rows = (data.get("result") or {}).get("data") or []
        dates = [day(r["SUSPEND_START_DATE"][:10]) for r in rows]
        within = sum(context["start"] <= d <= context["end"] for d in dates)
        reason = (
            "empty_response_not_normal_state"
            if not rows
            else "requested_history_not_returned"
            if within == 0
            else "unadopted_partial_response"
        )
        results.append(
            {
                "path": key,
                "sha256": digest(raw_path),
                "requested_date": record["date"],
                "reason": reason,
                "rows": len(rows),
                "within_scope_rows": within,
                "earliest_event_date": min(dates) if dates else None,
                "latest_event_date": max(dates) if dates else None,
                "reported_total": (data.get("result") or {}).get("count"),
                "reported_pages": (data.get("result") or {}).get("pages"),
                "observed_at_utc": instant(record["captured_at_utc"]),
                "adopted_events": 0,
            }
        )
    for pair in spec["targeted"]:
        raw_path, meta = paths[pair["raw"]], read_json(paths[pair["metadata"]])
        require(
            meta["sha256"] == digest(raw_path) and meta["bytes"] == raw_path.stat().st_size,
            "targeted failure binding differs",
        )
        require(
            urlparse(meta["url"]).hostname == "datacenter-web.eastmoney.com"
            and meta["params"]["reportName"] == "RPT_CUSTOM_SUSPEND_DATA_INTERFACE",
            "targeted failure source differs",
        )
        result = read_json(raw_path)
        require(
            meta["source_result"] == result
            and result.get("success") is False
            and result.get("code") == 9501
            and result.get("result") is None,
            "selected parameter-failure response differs",
        )
        results.append(
            {
                "path": pair["raw"],
                "sha256": digest(raw_path),
                "reason": "parameter_failure_not_normal_state",
                "provider_result": result,
                "observed_at_utc": instant(meta["captured_at_utc"]),
                "adopted_events": 0,
            }
        )
    return results
