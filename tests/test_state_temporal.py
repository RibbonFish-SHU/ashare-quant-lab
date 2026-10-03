"""Self-contained counterexamples for event time, visibility and partial sessions."""

from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from ashare_lab.state_evidence.temporal import (
    daily_reference,
    event,
    event_conflicts,
    state_at,
    visible_events,
)

SYMBOL = "688065.SH"


def halt_resume():
    return [
        event(
            symbol=SYMBOL,
            kind=kind,
            effective_date=d,
            effective_session=session,
            observed_at_utc="2026-10-03T12:00:00+00:00",
        )
        for kind, d, session in [
            ("halt", "2023-06-15", "afternoon_open"),
            ("resume", "2023-06-26", "morning_open"),
        ]
    ]


def name(d="2022-01-01", before="Old", after="ST New", **kwargs):
    return event(
        symbol="000001.SZ",
        kind="name_change",
        effective_date=d,
        effective_session="date_only",
        before_name=before,
        after_name=after,
        before_contains_st="ST" in before.upper(),
        after_contains_st="ST" in after.upper(),
        observed_at_utc="2026-10-03T12:00:00+00:00",
        **kwargs,
    )


def retro(events, d, **kwargs):
    return daily_reference(events, SYMBOL, date.fromisoformat(d), is_open=True, **kwargs)


def test_partial_halt_with_positive_trade_volume_is_not_whole_day_conflict():
    result = retro(halt_resume(), "2023-06-15", volume_shares=353426)
    assert result["classification"] == "partial_day_halt_reference"
    assert result["positive_volume_conflict"] is False
    assert result["morning"]["trading_reference"] == "unknown"
    assert result["afternoon"]["trading_reference"] == "halt_reference"
    assert result["can_trade"] is None


@pytest.mark.parametrize("d", ["2023-06-16", "2023-06-19", "2023-06-20", "2023-06-21"])
def test_full_halted_sessions(d):
    assert retro(halt_resume(), d, volume_shares=0)["classification"] == "full_day_halt_reference"
    assert retro(halt_resume(), d, volume_shares=1)["positive_volume_conflict"] is True


def test_calendar_closed_days_are_not_full_trading_halts():
    for d in (17, 18, 22, 23, 24, 25):
        result = daily_reference(halt_resume(), SYMBOL, date(2023, 6, d), is_open=False)
        assert result["classification"] == "closed_calendar_day"


def test_resume_at_morning_is_independent_and_does_not_certify_tradability():
    result = retro(halt_resume(), "2023-06-26", volume_shares=8046605)
    assert result["classification"] == "no_selected_halt_reference"
    assert result["morning"]["trading_reference"] == "resume_reference"
    assert result["morning"]["can_trade"] is None


def test_later_resume_cannot_close_halt_before_knowledge_time():
    rows = halt_resume()
    rows[0].update(
        available_time="2023-06-15T12:40:00+08:00", availability_basis="verified_timestamp"
    )
    rows[1].update(
        available_time="2023-06-27T09:00:00+08:00", availability_basis="verified_timestamp"
    )
    query = dict(events=rows, symbol=SYMBOL, event_date=date(2023, 6, 26), session="afternoon_open")
    before = state_at(**query, knowledge_time="2023-06-26T15:00:00+08:00")
    after = state_at(**query, knowledge_time="2023-06-27T09:00:00+08:00")
    assert before["trading_reference"] == "halt_reference"
    assert before["excluded_events"] == 1
    assert after["trading_reference"] == "resume_reference"


def test_future_effective_resume_cannot_close_halt_even_when_known():
    rows = halt_resume()
    for row in rows:
        row.update(
            available_time="2023-06-14T09:00:00+08:00", availability_basis="verified_timestamp"
        )
    result = state_at(
        rows, SYMBOL, date(2023, 6, 20), "close", knowledge_time="2023-06-27T09:00:00+08:00"
    )
    assert result["trading_reference"] == "halt_reference"


def test_unknown_availability_and_midnight_label_are_excluded_by_default():
    rows = halt_resume()
    rows[1].update(
        source_timestamp="2023-06-26T00:00:00+08:00", source_timestamp_precision="date_label"
    )
    assert visible_events(rows, "2023-06-30T00:00:00+08:00") == []
    assert visible_events(rows, "2030-01-01T00:00:00+00:00") == []
    assert visible_events(rows, None, view="retrospective") == rows


def test_observation_policy_is_explicit_and_cannot_backdate_discovery():
    rows = halt_resume()
    assert visible_events(rows, "2023-06-30T00:00:00+00:00", policy="observed_at") == []
    result = state_at(
        rows,
        SYMBOL,
        date(2023, 6, 26),
        "close",
        knowledge_time="2026-10-03T12:00:00+00:00",
        policy="observed_at",
    )
    assert result["trading_reference"] == "resume_reference"
    assert result["availability_policy"] == "observed_at"
    assert all(row["available_time"] is None for row in rows)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"knowledge_time": datetime(2023, 1, 1)},
        {"knowledge_time": "2023-01-01T00:00:00+00:00", "policy": "date_midnight"},
    ],
)
def test_strict_query_requires_aware_cutoff_and_known_policy(args):
    with pytest.raises(ValueError):
        state_at([], SYMBOL, date(2023, 1, 1), "close", **args)


def test_no_events_never_certifies_non_st_or_active_trading():
    row = state_at([], "000002.SZ", date(2023, 1, 3), "close", view="retrospective")
    assert row["name_reference"] is None
    assert row["name_contains_st_diagnostic"] is None
    assert row["certified_st"] is None and row["can_trade"] is None
    assert row["trading_reference"] == "unknown"


def test_name_is_a_diagnostic_with_unverified_continuity():
    row = state_at([name()], "000001.SZ", date(2023, 1, 3), "close", view="retrospective")
    assert row["name_reference"] == "ST New" and row["name_contains_st_diagnostic"] is True
    assert row["name_continuity_verified"] is False and row["certified_st"] is None


def test_later_name_does_not_backfill_before_first_event():
    row = state_at([name()], "000001.SZ", date(2000, 1, 1), "close", view="retrospective")
    assert row["name_reference"] is None


def test_broken_name_chain_does_not_choose_a_convenient_history():
    rows = [name(after="Middle"), name("2022-03-01", before="Different", after="End")]
    issues = event_conflicts(rows)
    assert [r["kind"] for r in issues] == ["broken_name_chain"]
    assert (
        state_at(rows, "000001.SZ", date(2023, 1, 3), "close", view="retrospective")[
            "name_reference"
        ]
        is None
    )
    # A future chain break must not contaminate the earlier view.
    assert (
        state_at(rows, "000001.SZ", date(2022, 2, 1), "close", view="retrospective")[
            "name_reference"
        ]
        == "Middle"
    )


def test_invisible_name_conflict_does_not_leak_backwards():
    rows = [
        name(available_time="2022-01-02T00:00:00+00:00", availability_basis="verified_timestamp"),
        name(
            after="Conflict",
            available_time="2023-02-01T00:00:00+00:00",
            availability_basis="verified_timestamp",
        ),
    ]
    result = state_at(
        rows, "000001.SZ", date(2023, 1, 3), "close", knowledge_time="2023-01-03T12:00:00+00:00"
    )
    assert result["name_reference"] == "ST New"


@pytest.mark.parametrize("same", [True, False])
def test_duplicate_and_conflicting_events_preserve_both_locators(same):
    first = halt_resume()[0]
    second = deepcopy(first)
    second["event_id"] = "another-source"
    if not same:
        second["kind"] = "resume"
    issues = event_conflicts([first, second])
    assert issues[0]["kind"] == ("duplicate_event" if same else "conflicting_event")
    state = retro([first, second], "2023-06-16")
    assert state["classification"] == "conflicting_reference"


def test_no_input_mutation_and_no_naive_availability():
    rows = halt_resume()
    before = deepcopy(rows)
    retro(rows, "2023-06-20")
    assert rows == before
    with pytest.raises(ValueError, match="unverified availability"):
        name(available_time=datetime(2023, 1, 1, tzinfo=timezone.utc))
