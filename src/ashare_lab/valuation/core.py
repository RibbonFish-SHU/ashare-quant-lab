"""Synthetic NAV, with explicit price and uncredited-share assumptions.

This is not a historical-data completeness test, a real restricted-share fair
value model, or a replacement for the execution and corporate-action ledgers.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
import re
from zoneinfo import ZoneInfo

from ashare_lab.corporate_actions import ActionState, cash_claims, export_state, restore_state


SHANGHAI = ZoneInfo("Asia/Shanghai")
ZERO = Decimal("0")
SAME_UNADJUSTED_CLOSE = "same_security_unadjusted_close"
NUMERIC_DIGITS = 128


class ValuationInputError(ValueError):
    """A synthetic close value cannot be established from the explicit inputs."""


def _require(condition, message):
    if not condition:
        raise ValuationInputError(message)


def _day(value, name):
    _require(type(value) is date, f"{name} must be a date, not a timestamp/string")
    return value


def _instant(value, name):
    try:
        _require(
            isinstance(value, datetime) and value.utcoffset() is not None,
            f"{name} must be timezone aware",
        )
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ValuationInputError(f"invalid {name}: {exc}") from exc


def _shanghai(value, name):
    try:
        return value.astimezone(SHANGHAI)
    except (OverflowError, ValueError) as exc:
        raise ValuationInputError(f"{name} cannot be represented in Shanghai time") from exc


def _positive_decimal(value):
    _require(
        type(value) in {Decimal, str, int},
        "close price must be exact Decimal, text or int, not float/bool",
    )
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValuationInputError("invalid close price") from exc
    _require(result.is_finite() and result > ZERO, "close price must be finite and positive")
    return result


@dataclass(frozen=True)
class ValuationPrice:
    """An explicitly unadjusted synthetic close, with its actual knowledge time."""

    symbol: str
    event_date: date
    close_price: Decimal
    available_time: datetime
    adjustment_basis: str
    data_kind: str

    def __post_init__(self):
        _require(
            isinstance(self.symbol, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", self.symbol),
            "invalid price symbol",
        )
        _day(self.event_date, "price event date")
        _require(self.data_kind == "synthetic", "only explicit synthetic prices are accepted")
        _require(self.adjustment_basis == "unadjusted", "price basis must explicitly be unadjusted")
        object.__setattr__(self, "close_price", _positive_decimal(self.close_price))
        available = _instant(self.available_time, "price availability")
        local = _shanghai(available, "price availability")
        _require(
            local.date() > self.event_date
            or (local.date() == self.event_date and local.time() >= time(15)),
            "close price cannot be available before its Shanghai close",
        )
        object.__setattr__(self, "available_time", available)


def _snapshot(state):
    _require(isinstance(state, ActionState), "valuation requires an ActionState")
    try:
        # Public persistence APIs validate integrity, stage/account chains and
        # lifecycle invariants, while producing a detached copy for this read.
        payload = export_state(state)
        return restore_state(payload, expected_integrity=state.integrity)
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise ValuationInputError(f"invalid action state: {exc}") from exc


def _claims_by_security(state, on_date):
    stages = {(entry.namespace, entry.identity, entry.stage) for entry in state.journal}
    by_security = {}
    for right in state.entitlements:
        identity = right.action.action_id
        accrued = ("action", identity, "ex_accrual") in stages
        _require(
            right.action.ex_date > on_date or accrued,
            f"registered action {identity} is due but has no ex_accrual",
        )
        if not accrued:
            continue
        row = by_security.setdefault(
            right.action.symbol,
            {"gross_cash_receivable": ZERO, "withholding_payable": ZERO, "uncredited_shares": 0},
        )
        if right.action.cash and ("action", identity, "cash_payment") not in stages:
            row["gross_cash_receivable"] += right.gross_cash
            row["withholding_payable"] += right.tax
        if right.action.shares and ("action", identity, "share_credit") not in stages:
            row["uncredited_shares"] += right.new_shares
    return by_security


def _prices(prices, on_date, asof):
    _require(isinstance(prices, Mapping), "prices must be an explicit symbol mapping")
    result = {}
    for symbol, observation in prices.items():
        _require(isinstance(observation, ValuationPrice), "prices must contain ValuationPrice")
        observation = replace(observation)  # Revalidate even deliberately mutated frozen inputs.
        _require(symbol == observation.symbol, "price mapping key differs from price symbol")
        _require(observation.event_date == on_date, "price event date differs from valuation date")
        _require(observation.available_time <= asof, "price is not available at valuation asof")
        result[symbol] = observation
    return result


def _arithmetic_context(state, observations):
    _require(isinstance(state, ActionState), "valuation requires an ActionState")
    count = 0

    def inspect(item):
        nonlocal count
        if isinstance(item, Decimal):
            _require(item.is_finite(), "state/price is outside the supported numeric range")
            parts = item.as_tuple()
            _require(
                len(parts.digits) <= NUMERIC_DIGITS
                and -NUMERIC_DIGITS <= parts.exponent <= NUMERIC_DIGITS,
                "state/price is outside the supported numeric range",
            )
            count += 1
        elif type(item) is int:
            _require(
                -(10**NUMERIC_DIGITS) < item < 10**NUMERIC_DIGITS,
                "integer is outside the supported numeric range",
            )
            count += 1
        elif isinstance(item, Mapping):
            for value in item.values():
                inspect(value)
        elif isinstance(item, (list, tuple)):
            for value in item:
                inspect(value)

    inspect(asdict(state))
    for observation in observations.values():
        inspect(asdict(observation))
    # Bounded coefficients/exponents and integer quantities need at most 4N
    # digits across product/addition scales, plus carry digits for all inputs.
    # Margin also covers ledger cents quantization followed by withholding.
    # A fresh Context isolates precision, traps and flags from the caller.
    return Context(prec=4 * NUMERIC_DIGITS + 32 + len(str(count)))


def value_at_close(
    state,
    prices,
    *,
    on_date,
    asof,
    state_processed_through,
    corporate_action_scope_complete=False,
    uncredited_share_policy=None,
):
    """Value the supported synthetic scope without changing any caller-owned input.

    ``state_processed_through == asof`` is the caller's declaration that all
    relevant synthetic operations have been processed, not proof of historical
    data completeness. The last actual state operation may precede that time.
    ``corporate_action_scope_complete`` must be exactly True. A nonzero pending
    share balance additionally requires ``SAME_UNADJUSTED_CLOSE`` explicitly.

    The returned independent mapping retains Decimal amounts and date/datetime
    metadata; it is not a JSON serialization. Each security value excludes the
    account's unallocated spendable cash. Date-available shares indicate only
    lot locks, not market liquidity or permission to execute an order.
    """
    on_date = _day(on_date, "valuation date")
    asof = _instant(asof, "valuation asof")
    local = _shanghai(asof, "valuation asof")
    _require(local.date() == on_date, "valuation date differs from asof Shanghai date")
    _require(local.time() >= time(15), "valuation requires the 15:00 Shanghai close or later")
    processed = _instant(state_processed_through, "state_processed_through")
    _require(processed == asof, "state_processed_through must equal valuation asof")
    _require(
        corporate_action_scope_complete is True,
        "corporate-action scope must be explicitly declared complete for this synthetic scenario",
    )
    _require(
        uncredited_share_policy is None or uncredited_share_policy == SAME_UNADJUSTED_CLOSE,
        "unknown uncredited-share valuation policy",
    )
    observations = _prices(prices, on_date, asof)
    with localcontext(_arithmetic_context(state, observations)):
        return _value_snapshot(
            state, observations, on_date, asof, processed, uncredited_share_policy
        )


def _value_snapshot(state, observations, on_date, asof, processed, uncredited_share_policy):
    snapshot = _snapshot(state)
    _require(snapshot.clock <= asof, "state clock is in the valuation future")
    _require(on_date in snapshot.calendar.dates, "valuation date is absent from trading calendar")
    claims = _claims_by_security(snapshot, on_date)
    securities = {}
    for symbol in sorted(set(snapshot.account.lots) | set(claims)):
        claim = claims.get(symbol, {})
        credited = snapshot.account.total_shares(symbol)
        available = snapshot.account.available_shares(symbol, on_date)
        pending = claim.get("uncredited_shares", 0)
        _require(
            not pending or uncredited_share_policy == SAME_UNADJUSTED_CLOSE,
            f"uncredited shares for {symbol} require an explicit synthetic price assumption",
        )
        needs_price = credited > 0 or pending > 0
        _require(not needs_price or symbol in observations, f"missing close price for {symbol}")
        observation = observations[symbol] if needs_price else None
        close = observation.close_price if observation else ZERO
        gross = claim.get("gross_cash_receivable", ZERO)
        tax = claim.get("withholding_payable", ZERO)
        credited_value, pending_value = credited * close, pending * close
        securities[symbol] = {
            "credited_shares": credited,
            "date_available_shares": available,
            "locked_credited_shares": credited - available,
            "uncredited_shares": pending,
            "price": asdict(observation) if observation else None,
            "credited_shares_market_value": credited_value,
            "uncredited_shares_market_value": pending_value,
            "gross_cash_receivable": gross,
            "withholding_payable": tax,
            "net_cash_receivable": gross - tax,
            "value_excluding_spendable_cash": credited_value + pending_value + gross - tax,
        }
    totals = {
        name: sum((row[name] for row in securities.values()), ZERO)
        for name in (
            "credited_shares_market_value",
            "uncredited_shares_market_value",
            "gross_cash_receivable",
            "withholding_payable",
            "net_cash_receivable",
        )
    }
    # Independently reconcile the decomposition to the public ledger's claims.
    ledger_claims = cash_claims(snapshot)
    _require(
        all(
            totals[name] == ledger_claims[name]
            for name in ("gross_cash_receivable", "withholding_payable", "net_cash_receivable")
        )
        and {
            s: row["uncredited_shares"] for s, row in securities.items() if row["uncredited_shares"]
        }
        == {s: count for s, count in ledger_claims["uncredited_shares"].items() if count},
        "security decomposition differs from the corporate-action ledger",
    )
    return {
        "schema_version": "synthetic-close-valuation-v1",
        "data_kind": "synthetic",
        "research_eligible": False,
        "supported_synthetic_scope_valued": True,
        "complete_portfolio_valuation_available": False,
        "account_id": snapshot.account_id,
        "state_integrity": snapshot.integrity,
        "state_clock": snapshot.clock,
        "on_date": on_date,
        "asof": asof,
        "state_processed_through": processed,
        "corporate_action_scope_complete": True,
        "price_adjustment_basis": "unadjusted",
        "uncredited_share_policy": uncredited_share_policy,
        "spendable_cash": snapshot.account.cash,
        **totals,
        "nav": snapshot.account.cash
        + totals["credited_shares_market_value"]
        + totals["uncredited_shares_market_value"]
        + totals["net_cash_receivable"],
        "securities": securities,
    }
