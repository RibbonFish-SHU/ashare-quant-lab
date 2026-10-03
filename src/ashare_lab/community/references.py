"""Read already reviewed snapshots without changing their raw-source accounting."""

from datetime import date

import pyarrow.parquet as pq

from ashare_lab.fallback.evidence import load_reference_evidence
from ashare_lab.fallback.pipeline import convert
from .source import read_json, require, verified_file


def unique_rows(rows, keys, label):
    result = {}
    for row in rows:
        key = tuple(row[k] for k in keys)
        require(key not in result, f"duplicate/conflicting {label} reference")
        result[key] = row
    return result


def load_references(context, root):
    references = context["references"]
    prices, securities, calendars = [], [], []
    for snapshot in context["config"]["snapshots"]:
        manifest = read_json(verified_file(root, snapshot["manifest"], references))
        require(
            manifest["research_eligible"] is False, "expected a non-research reference candidate"
        )
        for kind, locator in snapshot["tables"].items():
            path = verified_file(root, locator, references)
            expected = (
                manifest["tables"][kind]["sha256"]
                if "tables" in manifest
                else manifest["files"][kind + ".parquet"]
            )
            require(expected == locator["sha256"], "reference manifest/table binding differs")
            rows = pq.read_table(path).to_pylist()
            for row in rows:
                row["table_path"], row["table_sha256"] = str(path), locator["sha256"]
            if kind == "bars":
                for row in rows:
                    require(row["event_date"].year == 2023, "reference bars outside 2023")
                    require(
                        row["record_status"] not in {"invalid", "outside_requested_interval"},
                        "invalid price reference",
                    )
                    require(row.get("source_adjustflag", "3") == "3", "adjusted reference price")
                    row["provider"] = row.get("provider", row.get("source"))
                    require(
                        row["provider"] in {"baostock", "eastmoney"},
                        "unexpected or same-source primary reference",
                    )
                    if "requested_start" in row:
                        require(
                            row["requested_start"] <= row["event_date"] <= row["requested_end"]
                            and row["within_requested_interval"] is True,
                            "reference outside its requested interval",
                        )
                prices.extend(rows)
            elif kind == "securities":
                securities.extend(rows)
            elif kind == "calendar":
                calendars.extend(r for r in rows if r["event_date"].year == 2023)
            else:
                raise ValueError("unsupported reference table")
    primary = unique_rows(prices, ("symbol", "event_date"), "primary quote")
    listings = {k[0]: v for k, v in unique_rows(securities, ("symbol",), "listing").items()}
    calendar = {k[0]: v for k, v in unique_rows(calendars, ("event_date",), "calendar").items()}
    require(len({r["source"] for r in calendars}) == 1, "calendar sources are ambiguous")
    evidence, extra = None, []
    if context["config"].get("include_verified_szse_tencent"):
        evidence = load_reference_evidence(root)
        references.extend(evidence["references"])
        for row in evidence["listing"]:
            old = listings.get(row["symbol"])
            ipo = date.fromisoformat(row["listed_date"])
            if old is not None and old.get("ipo_date") is not None:
                require(old["ipo_date"] == ipo, "IPO reference dates conflict")
            else:
                listings[row["symbol"]] = {**row, "ipo_date": ipo}
        for capture in evidence["price_captures"]:
            converted, issues = convert(capture)
            require(not issues, "invalid verified price capture")
            extra.extend(r for r in converted if r["within_requested_interval"])
    unique_rows(extra, ("provider", "symbol", "event_date"), "supplementary quote")
    conditional = context["documents"].get("conditional_membership")
    if conditional is not None:
        require(
            conditional["research_eligible"] is False and bool(conditional["assumptions"]),
            "conditional membership requires explicit assumptions",
        )
        for locator in conditional["inputs"]:
            verified_file(root, locator, references)
    return {
        "primary": primary,
        "extra": extra,
        "listings": listings,
        "calendar": calendar,
        "evidence": evidence,
        "conditional_membership": conditional,
    }


def locator(row):
    return {
        key: row.get(key)
        for key in (
            "provider",
            "source",
            "query_id",
            "raw_path",
            "raw_sha256",
            "row_number",
            "table_path",
            "table_sha256",
            "observed_at_utc",
            "record_status",
            "trade_observation",
            "requested_start",
            "requested_end",
            "source_trading_active",
        )
    }
