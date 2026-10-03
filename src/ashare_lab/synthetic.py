"""Small deterministic fixtures; never a source of market research results."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from .contracts import validate_records

SH = ZoneInfo("Asia/Shanghai")


def at(day: date, hour=16, minute=0):
    return datetime.combine(day, time(hour, minute), SH).astimezone(timezone.utc)


def sessions():
    # Artificial weekday calendar, not an exchange calendar or approved research range.
    return [
        date(2024, 1, 2) + timedelta(days=i)
        for i in range(36)
        if (date(2024, 1, 2) + timedelta(days=i)).weekday() < 5
    ][:24]


def fixture(seed=20261003):
    rng = np.random.default_rng(seed)
    days = sessions()
    announced = at(date(2023, 12, 1))

    def common(day, available=None, version=1):
        timestamp = available or at(day, 15, 10)
        return {
            "event_date": day,
            "source": "synthetic-fixture",
            "source_version": "synthetic-v1",
            "record_version": version,
            "publish_time": timestamp,
            "available_time": timestamp,
            "ingested_at": at(date(2024, 3, 1)),
            "data_kind": "synthetic",
        }

    data = {name: [] for name in ("bars", "calendar", "membership", "actions", "status")}
    for day in days:
        for exchange in ("SSE", "SZSE"):
            data["calendar"].append(
                {
                    **common(day, announced),
                    "exchange": exchange,
                    "is_open": True,
                    "open_time": at(day, 9, 30),
                    "close_time": at(day, 15),
                }
            )
        for number, symbol in enumerate(("600000.SH", "000001.SZ", "600001.SH")):
            opening = 10.0 + number + len(data["bars"]) * 0.01
            closing = opening * (1 + rng.uniform(-0.015, 0.015))
            data["bars"].append(
                {
                    **common(day),
                    "symbol": symbol,
                    "open": opening,
                    "high": max(opening, closing) + 0.1,
                    "low": min(opening, closing) - 0.1,
                    "close": closing,
                    "volume": 10000.0,
                    "amount": opening * 10000,
                    "turnover": 0.01,
                }
            )
            data["status"].append(
                {
                    **common(day, at(day, 9)),
                    "symbol": symbol,
                    "listed_date": date(2020, 1, 1),
                    "delisted_date": None,
                    "is_st": False,
                    "is_suspended": False,
                    "trading_status": "active",
                    "limit_up": None,
                    "limit_down": None,
                }
            )
    for symbol, identifier in (("600000.SH", "a"), ("000001.SZ", "b")):
        data["membership"].append(
            {
                **common(days[0], announced),
                "index_symbol": "000300.SH",
                "symbol": symbol,
                "membership_id": identifier,
                "effective_to": None,
            }
        )
    data["membership"].append(
        {
            **common(days[0], at(days[6]), 2),
            "index_symbol": "000300.SH",
            "symbol": "600000.SH",
            "membership_id": "a",
            "effective_to": days[8],
        }
    )
    data["membership"].append(
        {
            **common(days[8], at(days[6])),
            "index_symbol": "000300.SH",
            "symbol": "600001.SH",
            "membership_id": "c",
            "effective_to": None,
        }
    )
    # A correction to T is available at T+2 17:00, not at T close.
    corrected = dict(data["bars"][0])
    corrected.update(common(days[0], at(days[2], 17), 2))
    corrected["close"] += 0.01
    data["bars"].append(corrected)
    data["actions"].append(
        {
            **common(days[10], at(days[8])),
            "symbol": "000001.SZ",
            "action_id": "synthetic-action-1",
            "action_type": "split",
            "factor": 2.0,
            "cash_per_share": 0.0,
        }
    )
    return {
        name: validate_records(name, rows, data_kind="synthetic") for name, rows in data.items()
    }
