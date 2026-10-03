"""Lossless, typed candidates. This schema does not confer historical PIT eligibility."""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json

import pyarrow as pa

from .queries import API_KINDS, query_id, symbol

VERSION = "baostock-candidate-v1"
DECIMAL = pa.decimal128(32, 12)
COMMON = [
    ("query_id", pa.string()),
    ("raw_path", pa.string()),
    ("raw_sha256", pa.string()),
    ("raw_query_name", pa.string()),
    ("row_number", pa.int64()),
    ("observed_at_utc", pa.timestamp("us", tz="UTC")),
    ("observation_precision", pa.string()),
    ("source", pa.string()),
    ("sdk", pa.string()),
    ("raw_values_json", pa.string()),
    ("record_status", pa.string()),
    ("historical_publish_time", pa.timestamp("us", tz="UTC")),
    ("available_time", pa.timestamp("us", tz="UTC")),
    ("time_basis", pa.string()),
]
SNAPSHOT = [
    ("query_date", pa.date32()),
    ("source_update_date", pa.date32()),
    ("symbol", pa.string()),
    ("name_observed", pa.string()),
]
DIVIDEND_DATES = {
    "dividPreNoticeDate": "pre_notice_date",
    "dividAgmPumDate": "agm_date",
    "dividPlanAnnounceDate": "proposal_announcement_date",
    "dividPlanDate": "implementation_announcement_date",
    "dividRegistDate": "record_date",
    "dividOperateDate": "ex_date",
    "dividPayDate": "cash_payment_date",
    "dividStockMarketDate": "bonus_listing_date",
}
FIELDS = {
    "bars": [("symbol", pa.string()), ("event_date", pa.date32())]
    + [
        (name, DECIMAL)
        for name in ("open", "high", "low", "close", "preclose", "amount_cny", "turnover_fraction")
    ]
    + [
        ("volume_shares", pa.int64()),
        ("source_adjustflag", pa.string()),
        ("source_trading_active", pa.bool_()),
        ("source_is_st", pa.bool_()),
        ("trade_observation", pa.string()),
    ],
    "calendar": [
        ("event_date", pa.date32()),
        ("is_open", pa.bool_()),
        ("exchange_scope", pa.string()),
    ],
    "membership_snapshots": SNAPSHOT,
    "st_snapshots": SNAPSHOT,
    "suspension_snapshots": SNAPSHOT,
    "securities": [
        ("symbol", pa.string()),
        ("name_observed", pa.string()),
        ("ipo_date", pa.date32()),
        ("source_out_date", pa.date32()),
        ("source_type", pa.string()),
        ("source_listing_status_observed", pa.string()),
        ("out_date_basis", pa.string()),
    ],
    "dividends": [("symbol", pa.string())]
    + [(v, pa.date32()) for v in DIVIDEND_DATES.values()]
    + [
        ("cash_before_tax_cny_per_share", DECIMAL),
        ("cash_after_tax_expression", pa.string()),
        ("bonus_shares_per_share", DECIMAL),
        ("reserve_shares_per_share", DECIMAL),
        ("distribution_text", pa.string()),
        ("date_precision", pa.string()),
    ],
    "factors": [
        ("symbol", pa.string()),
        ("ex_date", pa.date32()),
        ("source_forward_factor", DECIMAL),
        ("source_backward_cumulative_factor", DECIMAL),
        ("source_adjust_factor_value", DECIMAL),
        ("factor_basis", pa.string()),
    ],
}
SCHEMAS = {
    name: pa.schema(
        COMMON + fields,
        metadata={
            b"schema_version": VERSION.encode(),
            b"data_kind": b"real_candidate",
            b"research_eligible": b"false",
        },
    )
    for name, fields in FIELDS.items()
}
KEYS = {
    "bars": ("symbol", "event_date"),
    "calendar": ("event_date",),
    "securities": ("symbol",),
    "dividends": ("symbol", "ex_date"),
    "factors": ("symbol", "ex_date"),
    **{
        name: ("query_date", "symbol")
        for name in ("membership_snapshots", "st_snapshots", "suspension_snapshots")
    },
}


def decimal_value(text):
    value = Decimal(text)
    if not value.is_finite() or abs(value) >= Decimal("1e20"):
        raise ValueError("non-finite or out-of-range decimal")
    with localcontext() as context:
        context.prec = 50
        if value != value.quantize(Decimal("0.000000000001")):
            raise ValueError("precision exceeds candidate schema; raw value preserved")
    if value < 0:
        raise ValueError("negative financial value")
    return value


def day(text):
    parsed = date.fromisoformat(text)
    if parsed.isoformat() != text:
        raise ValueError("noncanonical date")
    return parsed


def bit(text):
    if text not in {"0", "1"}:
        raise ValueError("expected source flag 0 or 1")
    return text == "1"


def shares(text):
    if not text.isascii() or not text.isdigit() or int(text) >= 2**63:
        raise ValueError("expected a nonnegative whole-share count")
    return int(text)


def convert(record):
    """Return every source row, including invalid rows, with localized issues."""
    kind = API_KINDS[record["api"]]
    key = query_id(record)
    locator = record["raw_locator"]
    observed = datetime.fromisoformat(record["observed_at_utc"])
    if observed.tzinfo is None:
        raise ValueError("observation time requires timezone")
    fields = record["fields"]
    if not fields or len(set(fields)) != len(fields):
        raise ValueError("ambiguous raw fields")
    output, issues = [], []
    for number, values in enumerate(record["rows"]):
        context = {
            "query_id": key,
            "raw_path": locator["path"],
            "raw_sha256": locator["sha256"],
            "raw_query_name": locator.get("query_name"),
            "row_number": number,
        }
        row = {
            **context,
            "source": "baostock",
            "sdk": record["sdk"],
            "observed_at_utc": observed.astimezone(timezone.utc),
            "observation_precision": record["observation_precision"],
            "historical_publish_time": None,
            "available_time": None,
            "time_basis": "unknown_historical_vintage",
            "record_status": "valid_candidate",
            "raw_values_json": json.dumps({"fields": fields, "values": values}, ensure_ascii=False),
        }
        start = len(issues)

        def issue(code, field, value, detail, status="fail"):
            issues.append(
                {
                    **context,
                    "status": status,
                    "code": code,
                    "field": field,
                    "raw_value": value,
                    "detail": detail,
                }
            )

        if len(values) != len(fields) or any(not isinstance(v, str) for v in values):
            issue("raw_shape", "*", values, "one source string required per field")
            raw = {}
        else:
            raw = dict(zip(fields, values))

        def take(name, parse=str, required=False):
            value = raw.get(name)
            if value is None or value == "":
                if required:
                    issue(
                        "required_value_missing",
                        name,
                        value,
                        "absent and empty are preserved in raw",
                    )
                return None
            try:
                return parse(value)
            except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
                issue("type_or_value_error", name, value, str(exc))
                return None

        if kind != "calendar":
            row["symbol"] = take("code", symbol, True)
            requested = record["parameters"].get("code")
            if requested and raw.get("code") != requested:
                issue("unexpected_security", "code", raw.get("code"), "differs from query code")
        if kind in {"membership_snapshots", "st_snapshots", "suspension_snapshots"}:
            row.update(
                query_date=day(record["parameters"]["date"]),
                source_update_date=take("updateDate", day, True),
                name_observed=take("code_name"),
            )
            if row["source_update_date"] and row["source_update_date"] > row["query_date"]:
                issue(
                    "future_snapshot",
                    "updateDate",
                    raw["updateDate"],
                    "update after requested date",
                )
        elif kind == "calendar":
            row.update(
                event_date=take("calendar_date", day, True),
                is_open=take("is_trading_day", bit, True),
                exchange_scope="SSE_SZSE_provider_joint",
            )
        elif kind == "bars":
            row.update(
                event_date=take("date", day, True),
                source_trading_active=take("tradestatus", bit, True),
                source_is_st=take("isST", bit, True),
                source_adjustflag=take("adjustflag"),
            )
            active = row["source_trading_active"] is True
            for name in ("open", "high", "low", "close", "preclose"):
                row[name] = take(name, decimal_value, active and name != "preclose")
            row["volume_shares"] = take("volume", shares, active)
            row["amount_cny"] = take("amount", decimal_value, active)
            row["turnover_fraction"] = take("turn", lambda v: decimal_value(str(Decimal(v) / 100)))
            if row["source_adjustflag"] not in {None, "3"}:
                issue(
                    "adjusted_price", "adjustflag", raw["adjustflag"], "raw bars must be unadjusted"
                )
            prices = [row[n] for n in ("open", "high", "low", "close")]
            if all(p is not None for p in prices):
                o, h, low, c = prices
                if not low <= o <= h or not low <= c <= h or (active and min(prices) <= 0):
                    issue("ohlc_order", "OHLC", [str(p) for p in prices], "invalid price bounds")
            row["trade_observation"] = (
                "active_reported_not_execution_guarantee"
                if active
                else "suspended_missing_quantity"
                if row["volume_shares"] is None
                else "suspended_zero_quantity"
                if row["volume_shares"] == 0
                else "status_quantity_conflict"
            )
            if row["source_trading_active"] is False and (
                (row["volume_shares"] or 0) != 0 or (row["amount_cny"] or 0) != 0
            ):
                issue(
                    "suspension_quantity_conflict",
                    "volume/amount",
                    [raw.get("volume"), raw.get("amount")],
                    "suspended source status has nonzero quantity",
                )
        elif kind == "securities":
            row.update(
                name_observed=take("code_name"),
                ipo_date=take("ipoDate", day, True),
                source_out_date=take("outDate", day),
                source_type=take("type", required=True),
                source_listing_status_observed=take("status", required=True),
                out_date_basis="source_date_not_verified_as_first_delisted_day",
            )
            if row["source_type"] != "1":
                issue("not_equity", "type", raw.get("type"), "source type must be stock")
            if row["source_listing_status_observed"] not in {"0", "1"}:
                issue(
                    "unknown_listing_status", "status", raw.get("status"), "unknown current status"
                )
            if (
                row["source_out_date"]
                and row["ipo_date"]
                and row["source_out_date"] < row["ipo_date"]
            ):
                issue("reversed_listing_dates", "outDate", raw["outDate"], "outDate precedes IPO")
        elif kind == "dividends":
            for source, target in DIVIDEND_DATES.items():
                row[target] = take(source, day, source == "dividOperateDate")
            row.update(
                cash_before_tax_cny_per_share=take("dividCashPsBeforeTax", decimal_value),
                cash_after_tax_expression=take("dividCashPsAfterTax"),
                bonus_shares_per_share=take("dividStocksPs", decimal_value),
                reserve_shares_per_share=take("dividReserveToStockPs", decimal_value),
                distribution_text=take("dividCashStock"),
                date_precision="day",
            )
        elif kind == "factors":
            row.update(
                ex_date=take("dividOperateDate", day, True),
                source_forward_factor=take("foreAdjustFactor", decimal_value, True),
                source_backward_cumulative_factor=take("backAdjustFactor", decimal_value, True),
                source_adjust_factor_value=take("adjustFactor", decimal_value, True),
                factor_basis="provider_cumulative_values_algorithm_unverified",
            )
            for field in (
                "source_forward_factor",
                "source_backward_cumulative_factor",
                "source_adjust_factor_value",
            ):
                if row[field] == 0:
                    issue("zero_factor", field, "0", "price adjustment factors must be positive")
        event = row.get("event_date", row.get("ex_date"))
        p = record["parameters"]
        if event and "start_date" in p and not day(p["start_date"]) <= event <= day(p["end_date"]):
            issue("outside_query_interval", "event_date", str(event), "event outside request")
        if kind == "dividends" and event and event.year != int(p["year"]):
            issue(
                "outside_query_year", "dividOperateDate", str(event), "event outside operate year"
            )
        if any(i["status"] == "fail" for i in issues[start:]):
            row["record_status"] = "invalid"
        output.append(row)
    return kind, output, issues


def table(kind, rows):
    return pa.Table.from_pylist(rows, schema=SCHEMAS[kind])
