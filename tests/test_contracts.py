from copy import deepcopy
from datetime import timedelta

import pytest

from ashare_lab.contracts import (
    ContractError,
    as_of,
    members_at,
    require_feature_available,
    validate_records,
)
from ashare_lab.synthetic import at, fixture, sessions


@pytest.fixture
def tables():
    return fixture()


def test_late_correction_is_invisible_until_available(tables):
    early = as_of("bars", tables["bars"], at(sessions()[0])).to_pylist()
    late = as_of("bars", tables["bars"], at(sessions()[2], 17)).to_pylist()
    original = next(r for r in early if r["symbol"] == "600000.SH")
    corrected = next(
        r for r in late if r["symbol"] == "600000.SH" and r["event_date"] == sessions()[0]
    )
    assert original["record_version"] == 1
    assert corrected["record_version"] == 2
    assert corrected["close"] == pytest.approx(original["close"] + 0.01)


def test_membership_revision_then_effective_boundary(tables):
    # Before a removal is announced, even a future-date question cannot know it.
    assert "600000.SH" in members_at(tables["membership"], sessions()[8], at(sessions()[5]))
    assert members_at(tables["membership"], sessions()[7], at(sessions()[7])) == [
        "000001.SZ",
        "600000.SH",
    ]
    assert members_at(tables["membership"], sessions()[8], at(sessions()[8])) == [
        "000001.SZ",
        "600001.SH",
    ]


def test_unknown_and_future_availability_fail(tables):
    with pytest.raises(ContractError, match="after decision"):
        require_feature_available(tables["bars"], at(sessions()[0]))
    row = tables["bars"].to_pylist()[0]
    row["available_time"] = None
    with pytest.raises(ContractError, match="null forbidden"):
        validate_records("bars", [row], data_kind="synthetic")


@pytest.mark.parametrize(
    "change,match",
    [
        ({"record_version": 0}, "positive integer"),
        ({"source_version": ""}, "nonempty"),
        ({"data_kind": "real"}, "cannot be mixed"),
        ({"event_date": "2024-01-02"}, "date required"),
        ({"close": float("nan")}, "finite"),
        ({"volume": -1}, "nonnegative"),
        ({"open": 10000}, "OHLC"),
        ({"symbol": "600000"}, "symbol"),
        ({"unexpected": 1}, "unknown"),
    ],
)
def test_bad_rows_rejected(tables, change, match):
    row = tables["bars"].to_pylist()[0]
    row.update(change)
    with pytest.raises(ContractError, match=match):
        validate_records("bars", [row], data_kind="synthetic")


def test_duplicate_primary_key_rejected(tables):
    row = tables["bars"].to_pylist()[0]
    with pytest.raises(ContractError, match="duplicate revision key"):
        validate_records("bars", [row, deepcopy(row)], data_kind="synthetic")


def test_naive_timestamp_and_reversed_time_rejected(tables):
    row = tables["bars"].to_pylist()[0]
    row["publish_time"] = row["publish_time"].replace(tzinfo=None)
    with pytest.raises(ContractError, match="timezone-aware"):
        validate_records("bars", [row], data_kind="synthetic")
    row = tables["bars"].to_pylist()[0]
    row["available_time"] -= timedelta(seconds=1)
    with pytest.raises(ContractError, match="publish_time <="):
        validate_records("bars", [row], data_kind="synthetic")


def test_ambiguous_revision_tie_rejected(tables):
    row = tables["bars"].to_pylist()[0]
    revised = {**row, "record_version": 2}
    with pytest.raises(ContractError, match="strictly increasing"):
        validate_records("bars", [row, revised], data_kind="synthetic")


def test_bar_before_close_rejected(tables):
    row = tables["bars"].to_pylist()[0]
    row["publish_time"] = at(sessions()[0], 14, 59)
    with pytest.raises(ContractError, match="before close"):
        validate_records("bars", [row], data_kind="synthetic")


def test_calendar_shanghai_date_check(tables):
    row = tables["calendar"].to_pylist()[0]
    row["open_time"] -= timedelta(days=1)
    with pytest.raises(ContractError, match="Shanghai date"):
        validate_records("calendar", [row], data_kind="synthetic")


def test_future_announced_action_not_yet_an_observed_feature(tables):
    table = as_of("actions", tables["actions"], at(sessions()[8]))
    assert len(table) == 1
    with pytest.raises(ContractError, match="event_date"):
        require_feature_available(table, at(sessions()[8]))


def test_overlapping_membership_rejected(tables):
    rows = tables["membership"].to_pylist()
    rows.append({**rows[0], "membership_id": "another-interval"})
    table = validate_records("membership", rows, data_kind="synthetic")
    with pytest.raises(ContractError, match="overlapping"):
        members_at(table, sessions()[0], at(sessions()[0]))


@pytest.mark.parametrize(
    "name,field,value",
    [
        ("membership", "effective_to", sessions()[0]),
        ("actions", "factor", 0),
        ("status", "is_suspended", True),
        ("status", "limit_up", -1),
    ],
)
def test_other_contracts_enforce_domains(tables, name, field, value):
    row = tables[name].to_pylist()[0]
    row[field] = value
    with pytest.raises(ContractError):
        validate_records(name, [row], data_kind="synthetic")
