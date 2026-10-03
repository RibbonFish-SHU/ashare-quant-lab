"""Candidate reconstruction and checks; no forward fills or trading qualification."""

from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_EVEN
import json

from .archive import FIELDS, OHLC, feature_value, float32_rounding_bound, parse_intervals
from .references import locator


def row_for_day(symbol, day, index, archive, context, references):
    values, locations = {}, {}
    for field in FIELDS:
        values[field], locations[field] = feature_value(archive["selected"][(symbol, field)], index)
    factor = values["factor"]
    if factor is not None and factor <= 0:
        raise ValueError(f"nonpositive factor: {symbol} {day}")
    for field in (*OHLC, "volume", "amount"):
        if values[field] is not None and (
            values[field] < 0 or (field in OHLC and values[field] == 0)
        ):
            raise ValueError(f"invalid {field}: {symbol} {day}")
    restored = {
        f: values[f] / factor if factor is not None and values[f] is not None else None
        for f in OHLC
    }
    basic = references["listings"].get(symbol)
    ipo = basic.get("ipo_date") if basic else None
    before_ipo = ipo is not None and day < ipo
    other = references["primary"].get((symbol, day))
    missing = any(v is None for v in restored.values())
    issues = []
    if before_ipo and any(v is not None for v in restored.values()):
        issues.append("price_before_ipo_reference")
    if not missing and not (
        restored["low"] <= restored["open"] <= restored["high"]
        and restored["low"] <= restored["close"] <= restored["high"]
    ):
        issues.append("inconsistent_ohlc")
    if before_ipo:
        status = "before_ipo_reference"
    elif missing and other and other.get("source_trading_active") is False:
        status = "missing_with_suspension_reference"
    elif missing:
        status = "missing_reason_unknown"
    else:
        status = "price_candidate"
    if issues:
        status = "invalid_candidate"
    bounds = {}
    for field in OHLC:
        if restored[field] is None:
            bounds[field] = None
        else:
            dx, df = float32_rounding_bound(values[field]), float32_rounding_bound(factor)
            bounds[field] = max(
                abs((values[field] + dx) / (factor - df) - restored[field]),
                abs((values[field] - dx) / (factor + df) - restored[field]),
            )
    listing_locator = (
        None
        if basic is None
        else {
            **locator(basic),
            "ipo_date": ipo,
            "provider": basic.get("provider", basic.get("source")),
            "sheet": basic.get("sheet"),
            "listing_date_cell": basic.get("listing_date_cell"),
        }
    )
    row = {
        "symbol": symbol,
        "event_date": day,
        "calendar_index": index,
        "archive_sha256": context["archive"]["sha256"],
        "source_repository": context["release"]["repository"],
        "source_version": context["release"]["tag"],
        "observed_at_utc": context["observed_at_utc"],
        "historical_publish_time": None,
        "available_time": None,
        "time_basis": "unknown_historical_vintage",
        "research_eligible": False,
        "requested_start": context["start"],
        "requested_end": context["end"],
        "record_status": status,
        "issues_json": json.dumps(issues),
        "ipo_date_reference": ipo,
        "listing_reference_json": json.dumps(listing_locator, default=str),
        "status_reference_json": json.dumps(locator(other), default=str) if other else None,
        "reference_trading_active": other.get("source_trading_active") if other else None,
        "delisted_date": None,
        "source_trading_active": None,
        "volume_source_unit": "unknown_normalized_upstream_quantity",
        "amount_source_unit": "unknown_upstream_quantity",
        "precision_basis": "float32_normalized_divided_by_float32_factor",
        "volume_shares": None,
        "amount_cny": None,
        "volume_is_exact_shares": False,
        "amount_is_exact_cny": False,
        "experimental_volume_shares": values["volume"] * factor * 100
        if factor is not None and values["volume"] is not None
        else None,
        "experimental_amount_cny": values["amount"] * 1000
        if values["amount"] is not None
        else None,
        "feature_locations_json": json.dumps(locations, sort_keys=True),
        **{f"raw_{f}": values[f] for f in FIELDS},
        **{f"restored_{f}": restored[f] for f in OHLC},
        **{f"float32_input_error_bound_{f}": bounds[f] for f in OHLC},
    }
    return row


def compare_row(row, other, group):
    result = {
        "symbol": row["symbol"],
        "event_date": row["event_date"],
        "group": group,
        "reference_provider": other.get("provider", other.get("source")),
        "reference_locator_json": json.dumps(locator(other), default=str),
        "archive_sha256": row["archive_sha256"],
        "calendar_index": row["calendar_index"],
    }
    for field in OHLC:
        value, reference = row[f"restored_{field}"], other.get(field)
        result[f"reference_{field}"] = None if reference is None else str(reference)
        result[f"restored_{field}"] = value
        result[f"delta_{field}"] = None
        result[f"equal_cent_{field}"] = None
        if value is not None and reference is not None:
            observed, expected = Decimal.from_float(value), Decimal(str(reference))
            if not expected.is_finite():
                raise ValueError("nonfinite reference price")
            result[f"delta_{field}"] = float(observed - expected)
            result[f"equal_cent_{field}"] = observed.quantize(
                Decimal("0.01"), ROUND_HALF_EVEN
            ) == expected.quantize(Decimal("0.01"), ROUND_HALF_EVEN)
    for field, reference_key in (("volume", "volume_shares"), ("amount", "amount_cny")):
        value = row[f"experimental_{field}_{'shares' if field == 'volume' else 'cny'}"]
        expected = other.get(
            reference_key,
            other.get("volume_scaled_shares" if field == "volume" else "amount_scaled_cny"),
        )
        result[f"experimental_{field}_delta"] = (
            float(Decimal.from_float(value) - Decimal(str(expected)))
            if value is not None and expected is not None
            else None
        )
    return result


def comparison_summary(comparisons):
    totals, per_symbol, per_field = Counter(), defaultdict(Counter), defaultdict(Counter)
    maximum = 0.0
    missing, mismatches = [], []
    for row in comparisons:
        totals["reference_rows"] += 1
        per_symbol[row["symbol"]]["reference_rows"] += 1
        absent, changed = [], []
        for field in OHLC:
            equal = row[f"equal_cent_{field}"]
            key = (
                "unavailable_values"
                if equal is None
                else "equal_after_cent_rounding"
                if equal
                else "cent_mismatches"
            )
            for counts in (totals, per_symbol[row["symbol"]], per_field[field]):
                counts[key] += 1
                if equal is not None:
                    counts["compared_values"] += 1
            if equal is None:
                absent.append(field)
            else:
                maximum = max(maximum, abs(row[f"delta_{field}"]))
                if not equal:
                    changed.append(field)
        detail = {
            "symbol": row["symbol"],
            "date": str(row["event_date"]),
            "reference_locator": json.loads(row["reference_locator_json"]),
        }
        if absent:
            missing.append({**detail, "fields": absent})
        if changed:
            mismatches.append({**detail, "fields": changed})
    return {
        "counts": dict(totals),
        "per_symbol": {k: dict(v) for k, v in per_symbol.items()},
        "per_field": {k: dict(v) for k, v in per_field.items()},
        "max_abs_price_error_cny": maximum,
        "missing_rows": missing,
        "mismatches": mismatches,
        "cent_rounding": "Decimal.from_float(recovered).quantize(0.01, ROUND_HALF_EVEN)",
        "independent_upstream_verified": False,
        "experimental_quantity_max_abs_deltas": {
            f: max(
                (
                    abs(r[f"experimental_{f}_delta"])
                    for r in comparisons
                    if r[f"experimental_{f}_delta"] is not None
                ),
                default=None,
            )
            for f in ("volume", "amount")
        },
    }


def membership_report(archive, conditional, latest, start, end):
    rows = parse_intervals(archive["texts"]["qlib_bin/instruments/csi300.txt"], latest)
    result = {
        "research_eligible": False,
        "formal_universe": False,
        "source": "community csi300 interval reference; Tushare snapshot lineage",
        "raw_interval_rows": len(rows),
        "derived_membership_written": False,
        "historical_available_time": None,
    }
    if conditional is None:
        return {**result, "status": "unverified", "reason": "no bound conditional event reference"}
    states = conditional["states"]
    if not states or [s["from"] for s in states] != sorted({s["from"] for s in states}):
        raise ValueError("conditional membership states unordered or duplicate")
    comparisons = []
    for day in archive["calendar"]:
        if not start <= day <= end:
            continue
        available = [s for s in states if s["from"] <= str(day)]
        if not available:
            raise ValueError("conditional membership baseline missing")
        matches = [r["symbol"] for r in rows if r["start"] <= day <= r["end"]]
        if len(set(matches)) != len(matches):
            raise ValueError("overlapping community membership intervals")
        expected = available[-1]["members"]
        if len(set(expected)) != len(expected):
            raise ValueError("duplicate conditional members")
        comparisons.append(
            {
                "date": str(day),
                "observed_count": len(matches),
                "conditional_count": len(expected),
                "conditional_state_from": available[-1]["from"],
                "missing": sorted(set(expected) - set(matches)),
                "extra": sorted(set(matches) - set(expected)),
            }
        )
    mismatches = [r for r in comparisons if r["missing"] or r["extra"]]
    return {
        **result,
        "status": "unverified",
        "assumptions": conditional["assumptions"],
        "reference_inputs": conditional["inputs"],
        "days_compared": len(comparisons),
        "baseline": comparisons[0] if comparisons else None,
        "equal_days": len(comparisons) - len(mismatches),
        "mismatch_days": mismatches,
        "mismatch_days_by_conditional_event": dict(
            Counter(r["conditional_state_from"] for r in mismatches)
        ),
    }
