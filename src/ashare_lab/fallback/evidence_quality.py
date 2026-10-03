"""Pure checks using verified reference observations, with explicit denominators."""

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, localcontext


OHLC = ("open", "high", "low", "close")


def locator(row):
    return {
        k: row[k]
        for k in (
            "provider",
            "query_id",
            "raw_path",
            "raw_sha256",
            "row_number",
            "raw_values_json",
            "volume_raw",
            "volume_source_unit",
            "volume_unit_basis",
            "amount_raw",
            "amount_source_unit",
            "numeric_display_exponents_json",
            "precision_basis",
        )
    }


def compare_prices(rows, reference_rows):
    """Compare every independent overlap, never choose one conflicting version."""
    index, identities = defaultdict(list), set()
    for row in reference_rows:
        key = (row["provider"], row["symbol"], row["event_date"])
        if key in identities:
            raise ValueError(
                "duplicate/conflicting price references require explicit version selection"
            )
        identities.add(key)
        if row["record_status"] == "invalid" or row["event_date"] is None:
            raise ValueError("invalid price reference")
        if row["within_requested_interval"]:
            index[(row["symbol"], row["event_date"])].append(row)
    comparisons, same_source, conflicts = [], [], []
    covered, full, covered_symbols = set(), set(), set()
    covered_fields = {field: set() for field in OHLC}
    selected = [r for r in rows if r["within_requested_interval"]]
    unknown_units = set()
    for row in selected:
        others = index.get((row["symbol"], row["event_date"]), [])
        independent = []
        for other in others:
            if other["provider"] == row["provider"]:
                same_source.append({"candidate": locator(row), "reference": locator(other)})
                continue
            if other["raw_sha256"] == row["raw_sha256"]:
                raise ValueError("same raw bytes cannot be independent price evidence")
            independent.append(other)
        if len({tuple(r[f] for f in OHLC) for r in independent}) > 1:
            conflicts.append(
                {
                    "symbol": row["symbol"],
                    "date": str(row["event_date"]),
                    "references": [locator(r) for r in independent],
                }
            )
        for other in independent:
            if row["record_status"] == "invalid":
                continue
            with localcontext() as context:
                context.prec = 50
                differences = {
                    f: str(row[f] - other[f])
                    for f in OHLC
                    if row[f] is not None and other[f] is not None
                }
                amount = (
                    str(row["amount_scaled_cny"] - other["amount_scaled_cny"])
                    if row["amount_scaled_cny"] is not None
                    and other["amount_scaled_cny"] is not None
                    else None
                )
                volume = (
                    str(row["volume_scaled_shares"] - other["volume_scaled_shares"])
                    if row["volume_scaled_shares"] is not None
                    and other["volume_scaled_shares"] is not None
                    else None
                )
            if not differences:
                continue
            key = (row["query_id"], row["row_number"])
            covered.add(key)
            for field in differences:
                covered_fields[field].add(key)
            covered_symbols.add(row["symbol"])
            if len(differences) == len(OHLC):
                full.add(key)
            for participant in (row, other):
                if participant["volume_scale_to_shares"] is None:
                    unknown_units.add((participant["provider"], participant["symbol"]))
            comparisons.append(
                {
                    "symbol": row["symbol"],
                    "date": str(row["event_date"]),
                    "candidate": locator(row),
                    "reference": locator(other),
                    "ohlc_differences": differences,
                    "display_amount_difference_cny": amount,
                    "display_volume_difference_shares": volume,
                    "volume_comparison_status": "unverified"
                    if volume is None
                    else "display_values_only",
                }
            )
    nonzero = sum(Decimal(v) != 0 for c in comparisons for v in c["ohlc_differences"].values())
    symbols = {r["symbol"] for r in rows}
    return {
        "comparisons": comparisons,
        "compared_pairs": len(comparisons),
        "compared_rows": len(covered),
        "compared_ohlc_values": sum(len(c["ohlc_differences"]) for c in comparisons),
        "compared_fields": dict(Counter(f for c in comparisons for f in c["ohlc_differences"])),
        "uncovered_field_rows": {
            f: len(selected) - len(keys) for f, keys in covered_fields.items()
        },
        "nonzero_differences": nonzero,
        "interval_rows": len(selected),
        "interval_rows_without_complete_ohlc_comparison": len(selected) - len(full),
        "covered_symbols": sorted(covered_symbols),
        "uncovered_symbols": sorted(symbols - covered_symbols),
        "same_provider_excluded": same_source,
        "reference_conflicts": conflicts,
        "unknown_volume_units": [{"provider": p, "symbol": s} for p, s in sorted(unknown_units)],
    }


def listing_index(evidence):
    index = {}
    for row in evidence["listing"]:
        if row["symbol"] in index:
            raise ValueError(
                "duplicate/conflicting listing references require explicit version selection"
            )
        index[row["symbol"]] = row
    return index


def with_listing_reference(reference, evidence):
    """Fill only a missing IPO reference; outDate and historical fields stay unknown."""
    index = listing_index(evidence)
    basics = {r["symbol"]: dict(r) for r in reference["securities"]}
    for symbol, row in index.items():
        if row.get("listed_date") is not None:
            basic = basics.setdefault(symbol, {"symbol": symbol, "source_out_date": None})
            if basic.get("ipo_date") is None:
                basic["ipo_date"] = date.fromisoformat(row["listed_date"])
    return {**reference, "securities": list(basics.values())}


def report_checks(evidence, records, rows, primary_reference):
    from .pipeline import convert

    index = listing_index(evidence)
    symbols = {r["symbol"] for r in records}
    present = {s for s in symbols if s in index and index[s].get("listed_date") is not None}
    violations = []
    for row in rows:
        ref = index.get(row["symbol"])
        if ref and ref.get("listed_date") and row["event_date"] is not None:
            if row["event_date"] < date.fromisoformat(ref["listed_date"]):
                violations.append(
                    {
                        "symbol": row["symbol"],
                        "date": str(row["event_date"]),
                        "candidate": locator(row),
                        "reference": ref,
                    }
                )
    conflicts = []
    for basic in primary_reference["securities"]:
        other = index.get(basic["symbol"])
        if (
            other
            and other.get("listed_date")
            and basic.get("ipo_date")
            and str(basic["ipo_date"]) != other["listed_date"]
        ):
            conflicts.append(
                {
                    "symbol": basic["symbol"],
                    "primary_ipo_date": str(basic["ipo_date"]),
                    "reference": other,
                }
            )
    reference_rows = []
    for record in evidence["price_captures"]:
        converted, issues = convert(
            {**record, "observed_at_utc": datetime.fromisoformat(record["observed_at_utc"])}
        )
        if issues:
            raise ValueError("invalid price reference conversion")
        reference_rows.extend(converted)
    compared = compare_prices(rows, reference_rows)
    unknown = compared["unknown_volume_units"]
    checks = [
        {
            "name": "ipo_reference",
            "status": "pass" if symbols and present == symbols else "unverified",
            "count": len(present),
            "candidate_symbols": len(symbols),
            "covered_symbols": sorted(present),
            "missing_symbols": sorted(symbols - present),
            "reference_universe_count": len(index),
            "observations": [index[s] for s in sorted(present)],
        },
        {
            "name": "ipo_reference_boundary",
            "status": "fail"
            if violations or conflicts
            else "pass"
            if present == symbols and symbols
            else "unverified",
            "count": len(violations),
            "violations": violations,
            "conflicts": conflicts,
            "meaning": "observed bars on/after reference IPO date; no delisting or PIT certification",
        },
        {
            "name": "delisting_boundary",
            "status": "unverified",
            "count": len(symbols),
            "symbols": sorted(symbols),
            "reason": "listing reference provides no delisting date or semantics",
        },
        {
            "name": "pit_availability",
            "status": "unverified",
            "count": len(symbols),
            "reason": "current observation time is known; historical availability and revision vintage remain unknown",
        },
        {
            "name": "cross_source_ohlc_overlap",
            "status": "fail"
            if compared["reference_conflicts"]
            else "unverified"
            if compared["nonzero_differences"]
            or not compared["compared_rows"]
            or compared["interval_rows_without_complete_ohlc_comparison"]
            else "pass",
            "count": compared["nonzero_differences"],
            **compared,
            "meaning": "only matching security/date rows within both requested intervals; difference = candidate minus reference",
        },
        {
            "name": "volume_unit_evidence",
            "status": "unverified",
            "count": len(unknown),
            "unknown_units_in_compared_rows": unknown,
            "reason": "unknown scales are not compared; known scales describe displayed quantities, not exact executions",
        },
    ]
    return checks
