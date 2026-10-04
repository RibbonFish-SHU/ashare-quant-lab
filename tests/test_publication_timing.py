"""Keep hypothetical historical date bounds distinct from current capture facts."""

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.publication_timing import PublicationEvidence, PublicationTimingError, diagnose


SH = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def local(day=8, hour=0, minute=0, second=0):
    return datetime(2023, 6, day, hour, minute, second, tzinfo=SH)


def evidence(**changes):
    return replace(
        PublicationEvidence(
            "synthetic-notice", "a" * 64, date(2023, 6, 8), datetime(2026, 10, 4, 8, tzinfo=UTC)
        ),
        **changes,
    )


def scenario(item=None, when=None):
    return diagnose(item or evidence(), mode="date_upper_bound", knowledge_time=when or local(9))


def observed(item=None, when=None):
    return diagnose(item or evidence(), mode="observed_at", knowledge_time=when or local(9))


def test_midnight_shanghai_is_previous_utc_date_at_1600():
    result = scenario()
    assert result["scenario_assumed_at_utc"] == "2023-06-08T16:00:00+00:00"
    assert result["scenario_basis_date"] == "2023-06-08"
    assert result["status"] == "visible" and result["visible_by_knowledge_time"] is True
    assert result["available_time"] is None and result["research_eligible"] is False


@pytest.mark.parametrize("offset,visible", [(-1, False), (0, True), (1, True)])
def test_scenario_inclusive_boundary_at_microsecond_precision(offset, visible):
    when = datetime(2023, 6, 8, 16, tzinfo=UTC) + timedelta(microseconds=offset)
    result = scenario(when=when)
    assert result["visible_by_knowledge_time"] is visible


@pytest.mark.parametrize("offset,visible", [(-1, False), (0, True), (1, True)])
def test_observed_inclusive_boundary_at_actual_capture_completion(offset, visible):
    capture = datetime(2026, 10, 4, 8, 17, 19, 654321, tzinfo=UTC)
    item = evidence(observed_at=capture)
    result = observed(item, capture + timedelta(microseconds=offset))
    assert result["visible_by_knowledge_time"] is visible
    assert result["observed_visible_at_utc"] == capture.isoformat()
    assert result["scenario_assumed_at_utc"] is None


@pytest.mark.parametrize(
    "day,next_midnight",
    [
        (date(2023, 1, 31), datetime(2023, 2, 1, tzinfo=SH)),
        (date(2023, 12, 31), datetime(2024, 1, 1, tzinfo=SH)),
        (date(2024, 2, 28), datetime(2024, 2, 29, tzinfo=SH)),
        (date(2024, 2, 29), datetime(2024, 3, 1, tzinfo=SH)),
        (date(2023, 2, 28), datetime(2023, 3, 1, tzinfo=SH)),
        (date(2023, 6, 9), datetime(2023, 6, 10, tzinfo=SH)),  # Friday -> Saturday, not Monday.
        (date(2023, 6, 10), datetime(2023, 6, 11, tzinfo=SH)),
    ],
)
def test_natural_day_boundaries_without_trading_calendar(day, next_midnight):
    result = scenario(evidence(publication_date=day), next_midnight)
    assert datetime.fromisoformat(result["scenario_assumed_at_utc"]) == next_midnight
    assert result["trading_calendar_applied"] is False


def test_evening_before_disclosure_document_date_is_legal():
    result = scenario(evidence(document_date=date(2023, 6, 7)))
    assert result["date_conflicts"] == [] and result["blocked_reasons"] == []
    assert result["scenario_basis_date"] == "2023-06-08"
    assert result["original_values"]["document_date"] == "2023-06-07"


def test_later_document_signature_does_not_silently_repair_publication_label():
    item = evidence(document_date=date(2023, 6, 9))
    result = scenario(item)
    assert result["blocked_reasons"] == ["document_date_after_publication_date"]
    assert result["scenario_assumed_at_utc"] is None
    assert result["status"] == "unavailable" and result["visible_by_knowledge_time"] is None
    captured = observed(item, item.observed_at)
    assert captured["visible_by_knowledge_time"] is True
    assert captured["date_conflicts"] == ["document_date_after_publication_date"]
    assert captured["blocked_reasons"] == []


def test_explicit_conflict_blocks_history_even_when_document_date_is_earlier():
    item = evidence(document_date=date(2022, 6, 7), date_conflict=True)
    assert scenario(item)["blocked_reasons"] == ["explicit_date_conflict"]
    result = observed(item, item.observed_at)
    assert result["visible_by_knowledge_time"] is True
    assert result["date_conflicts"] == ["explicit_date_conflict"]
    assert result["available_time"] is None and result["research_eligible"] is False


def test_late_source_version_never_backdates_new_bytes_to_original_publication():
    item = evidence(
        source_version_date=date(2023, 6, 12),
        source_version_date_basis="source_declared_version_date",
    )
    assert scenario(item, local(9))["visible_by_knowledge_time"] is False
    result = scenario(item, local(13))
    assert result["scenario_basis_date"] == "2023-06-12"
    assert datetime.fromisoformat(result["scenario_assumed_at_utc"]) == local(13)
    assert result["historical_revision_history_verified"] is False
    assert result["historical_revision_risk"]


def test_earlier_source_version_date_cannot_move_publication_assumption_earlier():
    item = evidence(
        source_version_date=date(2023, 6, 7),
        source_version_date_basis="source_declared_version_date",
    )
    assert scenario(item)["scenario_basis_date"] == "2023-06-08"


def test_source_reported_attachment_created_date_remains_an_explicit_assumption():
    item = evidence(
        source_version_date=date(2023, 6, 12),
        source_version_date_basis="source_reported_attachment_created_date_assumption",
    )
    result = scenario(item, local(13))
    assert result["scenario_basis_date"] == "2023-06-12"
    assert (
        "not a historical version publication date" in result["source_version_date_interpretation"]
    )
    assert result["original_values"]["source_version_date_basis"] == item.source_version_date_basis
    assert result["available_time"] is None and result["research_eligible"] is False


@pytest.mark.parametrize(
    "basis",
    [
        None,
        "filesystem_mtime",
        "pdf_creation_time",
        "filename_timestamp",
        "inferred_event_date",
        "observed_at",
        "",
        True,
    ],
)
def test_version_date_requires_an_approved_explicit_source_basis(basis):
    with pytest.raises(PublicationTimingError, match="explicit source basis"):
        evidence(source_version_date=date(2023, 6, 12), source_version_date_basis=basis)


def test_version_basis_without_date_is_not_treated_as_a_known_version():
    with pytest.raises(PublicationTimingError, match="without a date"):
        evidence(source_version_date_basis="source_declared_version_date")


def test_unknown_publication_cannot_fall_back_to_document_version_or_observation_date():
    item = evidence(
        publication_date=None,
        document_date=date(2023, 6, 7),
        source_version_date=date(2023, 6, 12),
        source_version_date_basis="source_declared_version_date",
    )
    result = scenario(item)
    assert result["blocked_reasons"] == ["publication_date_unknown"]
    assert result["scenario_assumed_at_utc"] is None and result["scenario_basis_date"] is None
    assert observed(item, item.observed_at)["visible_by_knowledge_time"] is True


def test_unknown_observation_never_uses_system_clock_or_publication():
    item = evidence(observed_at=None)
    result = observed(item)
    assert result["blocked_reasons"] == ["observed_at_unknown"]
    assert (
        result["normalized_observed_at_utc"] is None and result["observed_visible_at_utc"] is None
    )
    assert result["visible_by_knowledge_time"] is None
    assert (
        scenario(item)["visible_by_knowledge_time"] is True
    )  # Explicit date scenario still possible.


@pytest.mark.parametrize("field", ["publication_date", "source_version_date", "document_date"])
def test_future_source_dates_conflict_with_observation_but_do_not_erase_capture(field):
    changes = {field: date(2023, 6, 9), "observed_at": local(8, 23, 59, 59)}
    if field == "source_version_date":
        changes["source_version_date_basis"] = "source_declared_version_date"
    item = evidence(**changes)
    reason = field + "_after_observation_date"
    result = scenario(item)
    assert reason in result["date_conflicts"] and reason in result["blocked_reasons"]
    assert result["scenario_assumed_at_utc"] is None
    result = observed(item, item.observed_at)
    assert result["visible_by_knowledge_time"] is True
    assert reason in result["date_conflicts"] and result["blocked_reasons"] == []


def test_observation_date_comparison_uses_shanghai_not_utc_calendar_day():
    just_after_shanghai_midnight = datetime(2023, 6, 7, 16, tzinfo=UTC)
    assert scenario(evidence(observed_at=just_after_shanghai_midnight))["date_conflicts"] == []
    just_before = just_after_shanghai_midnight - timedelta(microseconds=1)
    assert scenario(evidence(observed_at=just_before))["date_conflicts"] == [
        "publication_date_after_observation_date"
    ]


def test_equivalent_timezones_give_equal_comparison_without_losing_original_values():
    capture = datetime(2023, 6, 8, 9, 15, tzinfo=SH)
    item = evidence(observed_at=capture)
    first = observed(item, capture)
    second = observed(item, capture.astimezone(timezone(timedelta(hours=-7))))
    assert first["knowledge_time_utc"] == second["knowledge_time_utc"]
    assert first["visible_by_knowledge_time"] is second["visible_by_knowledge_time"] is True
    assert first["original_values"]["observed_at"] == "2023-06-08T09:15:00+08:00"
    assert first["normalized_observed_at_utc"] == "2023-06-08T01:15:00+00:00"


@pytest.mark.parametrize("field", ["publication_date", "source_version_date", "document_date"])
@pytest.mark.parametrize("value", ["2023-02-30", "2023-06-08", local(8), 1686153600000, True])
def test_date_fields_reject_strings_timestamps_epochs_and_booleans(field, value):
    with pytest.raises(PublicationTimingError, match="must be a date or None"):
        evidence(**{field: value})


@pytest.mark.parametrize(
    "value", [datetime(2023, 6, 8), "2023-06-08T00:00:00Z", None, 1686153600, True]
)
def test_queries_require_actual_aware_datetime(value):
    with pytest.raises(PublicationTimingError, match="knowledge_time"):
        diagnose(evidence(), mode="date_upper_bound", knowledge_time=value)


@pytest.mark.parametrize("value", [datetime(2023, 6, 8), "2023-06-08T00:00:00Z", 1686153600, True])
def test_capture_time_requires_aware_datetime_when_known(value):
    with pytest.raises(PublicationTimingError, match="observed_at"):
        evidence(observed_at=value)


@pytest.mark.parametrize("mode", ["", "historical", "combined", None, True, []])
def test_modes_cannot_be_combined_or_silently_defaulted(mode):
    with pytest.raises(PublicationTimingError, match="mode"):
        diagnose(evidence(), mode=mode, knowledge_time=local(9))


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_id": ""},
        {"evidence_id": " padded "},
        {"evidence_id": 1},
        {"source_sha256": ""},
        {"source_sha256": "g" * 64},
        {"source_sha256": "a" * 63},
        {"date_conflict": 1},
        {"date_conflict": None},
        {"publication_label_kind": "exact_minute"},
    ],
)
def test_identity_hash_and_precision_declarations_are_validated(changes):
    with pytest.raises(PublicationTimingError):
        evidence(**changes)


def test_unverified_minute_label_cannot_advance_date_scenario_before_same_day_open():
    item = evidence(publication_label_kind="minute_label_unverified")
    for hour in (0, 9, 15, 23):
        assert scenario(item, local(8, hour))["visible_by_knowledge_time"] is False
    assert scenario(item, local(9))["visible_by_knowledge_time"] is True
    assert scenario(item)["original_values"]["publication_label_kind"] == "minute_label_unverified"


def test_same_day_resume_event_does_not_pull_assumption_back_to_event_open():
    result = scenario(evidence(document_date=date(2023, 6, 8)), local(8, 9, 30))
    assert result["visible_by_knowledge_time"] is False
    assert datetime.fromisoformat(result["scenario_assumed_at_utc"]) == local(9)


def test_views_never_substitute_capture_and_historical_date_assumption():
    item = evidence()
    historic = scenario(item, local(9))
    capture = observed(item, local(9))
    assert (
        historic["visible_by_knowledge_time"] is True
        and capture["visible_by_knowledge_time"] is False
    )
    assert historic["observed_visible_at_utc"] is None
    assert capture["scenario_assumed_at_utc"] is None and capture["scenario_basis_date"] is None
    for result in (historic, capture):
        assert result["diagnostic_only"] is True
        assert result["available_time"] is None and result["research_eligible"] is False
        assert result["historical_revision_history_verified"] is False


def test_missing_every_date_leaves_both_views_unavailable():
    item = evidence(publication_date=None, observed_at=None)
    assert scenario(item)["blocked_reasons"] == ["publication_date_unknown"]
    assert observed(item)["blocked_reasons"] == ["observed_at_unknown"]


def test_last_representable_date_is_recorded_as_unavailable_not_wrapped():
    item = evidence(publication_date=date.max, observed_at=None)
    result = scenario(item, datetime.max.replace(tzinfo=UTC))
    assert result["blocked_reasons"] == ["date_upper_bound_out_of_range"]
    assert result["scenario_assumed_at_utc"] is None


def test_input_is_not_mutated_and_result_is_json_serializable_and_independent():
    item = evidence()
    before = asdict(item)
    result = scenario(item)
    assert json.loads(json.dumps(result)) == result
    result["original_values"]["publication_date"] = "replaced-in-output-only"
    result["blocked_reasons"].append("changed")
    assert asdict(item) == before
    assert scenario(item)["blocked_reasons"] == []


def test_changed_frozen_input_is_revalidated_before_query():
    item = evidence()
    object.__setattr__(item, "publication_date", local(8))
    with pytest.raises(PublicationTimingError, match="must be a date or None"):
        scenario(item)
