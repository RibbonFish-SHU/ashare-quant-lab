"""Pure event-time/knowledge-time queries, with no assumed clock or tradability."""

from collections import defaultdict
import hashlib
import re

from .common import day, instant, json_text, require

SESSIONS = {"date_only": 0, "morning_open": 1, "afternoon_open": 2, "close": 3}


def event(**fields):
    result = {
        "before_name": None,
        "after_name": None,
        "before_contains_st": None,
        "after_contains_st": None,
        "certified_st": None,
        "available_time": None,
        "availability_basis": "unknown",
        "source_timestamp": None,
        "source_timestamp_precision": "unknown",
        "signature_date": None,
        "signature_date_conflict": False,
        "research_eligible": False,
        **fields,
    }
    result["effective_date"] = day(result["effective_date"])
    if result.get("signature_date") is not None:
        result["signature_date"] = day(result["signature_date"])
    if result.get("observed_at_utc") is not None:
        result["observed_at_utc"] = instant(result["observed_at_utc"])
    if result["available_time"] is not None:
        result["available_time"] = instant(result["available_time"])
        require(result["availability_basis"] == "verified_timestamp", "unverified availability")
    result["event_id"] = hashlib.sha256(json_text(result).encode("utf-8")).hexdigest()
    validate_event(result)
    return result


def validate_event(row):
    require(re.fullmatch(r"\d{6}\.(SH|SZ)", row["symbol"]), "invalid event symbol")
    require(row["kind"] in {"name_change", "halt", "resume"}, "unsupported event kind")
    require(row["effective_session"] in SESSIONS, "invalid session anchor")
    require(
        row["research_eligible"] is False and row["certified_st"] is None,
        "reference event cannot certify trading or ST",
    )
    day(row["effective_date"])
    if row["kind"] == "name_change":
        require(row["effective_session"] == "date_only", "name change must retain date precision")
        require(bool(row["before_name"]) and bool(row["after_name"]), "empty name change")
        require(
            row["before_contains_st"] == ("ST" in row["before_name"].upper())
            and row["after_contains_st"] == ("ST" in row["after_name"].upper()),
            "ST string diagnostic differs from name",
        )
    else:
        require(
            row["effective_session"] in {"morning_open", "afternoon_open"},
            "halt/resume needs an explicit supported session anchor",
        )
    if row["available_time"] is not None:
        instant(row["available_time"])
        require(row["availability_basis"] == "verified_timestamp", "unverified availability")


def key(row):
    return day(row["effective_date"]), SESSIONS[row["effective_session"]]


def event_conflicts(events):
    """Keep conflicts visible; never resolve them using list ordering or future evidence."""
    by_slot, by_symbol, ids = defaultdict(list), defaultdict(list), set()
    issues = []
    for row in events:
        validate_event(row)
        if row["event_id"] in ids:
            issues.append(
                {
                    "kind": "duplicate_event_id",
                    "event_ids": [row["event_id"]],
                    "symbol": row["symbol"],
                }
            )
        ids.add(row["event_id"])
        category = "name" if row["kind"] == "name_change" else "trading"
        by_slot[(row["symbol"], category, *key(row))].append(row)
        if category == "name":
            by_symbol[row["symbol"]].append(row)
    for (symbol, category, _, _), rows in by_slot.items():
        if len(rows) > 1:
            values = {(r["kind"], r["before_name"], r["after_name"]) for r in rows}
            issues.append(
                {
                    "kind": "duplicate_event" if len(values) == 1 else "conflicting_event",
                    "category": category,
                    "symbol": symbol,
                    "event_ids": [r["event_id"] for r in rows],
                }
            )
    for symbol, rows in by_symbol.items():
        rows = sorted(rows, key=key)
        for left, right in zip(rows, rows[1:]):
            if key(left) < key(right) and left["after_name"] != right["before_name"]:
                issues.append(
                    {
                        "kind": "broken_name_chain",
                        "category": "name",
                        "symbol": symbol,
                        "event_ids": [left["event_id"], right["event_id"]],
                    }
                )
    return issues


def visible_events(events, knowledge_time, *, view="strict", policy="exclude_unknown"):
    require(view in {"strict", "retrospective"}, "unknown evidence view")
    require(policy in {"exclude_unknown", "observed_at"}, "unsupported availability policy")
    cutoff = instant(knowledge_time) if knowledge_time is not None else None
    require(view != "strict" or cutoff is not None, "strict view needs knowledge_time")
    selected = []
    for row in events:
        validate_event(row)
        if view == "retrospective":
            selected.append(row)
            continue
        available = row["available_time"]
        if available is None and policy == "observed_at":
            available = row.get("observed_at_utc")
        if available is not None and instant(available) <= cutoff:
            selected.append(row)
    return selected


def state_at(
    events,
    symbol,
    event_date,
    session,
    *,
    knowledge_time=None,
    view="strict",
    policy="exclude_unknown",
):
    require(session in {"morning_open", "afternoon_open", "close"}, "invalid query session")
    point = day(event_date), SESSIONS[session]
    relevant = [r for r in events if r["symbol"] == symbol and key(r) <= point]
    rows = visible_events(relevant, knowledge_time, view=view, policy=policy)
    conflicts = event_conflicts(rows)
    names = sorted((r for r in rows if r["kind"] == "name_change"), key=key)
    trading = sorted((r for r in rows if r["kind"] != "name_change"), key=key)
    name_bad = any(i.get("category") == "name" for i in conflicts)
    trade_bad = any(i.get("category") == "trading" for i in conflicts)
    name = names[-1] if names and not name_bad else None
    last = trading[-1] if trading and not trade_bad else None
    return {
        "symbol": symbol,
        "event_date": day(event_date),
        "session": session,
        "view": view,
        "availability_policy": policy,
        "knowledge_time": knowledge_time,
        "name_reference": name["after_name"] if name else None,
        "name_event_id": name["event_id"] if name else None,
        "name_contains_st_diagnostic": name["after_contains_st"] if name else None,
        "name_continuity_verified": False,
        "certified_st": None,
        "can_trade": None,
        "trading_reference": (
            "conflict"
            if trade_bad
            else "halt_reference"
            if last and last["kind"] == "halt"
            else "resume_reference"
            if last
            else "unknown"
        ),
        "trading_event_id": last["event_id"] if last else None,
        "visible_event_ids": [r["event_id"] for r in rows],
        "excluded_events": len(relevant) - len(rows),
        "conflicts": conflicts,
        "research_eligible": False,
    }


def daily_reference(
    events,
    symbol,
    event_date,
    *,
    is_open,
    volume_shares=None,
    knowledge_time=None,
    view="retrospective",
    policy="exclude_unknown",
):
    morning = state_at(
        events,
        symbol,
        event_date,
        "morning_open",
        knowledge_time=knowledge_time,
        view=view,
        policy=policy,
    )
    afternoon = state_at(
        events,
        symbol,
        event_date,
        "afternoon_open",
        knowledge_time=knowledge_time,
        view=view,
        policy=policy,
    )
    halted = [s["trading_reference"] == "halt_reference" for s in (morning, afternoon)]
    if not is_open:
        classification = "closed_calendar_day"
    elif any(s["trading_reference"] == "conflict" for s in (morning, afternoon)):
        classification = "conflicting_reference"
    elif all(halted):
        classification = "full_day_halt_reference"
    elif any(halted):
        classification = "partial_day_halt_reference"
    else:
        classification = "no_selected_halt_reference"
    return {
        "classification": classification,
        "morning": morning,
        "afternoon": afternoon,
        "positive_volume_conflict": (
            classification == "full_day_halt_reference"
            and volume_shares is not None
            and volume_shares > 0
        ),
        "research_eligible": False,
        "can_trade": None,
    }
