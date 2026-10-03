from datetime import date, timedelta

import pytest

from ashare_lab.contracts import ContractError, validate_records
from ashare_lab.synthetic import at, fixture, sessions
from ashare_lab.temporal import (
    LabelInterval,
    check_transform_fit,
    next_open_window,
    training_intervals,
)


def test_five_sessions_cross_weekend_and_never_trade_signal_close():
    window = next_open_window(
        fixture()["calendar"], sessions()[0], at(sessions()[0]), at(sessions()[6], 15, 10)
    )
    assert window.entry_time == at(date(2024, 1, 3), 9, 30)
    assert window.exit_time == at(date(2024, 1, 10), 9, 30)
    assert window.available_time > window.exit_time
    assert window.entry_time > window.signal_time


def test_signal_before_close_and_truncated_calendar_rejected():
    with pytest.raises(ContractError, match="after the session close"):
        next_open_window(
            fixture()["calendar"], sessions()[0], at(sessions()[0], 15), at(sessions()[6], 15, 10)
        )
    with pytest.raises(ContractError, match="insufficient"):
        next_open_window(
            fixture()["calendar"],
            sessions()[-1],
            at(sessions()[-1]),
            at(sessions()[-1]) + timedelta(days=10),
        )


def test_purge_real_price_overlap_and_label_maturity():
    def d(day, hour=16):
        return at(date(2024, 1, day), hour)

    mature = LabelInterval(d(2), d(3, 9), d(5, 9), d(5))
    delayed = LabelInterval(d(2), d(3, 9), d(5, 9), d(20))
    shared_exit = LabelInterval(d(3), d(4, 9), d(9, 9), d(9))
    valid = LabelInterval(d(8), d(9, 9), d(16, 9), d(16))
    result = training_intervals(
        [mature, delayed, shared_exit],
        train_start=d(1),
        train_end=d(8),
        fit_time=d(8),
        evaluation=[valid],
    )
    assert result == [mature]


def test_fitting_after_evaluation_signal_rejected():
    signal = at(sessions()[8])
    valid = LabelInterval(
        signal, at(sessions()[9], 9, 30), at(sessions()[14], 9, 30), at(sessions()[14], 15, 10)
    )
    with pytest.raises(ContractError, match="first evaluation signal"):
        training_intervals(
            [],
            train_start=at(sessions()[0]),
            train_end=signal,
            fit_time=signal + timedelta(seconds=1),
            evaluation=[valid],
        )


def test_signal_time_must_match_local_session_date():
    with pytest.raises(ContractError, match="signal_date"):
        next_open_window(
            fixture()["calendar"], sessions()[0], at(sessions()[1]), at(sessions()[6], 15, 10)
        )


def test_inverted_time_and_transform_fit_rejected():
    d = at(sessions()[0])
    with pytest.raises(ContractError, match="signal < entry"):
        LabelInterval(d, d, d + timedelta(days=5), d + timedelta(days=6))
    with pytest.raises(ContractError, match="only nonempty training"):
        check_transform_fit([sessions()[8]], set(sessions()[:8]))
    check_transform_fit(sessions()[:5], set(sessions()[:8]))


def fixture_window(calendar, signal_time=None):
    return next_open_window(
        calendar,
        sessions()[0],
        signal_time or at(sessions()[0]),
        at(sessions()[6], 15, 10),
    )


@pytest.mark.parametrize("second_source", ["alternating_dates", "closed_day", "same_day"])
def test_calendar_rejects_multiple_visible_sources_before_open_filter(second_source):
    rows = fixture()["calendar"].to_pylist()
    if second_source == "alternating_dates":
        for row in rows:
            if row["exchange"] == "SSE" and sessions().index(row["event_date"]) % 2:
                row["source"] = "synthetic-other"
    else:
        extra = {**rows[0], "source": "synthetic-other"}
        if second_source == "closed_day":
            extra.update(
                event_date=date(2024, 1, 6), is_open=False, open_time=None, close_time=None
            )
        rows.append(extra)
    calendar = validate_records("calendar", rows, data_kind="synthetic")
    with pytest.raises(ContractError, match="single calendar source"):
        fixture_window(calendar)


def test_calendar_source_check_ignores_other_exchange():
    original = fixture()["calendar"]
    rows = original.to_pylist()
    for row in rows:
        if row["exchange"] == "SZSE":
            row["source"] = "synthetic-other"
    calendar = validate_records("calendar", rows, data_kind="synthetic")
    assert fixture_window(calendar) == fixture_window(original)


def test_calendar_source_check_uses_availability_cutoff():
    original = fixture()["calendar"]
    rows = original.to_pylist()
    available = at(sessions()[0]) + timedelta(microseconds=1)
    rows.append(
        {
            **rows[0],
            "source": "synthetic-late",
            "publish_time": available,
            "available_time": available,
        }
    )
    calendar = validate_records("calendar", rows, data_kind="synthetic")
    assert fixture_window(calendar) == fixture_window(original)
    with pytest.raises(ContractError, match="single calendar source"):
        fixture_window(calendar, signal_time=available)


def test_single_source_calendar_accepts_closed_days_and_revisions():
    original = fixture()["calendar"]
    rows = original.to_pylist()
    rows.append(
        {
            **rows[0],
            "event_date": date(2024, 1, 6),
            "is_open": False,
            "open_time": None,
            "close_time": None,
        }
    )
    rows.append(
        {
            **rows[0],
            "record_version": 2,
            "source_version": "synthetic-v2",
            "publish_time": at(sessions()[0], 9),
            "available_time": at(sessions()[0], 9),
        }
    )
    calendar = validate_records("calendar", rows, data_kind="synthetic")
    assert fixture_window(calendar) == fixture_window(original)
