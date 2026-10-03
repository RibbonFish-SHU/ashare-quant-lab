"""Quality checks for candidates; source success is distinct from research eligibility."""

from collections import Counter, defaultdict
from datetime import timedelta

from .candidates import FIELDS, KEYS, day


def date_range(start, end):
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def location(row):
    return {
        k: str(row[k]) if k in {"event_date", "query_date", "ex_date"} else row[k]
        for k in (
            "query_id",
            "raw_path",
            "raw_sha256",
            "raw_query_name",
            "row_number",
            "symbol",
            "event_date",
            "query_date",
            "ex_date",
        )
        if k in row
    }


def audit(tables, records, issues, *, acquisition_complete, events=()):
    checks = []

    def check(name, status, count, examples=(), **details):
        checks.append(
            {
                "name": name,
                "status": status,
                "count": count,
                "examples": list(examples)[:8],
                **details,
            }
        )

    check("acquisition", "pass" if acquisition_complete else "fail", len(records))
    check("typed_conversion", "fail" if issues else "pass", len(issues), issues)
    grouped = defaultdict(list)
    for kind, rows in tables.items():
        for row in rows:
            grouped[(kind, row["query_id"])].append(row)
    duplicates, conflicts = [], []
    for (kind, key), rows in grouped.items():
        counts = Counter(tuple(r.get(k) for k in KEYS[kind]) for r in rows)
        duplicates.extend(
            {"kind": kind, "query_id": key, "key": [str(x) for x in k], "rows": n}
            for k, n in counts.items()
            if n > 1
        )
    for kind, rows in tables.items():
        seen = {}
        for row in rows:
            key = tuple(row.get(k) for k in KEYS[kind])
            if key in seen:
                # Requests can overlap or omit optional fields. Only jointly
                # observed typed values can contradict another observation.
                other = seen[key]
                changed = [
                    f
                    for f, _ in FIELDS[kind]
                    if row.get(f) is not None and other.get(f) is not None and row[f] != other[f]
                ]
                if changed:
                    conflicts.append(
                        {
                            "kind": kind,
                            "fields": changed,
                            "left": location(other),
                            "right": location(row),
                        }
                    )
            else:
                seen[key] = row
    check(
        "duplicate_keys_within_response",
        "fail" if duplicates else "pass",
        len(duplicates),
        duplicates,
    )
    check("overlapping_observations", "fail" if conflicts else "pass", len(conflicts), conflicts)

    calendar = {}
    for row in tables.get("calendar", []):
        if row["event_date"] is not None:
            calendar.setdefault(row["event_date"], row["is_open"])
    missing_calendar, missing_bars, closed_bars = [], [], []
    basics = {row["symbol"]: row for row in tables.get("securities", [])}
    listing_violations, out_boundaries, unknown_basics = [], [], []
    for record in records:
        api, p = record["api"], record["parameters"]
        if api not in {"query_trade_dates", "query_history_k_data_plus"}:
            continue
        dates = set(date_range(day(p["start_date"]), day(p["end_date"])))
        absent = dates - calendar.keys()
        if absent:
            missing_calendar.append(
                {
                    "query_id": record["query_id"],
                    "missing_days": len(absent),
                    "first": str(min(absent)),
                    "last": str(max(absent)),
                }
            )
        if api == "query_trade_dates":
            continue
        rows = grouped[("bars", record["query_id"])]
        actual = {r["event_date"] for r in rows}
        basic = basics.get(rows[0]["symbol"]) if rows else None
        if not basic:
            unknown_basics.append({"query_id": record["query_id"], "code": p["code"]})
        expected = {
            d
            for d in dates
            if calendar.get(d) is True
            and (not basic or not basic["ipo_date"] or d >= basic["ipo_date"])
            and (not basic or not basic["source_out_date"] or d < basic["source_out_date"])
        }
        for d in sorted(expected - actual):
            missing_bars.append({"query_id": record["query_id"], "code": p["code"], "date": str(d)})
        for row in rows:
            d = row["event_date"]
            if calendar.get(d) is False:
                closed_bars.append(location(row))
            if basic and d:
                if basic["ipo_date"] and d < basic["ipo_date"]:
                    listing_violations.append(location(row))
                if basic["source_out_date"] and d >= basic["source_out_date"]:
                    out_boundaries.append(
                        {**location(row), "source_out_date": str(basic["source_out_date"])}
                    )
    check(
        "calendar_request_coverage",
        "unverified" if missing_calendar else "pass",
        len(missing_calendar),
        missing_calendar,
    )
    check(
        "bars_missing_open_days",
        "fail" if missing_bars else "unverified" if missing_calendar else "pass",
        len(missing_bars),
        missing_bars,
    )
    check("bars_on_closed_days", "fail" if closed_bars else "pass", len(closed_bars), closed_bars)
    check(
        "pre_ipo_rows",
        "fail" if listing_violations else "pass",
        len(listing_violations),
        listing_violations,
    )
    check(
        "listing_metadata_coverage",
        "unverified" if unknown_basics else "pass",
        len(unknown_basics),
        unknown_basics,
    )
    check(
        "out_date_semantics",
        "unverified",
        len(out_boundaries),
        out_boundaries,
        reason="source outDate has not been verified as first delisted natural day; not used to certify boundary rows",
    )

    bars = tables.get("bars", [])
    observations = Counter(r["trade_observation"] for r in bars)
    suspended = [location(r) for r in bars if r["source_trading_active"] is False]
    check(
        "suspension_observations",
        "unverified",
        len(suspended),
        suspended,
        classes=dict(observations),
        reason="flat prices, zero quantity and missing quantity stay distinct; execution unverified",
    )
    st = [location(r) for r in bars if r["source_is_st"] is True]
    check(
        "st_and_star_st_history",
        "unverified",
        len(st),
        st,
        reason="isST is a source binary flag; complete ST/*ST transitions and daily limit prices not established",
    )

    member_rows = tables.get("membership_snapshots", [])
    snapshots, shapes, stale = {}, [], []
    for (kind, key), rows in grouped.items():
        if kind != "membership_snapshots":
            continue
        codes = {r["symbol"] for r in rows if r["symbol"]}
        shapes.append(
            {
                "query_id": key,
                "query_date": str(rows[0]["query_date"]),
                "rows": len(rows),
                "unique_symbols": len(codes),
            }
        )
        snapshots[rows[0]["query_date"]] = codes
        if any(
            r["source_update_date"] and (r["query_date"] - r["source_update_date"]).days > 14
            for r in rows
        ):
            stale.append({**location(rows[0]), "update_date": str(rows[0]["source_update_date"])})
    member_requests = [r for r in records if r["api"] == "query_hs300_stocks"]
    bad_shape = [s for s in shapes if s["rows"] != 300 or s["unique_symbols"] != 300]
    empty_members = [r["query_id"] for r in member_requests if not r["rows"]]
    check(
        "membership_shape",
        "fail" if bad_shape or empty_members else "pass",
        len(bad_shape) + len(empty_members),
        bad_shape,
        queried_snapshots=len(member_requests),
        empty_query_ids=empty_members,
    )
    check(
        "membership_update_age",
        "unverified" if stale else "pass",
        len(stale),
        stale,
        reason="updateDate is neither announcement time nor proof of historical membership",
    )
    reconciliations = []
    events = sorted(events, key=lambda e: e["first_trading_date"])
    for n, event in enumerate(events):
        first = day(event["first_trading_date"])
        next_first = day(events[n + 1]["first_trading_date"]) if n + 1 < len(events) else None
        added, removed = set(event["added"]), set(event["removed"])
        for d, codes in sorted(snapshots.items()):
            if d < first or (next_first and d >= next_first):
                continue
            missing, retained = sorted(added - codes), sorted(removed & codes)
            reconciliations.append(
                {
                    "event_id": event["id"],
                    "query_date": str(d),
                    "check": "post_event_presence",
                    "status": "fail" if missing or retained else "pass",
                    "missing_added": missing,
                    "retained_removed": retained,
                }
            )
        before = snapshots.get(day(event["effective_after_close_date"]))
        after = snapshots.get(first)
        if before is not None and after is not None:
            actual_added, actual_removed = after - before, before - after
            reconciliations.append(
                {
                    "event_id": event["id"],
                    "check": "exact_event_delta",
                    "status": "pass"
                    if actual_added == added and actual_removed == removed
                    else "fail",
                    "actual_added": sorted(actual_added),
                    "actual_removed": sorted(actual_removed),
                    "expected_added": sorted(added),
                    "expected_removed": sorted(removed),
                }
            )
    failed = [x for x in reconciliations if x["status"] == "fail"]
    check(
        "official_event_reconciliation",
        "fail" if failed else "pass" if reconciliations else "unverified",
        len(failed),
        failed,
        comparisons=len(reconciliations),
        reconciliations=reconciliations,
        scope="selected announcements only; post-event presence is not a full 300-code baseline proof",
    )
    changes = []
    dates = sorted(d for d in snapshots if d.year == 2023)
    for before, after in zip(dates, dates[1:]):
        adds, removes = snapshots[after] - snapshots[before], snapshots[before] - snapshots[after]
        applicable = [e for e in events if before < day(e["first_trading_date"]) <= after]
        if not adds and not removes and not applicable:
            continue
        predicted = set(snapshots[before])
        for event in applicable:
            predicted.difference_update(event["removed"])
            predicted.update(event["added"])
        changes.append(
            {
                "previous_query_date": str(before),
                "query_date": str(after),
                "added": sorted(adds),
                "removed": sorted(removes),
                "event_ids": [e["id"] for e in applicable],
                "status": "pass" if applicable and predicted == snapshots[after] else "unverified",
            }
        )
    check(
        "weekly_membership_changes",
        "unverified",
        len(changes),
        changes,
        changes=changes,
        reason="weekly observations cannot exclude changes and reversals between observations; temporary events need official coverage",
    )
    dividends, factors = tables.get("dividends", []), tables.get("factors", [])
    tax_texts = [
        r
        for r in dividends
        if r["cash_after_tax_expression"]
        and not r["cash_after_tax_expression"].replace(".", "", 1).isdigit()
    ]
    check(
        "corporate_action_semantics",
        "unverified",
        len(dividends),
        [
            {**location(r), "after_tax_expression": r["cash_after_tax_expression"]}
            for r in tax_texts
        ],
        text_tax_rows=len(tax_texts),
        factor_rows=len(factors),
        reason="event dates and proposal/implementation stages retained; cumulative factors are not event split multipliers; algorithm and historical revisions unverified",
    )
    check(
        "historical_availability_and_revisions",
        "unverified",
        sum(map(len, tables.values())),
        reason="current captures lack historical vintage/publication timestamps; official change log confirms historical repairs",
    )
    return {
        "schema_version": "candidate-quality-v1",
        "research_eligible": False,
        "status": "fail" if any(c["status"] == "fail" for c in checks) else "unverified",
        "checks": checks,
        "rows_by_kind": {k: len(v) for k, v in tables.items()},
        "membership_rows": len(member_rows),
        "formal_research_gate": "blocked",
    }
