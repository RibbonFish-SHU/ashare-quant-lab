"""Execution-time label boundaries, independent of any returns or model."""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pyarrow as pa

from .contracts import ContractError, as_of, aware


@dataclass(frozen=True)
class LabelInterval:
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    available_time: datetime

    def __post_init__(self):
        for name in ("signal_time", "entry_time", "exit_time", "available_time"):
            aware(getattr(self, name), name)
        if not self.signal_time < self.entry_time < self.exit_time <= self.available_time:
            raise ContractError("label requires signal < entry < exit <= label availability")


def next_open_window(
    calendar: pa.Table,
    signal_date: date,
    signal_time: datetime,
    exit_observation_available: datetime,
    *,
    exchange="SSE",
    horizon=5,
) -> LabelInterval:
    """Engineering convention: T+1 open to T+6 open = five session intervals.

    The selected exchange must have one visible source, including closed dates.
    Future scheduled sessions must already be published at signal time. Actual
    execution/price availability is supplied separately, never inferred as a feature.
    """
    aware(signal_time, "signal_time")
    aware(exit_observation_available, "exit_observation_available")
    if signal_time.astimezone(ZoneInfo("Asia/Shanghai")).date() != signal_date:
        raise ContractError("signal_time must fall on signal_date in Asia/Shanghai")
    if type(horizon) is not int or horizon < 1:
        raise ContractError("horizon must be a positive session count")
    rows = [
        r for r in as_of("calendar", calendar, signal_time).to_pylist() if r["exchange"] == exchange
    ]
    if len({r["source"] for r in rows}) != 1:
        raise ContractError("select a single calendar source for the requested exchange")
    rows = [r for r in rows if r["is_open"]]
    rows.sort(key=lambda r: r["event_date"])
    dates = [r["event_date"] for r in rows]
    if signal_date not in dates:
        raise ContractError("signal date is not a known open session")
    index = dates.index(signal_date)
    if signal_time <= rows[index]["close_time"]:
        raise ContractError("signal must be after the session close")
    if index + 1 + horizon >= len(rows):
        raise ContractError("insufficient known future sessions for label interval")
    return LabelInterval(
        signal_time,
        rows[index + 1]["open_time"],
        rows[index + 1 + horizon]["open_time"],
        exit_observation_available,
    )


def training_intervals(
    labels: list[LabelInterval],
    *,
    train_start: datetime,
    train_end: datetime,
    fit_time: datetime,
    evaluation: list[LabelInterval],
) -> list[LabelInterval]:
    """Half-open signal range; mature labels only; purge actual price overlap."""
    for name, value in (
        ("train_start", train_start),
        ("train_end", train_end),
        ("fit_time", fit_time),
    ):
        aware(value, name)
    if not train_start < train_end <= fit_time:
        raise ContractError("require train_start < train_end <= fit_time")
    if any(label.signal_time < train_end for label in evaluation):
        raise ContractError("evaluation must follow the training signal range")
    if evaluation and fit_time > min(label.signal_time for label in evaluation):
        raise ContractError("model fit must finish no later than the first evaluation signal")
    selected = []
    for label in labels:
        if not train_start <= label.signal_time < train_end:
            continue
        if label.available_time > fit_time:
            continue
        if any(
            label.entry_time <= other.exit_time and other.entry_time <= label.exit_time
            for other in evaluation
        ):
            continue
        selected.append(label)
    return sorted(selected, key=lambda label: label.signal_time)


def check_transform_fit(fit_dates: list[date], train_dates: set[date]) -> None:
    if not fit_dates or not set(fit_dates).issubset(train_dates):
        raise ContractError("transform fitting must use only nonempty training observations")
