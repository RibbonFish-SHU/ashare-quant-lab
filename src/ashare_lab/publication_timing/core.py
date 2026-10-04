"""Separate assumed date-label visibility from observed capture visibility.

These diagnostics never certify historical availability or promote a candidate
to the existing research reader. No files, clocks, calendars or networks are read.
"""

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
MODES = frozenset({"date_upper_bound", "observed_at"})
VERSION_DATE_BASES = frozenset(
    {
        "source_declared_version_date",
        "source_reported_attachment_created_date_assumption",
    }
)


class PublicationTimingError(ValueError):
    """An input cannot represent the explicitly declared timing evidence."""


def _require(condition, message):
    if not condition:
        raise PublicationTimingError(message)


def _date_or_none(value, name):
    _require(
        value is None or type(value) is date,
        f"{name} must be a date or None, not a timestamp/epoch/string",
    )


def _instant(value, name):
    _require(
        isinstance(value, datetime) and value.utcoffset() is not None,
        f"{name} must be a timezone-aware datetime",
    )
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise PublicationTimingError(f"{name} is outside the supported UTC range") from exc


@dataclass(frozen=True)
class PublicationEvidence:
    evidence_id: str
    source_sha256: str
    publication_date: date | None
    observed_at: datetime | None
    source_version_date: date | None = None
    source_version_date_basis: str | None = None
    document_date: date | None = None
    date_conflict: bool = False
    publication_label_kind: str = "date_only"

    def __post_init__(self):
        _require(
            isinstance(self.evidence_id, str)
            and self.evidence_id.strip() == self.evidence_id
            and bool(self.evidence_id),
            "evidence_id must be a nonempty trimmed string",
        )
        _require(
            isinstance(self.source_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.source_sha256),
            "source_sha256 must be a lowercase SHA-256 digest",
        )
        for name in ("publication_date", "source_version_date", "document_date"):
            _date_or_none(getattr(self, name), name)
        if self.observed_at is not None:
            _instant(self.observed_at, "observed_at")
        _require(type(self.date_conflict) is bool, "date_conflict must be an explicit bool")
        _require(
            isinstance(self.publication_label_kind, str)
            and self.publication_label_kind in {"date_only", "minute_label_unverified"},
            "publication label cannot assert precise intraday release time",
        )
        if self.source_version_date is None:
            _require(
                self.source_version_date_basis is None, "version date basis supplied without a date"
            )
        else:
            _require(
                isinstance(self.source_version_date_basis, str)
                and self.source_version_date_basis in VERSION_DATE_BASES,
                "version date requires an explicit source basis; mtime/PDF/file-name times are not accepted",
            )


def _date_conflicts(evidence, observed):
    problems = []
    if evidence.date_conflict:
        problems.append("explicit_date_conflict")
    if (
        evidence.publication_date is not None
        and evidence.document_date is not None
        and evidence.document_date > evidence.publication_date
    ):
        problems.append("document_date_after_publication_date")
    if observed is not None:
        observation_day = observed.astimezone(SHANGHAI).date()
        for name in ("publication_date", "source_version_date", "document_date"):
            value = getattr(evidence, name)
            if value is not None and value > observation_day:
                problems.append(f"{name}_after_observation_date")
    return problems


def _original_values(evidence):
    return {
        key: value.isoformat() if isinstance(value, (date, datetime)) else value
        for key, value in asdict(evidence).items()
    }


def diagnose(evidence: PublicationEvidence, *, mode: str, knowledge_time: datetime) -> dict:
    """Query one explicit view with an inclusive, timezone-aware knowledge boundary.

    A blocked view has visible_by_knowledge_time=None, not a fabricated date.
    Publication date contradictions do not erase an independently supplied capture.
    """
    _require(isinstance(evidence, PublicationEvidence), "PublicationEvidence required")
    evidence = replace(evidence)  # Revalidate even deliberately changed frozen inputs.
    _require(
        isinstance(mode, str) and mode in MODES, "mode must be date_upper_bound or observed_at"
    )
    knowledge = _instant(knowledge_time, "knowledge_time")
    observed = (
        _instant(evidence.observed_at, "observed_at") if evidence.observed_at is not None else None
    )
    conflicts = _date_conflicts(evidence, observed)
    blocked = []
    point = None
    basis_date = None
    if mode == "date_upper_bound":
        blocked.extend(conflicts)
        if evidence.publication_date is None:
            blocked.append("publication_date_unknown")
        if not blocked:
            basis_date = max(
                evidence.publication_date, evidence.source_version_date or evidence.publication_date
            )
            try:
                point = datetime.combine(
                    basis_date + timedelta(days=1), time.min, tzinfo=SHANGHAI
                ).astimezone(timezone.utc)
            except (OverflowError, ValueError):
                blocked.append("date_upper_bound_out_of_range")
    elif observed is None:
        blocked.append("observed_at_unknown")
    else:
        point = observed
    visible = None if point is None else point <= knowledge
    status = "unavailable" if visible is None else "visible" if visible else "not_yet_visible"
    return {
        "schema_version": "publication-timing-diagnostic-v1",
        "evidence_id": evidence.evidence_id,
        "source_sha256": evidence.source_sha256,
        "mode": mode,
        "diagnostic_only": True,
        "available_time": None,
        "research_eligible": False,
        "original_values": _original_values(evidence),
        "knowledge_time_input": knowledge_time.isoformat(),
        "knowledge_time_utc": knowledge.isoformat(),
        "normalized_observed_at_utc": observed.isoformat() if observed is not None else None,
        "scenario_basis_date": basis_date.isoformat() if basis_date is not None else None,
        "scenario_assumed_at_utc": point.isoformat()
        if mode == "date_upper_bound" and point is not None
        else None,
        "observed_visible_at_utc": point.isoformat()
        if mode == "observed_at" and point is not None
        else None,
        "status": status,
        "visible_by_knowledge_time": visible,
        "comparison_includes_endpoint": True,
        "blocked_reasons": blocked,
        "date_conflicts": conflicts,
        "interpretation": (
            "scenario assumption: midnight in Shanghai on the natural day after the latest declared date bound; not certified historical availability"
            if mode == "date_upper_bound"
            else "retained bytes observed at capture completion; this is not a historical publication timestamp"
        ),
        "source_version_date_interpretation": (
            "source-reported attachment creation date used only as an assumed scenario lower bound; not a historical version publication date"
            if evidence.source_version_date_basis
            == "source_reported_attachment_created_date_assumption"
            else "caller-supplied source-declared version date; historical revision chain remains unverified"
            if evidence.source_version_date is not None
            else "source version date unknown"
        ),
        "historical_revision_history_verified": False,
        "historical_revision_risk": (
            "date labels and a supplied version bound do not prove when these exact bytes were historically accessible; no release history was authenticated"
        ),
        "trading_calendar_applied": False,
    }
