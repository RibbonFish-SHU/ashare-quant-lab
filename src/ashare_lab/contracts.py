"""Strict, versioned ingestion contracts. UTC storage; Shanghai session dates."""

from datetime import date, datetime
import math
import re
from typing import Iterable

import pyarrow as pa

SCHEMA_VERSION = "phase0-v1"
TS = pa.timestamp("us", tz="UTC")
COMMON = [
    pa.field("event_date", pa.date32(), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("source_version", pa.string(), nullable=False),
    pa.field("record_version", pa.int32(), nullable=False),
    pa.field("publish_time", TS, nullable=False),
    pa.field("available_time", TS, nullable=False),
    pa.field("ingested_at", TS, nullable=False),
    pa.field("data_kind", pa.string(), nullable=False),
]


def field(name, dtype, nullable=False):
    return pa.field(name, dtype, nullable=nullable)


SCHEMAS = {
    "bars": pa.schema(
        COMMON
        + [
            field("symbol", pa.string()),
            *[
                field(name, pa.float64())
                for name in ("open", "high", "low", "close", "volume", "amount", "turnover")
            ],
        ]
    ),
    "calendar": pa.schema(
        COMMON
        + [
            field("exchange", pa.string()),
            field("is_open", pa.bool_()),
            field("open_time", TS, True),
            field("close_time", TS, True),
        ]
    ),
    "membership": pa.schema(
        COMMON
        + [
            field("index_symbol", pa.string()),
            field("symbol", pa.string()),
            field("membership_id", pa.string()),
            field("effective_to", pa.date32(), True),
        ]
    ),
    "actions": pa.schema(
        COMMON
        + [
            field("symbol", pa.string()),
            field("action_id", pa.string()),
            field("action_type", pa.string()),
            field("factor", pa.float64()),
            field("cash_per_share", pa.float64()),
        ]
    ),
    "status": pa.schema(
        COMMON
        + [
            field("symbol", pa.string()),
            field("listed_date", pa.date32()),
            field("delisted_date", pa.date32(), True),
            field("is_st", pa.bool_()),
            field("is_suspended", pa.bool_()),
            field("trading_status", pa.string()),
            field("limit_up", pa.float64(), True),
            field("limit_down", pa.float64(), True),
        ]
    ),
}

# A membership interval can be revised, so its identity must not depend on its end date.
KEYS = {
    "bars": ("source", "symbol", "event_date"),
    "calendar": ("source", "exchange", "event_date"),
    "membership": ("source", "index_symbol", "symbol", "membership_id"),
    "actions": ("source", "symbol", "action_id"),
    "status": ("source", "symbol", "event_date"),
}


class ContractError(ValueError):
    """Invalid or ambiguous data cannot enter a research snapshot."""


def aware(value: datetime, name="timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{name}: timezone-aware datetime required")
    return value


def validate_records(name: str, records: Iterable[dict], *, data_kind: str) -> pa.Table:
    if name not in SCHEMAS:
        raise ContractError(f"unknown table: {name}")
    if data_kind not in {"synthetic", "real"}:
        raise ContractError("data_kind must be synthetic or real")
    schema = SCHEMAS[name]
    rows = list(records)
    revisions = {}
    for number, row in enumerate(rows):
        prefix = f"{name}[{number}]"
        missing = set(schema.names) - row.keys()
        extra = row.keys() - set(schema.names)
        if missing or extra:
            raise ContractError(f"{prefix}: missing={sorted(missing)}, unknown={sorted(extra)}")
        for item in schema:
            value = row[item.name]
            label = f"{prefix}.{item.name}"
            if value is None:
                if not item.nullable:
                    raise ContractError(f"{label}: null forbidden")
                continue
            if pa.types.is_timestamp(item.type):
                aware(value, label)
            elif pa.types.is_date(item.type):
                if type(value) is not date:
                    raise ContractError(f"{label}: date required, not a timestamp/string")
            elif pa.types.is_string(item.type):
                if not isinstance(value, str) or not value.strip():
                    raise ContractError(f"{label}: nonempty string required")
            elif pa.types.is_boolean(item.type):
                if type(value) is not bool:
                    raise ContractError(f"{label}: bool required")
            elif pa.types.is_integer(item.type):
                if type(value) is not int or value < 1:
                    raise ContractError(f"{label}: positive integer required")
            elif pa.types.is_floating(item.type):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ContractError(f"{label}: numeric value required")
                if not math.isfinite(value):
                    raise ContractError(f"{label}: finite value required")
        if row["data_kind"] != data_kind:
            raise ContractError(f"{prefix}: real and synthetic data cannot be mixed")
        if not row["publish_time"] <= row["available_time"] <= row["ingested_at"]:
            raise ContractError(f"{prefix}: require publish_time <= available_time <= ingested_at")
        if "symbol" in row and not re.fullmatch(r"\d{6}\.(SH|SZ)", row["symbol"]):
            raise ContractError(f"{prefix}: symbol must be six digits with .SH/.SZ")
        _validate_domain(name, row, prefix)
        key = tuple(row[k] for k in KEYS[name])
        revisions.setdefault(key, []).append(row)
    for key, versions in revisions.items():
        versions.sort(key=lambda row: row["record_version"])
        for prev, curr in zip(versions, versions[1:]):
            if prev["record_version"] == curr["record_version"]:
                raise ContractError(f"{name}: duplicate revision key {key}")
            if prev["available_time"] >= curr["available_time"]:
                raise ContractError(f"{name}: revisions must have strictly increasing availability")
    try:
        return pa.Table.from_pylist(rows, schema=schema).replace_schema_metadata(
            {
                b"schema_version": SCHEMA_VERSION.encode(),
                b"data_kind": data_kind.encode(),
                b"table_name": name.encode(),
            }
        )
    except (pa.ArrowException, OverflowError) as exc:
        raise ContractError(f"{name}: {exc}") from exc


def _validate_domain(name, row, prefix):
    if name == "bars":
        if min(row[k] for k in ("open", "high", "low", "close")) <= 0:
            raise ContractError(f"{prefix}: raw OHLC must be positive")
        if (
            not row["low"]
            <= min(row["open"], row["close"])
            <= max(row["open"], row["close"])
            <= row["high"]
        ):
            raise ContractError(f"{prefix}: inconsistent OHLC")
        if min(row[k] for k in ("volume", "amount", "turnover")) < 0:
            raise ContractError(f"{prefix}: volume/amount/turnover must be nonnegative")
        local_publish = row["publish_time"].astimezone(_shanghai())
        if local_publish.date() < row["event_date"] or (
            local_publish.date() == row["event_date"] and local_publish.hour < 15
        ):
            raise ContractError(f"{prefix}: completed daily bar cannot be published before close")
    elif name == "calendar":
        if row["exchange"] not in {"SSE", "SZSE"}:
            raise ContractError(f"{prefix}: unknown exchange")
        if row["is_open"]:
            start, end = row["open_time"], row["close_time"]
            if start is None or end is None or start >= end:
                raise ContractError(f"{prefix}: open session needs ordered open/close times")
            if any(t.astimezone(_shanghai()).date() != row["event_date"] for t in (start, end)):
                raise ContractError(f"{prefix}: session timestamp disagrees with Shanghai date")
        elif row["open_time"] is not None or row["close_time"] is not None:
            raise ContractError(f"{prefix}: closed session cannot have open/close times")
    elif name == "membership":
        if row["effective_to"] is not None and row["effective_to"] <= row["event_date"]:
            raise ContractError(f"{prefix}: empty/reversed membership interval")
    elif name == "actions":
        if row["action_type"] not in {"split", "cash_dividend", "adjustment"}:
            raise ContractError(f"{prefix}: unknown corporate action")
        if row["factor"] <= 0 or row["cash_per_share"] < 0:
            raise ContractError(f"{prefix}: invalid corporate action factor/cash")
    elif name == "status":
        if row["listed_date"] > row["event_date"]:
            raise ContractError(f"{prefix}: status predates listing")
        if row["delisted_date"] is not None and row["delisted_date"] <= row["listed_date"]:
            raise ContractError(f"{prefix}: delisting must follow listing")
        if row["trading_status"] not in {"active", "suspended", "delisted"}:
            raise ContractError(f"{prefix}: unknown trading status")
        if row["is_suspended"] != (row["trading_status"] == "suspended"):
            raise ContractError(f"{prefix}: inconsistent suspension status")
        if any(row[k] is not None and row[k] <= 0 for k in ("limit_up", "limit_down")):
            raise ContractError(f"{prefix}: limit prices must be positive or unknown (null)")
        if row["limit_up"] is not None and row["limit_down"] is not None:
            if row["limit_down"] > row["limit_up"]:
                raise ContractError(f"{prefix}: reversed limit prices")


def _shanghai():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Shanghai")


def as_of(name: str, table: pa.Table, cutoff: datetime) -> pa.Table:
    """Filter knowledge time BEFORE selecting the latest revision; never backfill."""
    aware(cutoff, "cutoff")
    kind = (table.schema.metadata or {}).get(b"data_kind", b"").decode()
    table = validate_records(name, table.to_pylist(), data_kind=kind)
    selected = {}
    for row in table.to_pylist():
        if row["available_time"] > cutoff:
            continue
        key = tuple(row[k] for k in KEYS[name])
        prev = selected.get(key)
        if prev is None or row["record_version"] > prev["record_version"]:
            selected[key] = row
    return validate_records(name, selected.values(), data_kind=kind)


def members_at(table: pa.Table, session: date, cutoff: datetime) -> list[str]:
    rows = as_of("membership", table, cutoff).to_pylist()
    if len({(r["source"], r["index_symbol"]) for r in rows}) > 1:
        raise ContractError("select a single membership source and index")
    active = [
        r
        for r in rows
        if r["event_date"] <= session and (r["effective_to"] is None or session < r["effective_to"])
    ]
    symbols = [r["symbol"] for r in active]
    if len(symbols) != len(set(symbols)):
        raise ContractError("overlapping membership intervals or multiple index/source selection")
    return sorted(symbols)


def require_feature_available(table: pa.Table, cutoff: datetime) -> None:
    """Fail on future/unknown observations instead of accepting them as features."""
    aware(cutoff, "cutoff")
    for row in table.to_pylist():
        if aware(row["available_time"], "available_time") > cutoff:
            raise ContractError("feature available_time is after decision cutoff")
        if row["event_date"] > cutoff.astimezone(_shanghai()).date():
            raise ContractError("feature event_date is after decision date")
