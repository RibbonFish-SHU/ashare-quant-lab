"""Bounded development plans; no model evaluation or holdout market data."""

from datetime import date, timedelta
import json
from pathlib import Path

from .pipeline import load_run
from .queries import BAR_FIELDS, query_id, validate_query
from .raw import cached_records, save_json


def plan(queries, purpose):
    unique = {query_id(q): validate_query(q) for q in queries}
    return {
        "schema_version": "source-plan-v1",
        "source": "baostock",
        "sdk": "0.9.4",
        "purpose": purpose,
        "research_eligible": False,
        "queries": list(unique.values()),
    }


def reuse_plan(roots):
    return plan(cached_records(roots).values(), "reuse existing exact requests without network")


def membership_2023():
    dates = {
        "2023-01-03",
        "2023-06-09",
        "2023-06-12",
        "2023-12-08",
        "2023-12-11",
        "2022-12-09",
        "2022-12-12",
    }
    d = date(2023, 1, 2)
    while d.year == 2023:
        dates.add(d.isoformat())
        d += timedelta(days=7)
    return plan(
        [
            {
                "api": "query_trade_dates",
                "parameters": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
            },
            {
                "api": "query_trade_dates",
                "parameters": {"start_date": "2022-12-09", "end_date": "2022-12-12"},
            },
            *[{"api": "query_hs300_stocks", "parameters": {"date": d}} for d in sorted(dates)],
        ],
        "2023 calendar and weekly membership observations plus official adjustment boundaries",
    )


def securities_2023(runs, events_path=None, *, original_plans=()):
    codes = set()
    for path in runs:
        records, complete, _ = load_run(path, original_plans=original_plans)
        if not complete:
            raise ValueError("finish membership collection before planning its union")
        for record in records:
            if record["api"] == "query_hs300_stocks" and record["parameters"]["date"].startswith(
                "2023-"
            ):
                position = record["fields"].index("code")
                codes.update(row[position] for row in record["rows"])
    if events_path:
        for event in json.loads(Path(events_path).read_text(encoding="utf-8"))["events"]:
            for code in event["added"] + event["removed"]:
                number, market = code.split(".")
                codes.add(market.lower() + "." + number)
    if not codes:
        raise ValueError("no observed or officially announced members to collect")
    queries = []
    for code in sorted(codes):
        interval = {"code": code, "start_date": "2023-01-01", "end_date": "2023-12-31"}
        queries.extend(
            [
                {"api": "query_stock_basic", "parameters": {"code": code}},
                {
                    "api": "query_history_k_data_plus",
                    "parameters": {
                        **interval,
                        "fields": BAR_FIELDS,
                        "frequency": "d",
                        "adjustflag": "3",
                    },
                },
                {
                    "api": "query_dividend_data",
                    "parameters": {"code": code, "year": "2023", "yearType": "operate"},
                },
                {"api": "query_adjust_factor", "parameters": interval},
            ]
        )
    result = plan(
        queries,
        "2023 observed/announced member union; candidate coverage, not a certified historical universe",
    )
    result["codes"] = sorted(codes)
    return result


def write_plan(path, value):
    path = Path(path)
    if path.exists():
        raise FileExistsError("plan is immutable; choose a new file")
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, value)
