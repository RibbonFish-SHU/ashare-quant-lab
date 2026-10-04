"""Conservative, self-contained synthetic A-share order execution.

This module deliberately models only the execution boundary.  It does not turn
prices into a strategy return, infer a quote from a missing value, or apply
corporate actions.  Every public entry point requires an explicit synthetic
input and an ordered trading calendar.
"""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


MONEY = Decimal("0.01")
BPS = Decimal("10000")
ZERO = Decimal("0")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ExecutionInputError(ValueError):
    """Input violates an explicit execution or provenance rule."""


def _decimal(value, field_name: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ExecutionInputError(f"{field_name} cannot be bool")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ExecutionInputError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite() or (nonnegative and result < ZERO):
        raise ExecutionInputError(f"{field_name} must be a finite non-negative decimal")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _integer(value, field_name: str, *, minimum=0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ExecutionInputError(f"{field_name} must be an integer >= {minimum}, not bool")
    return value


def _aware(value, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ExecutionInputError(f"{field_name} must be a time zone aware datetime")
    return value.astimezone(timezone.utc)


def _close_boundary(on_date: date) -> datetime:
    return datetime.combine(on_date, time(15), tzinfo=SHANGHAI)


def _after_close(value, on_date: date, field_name: str) -> datetime:
    value = _aware(value, field_name)
    if value.astimezone(SHANGHAI).date() != on_date:
        raise ExecutionInputError(f"{field_name} date differs from Shanghai date {on_date}")
    if value < _close_boundary(on_date):
        raise ExecutionInputError(f"{field_name} must be at or after the 15:00 Shanghai close")
    return value


def _date(value, field_name: str) -> date:
    if isinstance(value, datetime):
        raise ExecutionInputError(f"{field_name} must be a date, not datetime")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionInputError(f"{field_name} must be an ISO date") from exc


def _symbol(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 9
        or value[6] != "."
        or value[7:]
        not in {
            "SH",
            "SZ",
        }
        or not (value[:6].isascii() and value[:6].isdigit())
    ):
        raise ExecutionInputError(f"invalid A-share symbol: {value!r}")
    return value


def _synthetic(data_kind: str, context: str) -> None:
    if data_kind != "synthetic":
        raise ExecutionInputError(f"{context} accepts data_kind=synthetic only")


@dataclass(frozen=True)
class ExecutionCalendar:
    """An explicit, unique and ordered list of open trading dates."""

    dates: tuple[date, ...]
    data_kind: str = "synthetic"

    def __post_init__(self):
        _synthetic(self.data_kind, "ExecutionCalendar")
        parsed = tuple(_date(d, "calendar date") for d in self.dates)
        if not parsed or parsed != tuple(sorted(set(parsed))):
            raise ExecutionInputError("calendar dates must be non-empty, unique and ascending")
        object.__setattr__(self, "dates", parsed)

    def index(self, value) -> int:
        value = _date(value, "calendar date")
        try:
            return self.dates.index(value)
        except ValueError as exc:
            raise ExecutionInputError(f"date {value} is absent from the trading calendar") from exc

    def next(self, value) -> date:
        index = self.index(value)
        if index + 1 >= len(self.dates):
            raise ExecutionInputError(f"no next trading date after {self.dates[index]}")
        return self.dates[index + 1]

    def pairs(self, start, end, *, interval: int = 5) -> tuple[tuple[date, date], ...]:
        start, end = _date(start, "start"), _date(end, "end")
        _integer(interval, "rebalance interval", minimum=1)
        first, last = self.index(start), self.index(end)
        if first > last:
            raise ExecutionInputError("rebalance start must precede end")
        return tuple(
            (self.dates[i], self.dates[i + 1])
            for i in range(first, last, interval)
            if i + 1 <= last
        )


@dataclass(frozen=True)
class TaxRate:
    effective_date: date
    sell_stamp_rate: Decimal

    def __post_init__(self):
        object.__setattr__(self, "effective_date", _date(self.effective_date, "tax effective date"))
        object.__setattr__(
            self,
            "sell_stamp_rate",
            _decimal(self.sell_stamp_rate, "sell_stamp_rate", nonnegative=True),
        )
        if self.sell_stamp_rate >= 1:
            raise ExecutionInputError("sell_stamp_rate must be below 1")


@dataclass(frozen=True)
class CostModel:
    """Explicit, date-selected costs.  No policy rate is hidden in the engine."""

    commission_rate: Decimal = Decimal("0.0003")
    commission_minimum: Decimal = Decimal("5")
    transfer_fee_rate: Decimal = Decimal("0")
    buy_slippage_bps: Decimal = Decimal("0")
    sell_slippage_bps: Decimal = Decimal("0")
    tax_schedule: tuple[TaxRate, ...] = (TaxRate(date(1900, 1, 1), Decimal("0.001")),)

    def __post_init__(self):
        for name in (
            "commission_rate",
            "commission_minimum",
            "transfer_fee_rate",
            "buy_slippage_bps",
            "sell_slippage_bps",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, nonnegative=True))
        if self.commission_rate >= 1 or self.transfer_fee_rate >= 1:
            raise ExecutionInputError("fee rates must be below 1")
        if self.buy_slippage_bps >= BPS or self.sell_slippage_bps >= BPS:
            raise ExecutionInputError("slippage must be below 10000 bps")
        schedule = tuple(self.tax_schedule)
        if (
            not schedule
            or not all(isinstance(item, TaxRate) for item in schedule)
            or tuple(sorted(schedule, key=lambda item: item.effective_date)) != schedule
        ):
            raise ExecutionInputError("tax schedule must be non-empty and date ordered")
        if len({item.effective_date for item in schedule}) != len(schedule):
            raise ExecutionInputError("tax schedule effective dates must be unique")
        object.__setattr__(self, "tax_schedule", schedule)

    def stamp_rate(self, on_date) -> Decimal:
        on_date = _date(on_date, "execution date")
        eligible = [item for item in self.tax_schedule if item.effective_date <= on_date]
        if not eligible:
            raise ExecutionInputError(f"no stamp-tax rate effective on {on_date}")
        return eligible[-1].sell_stamp_rate

    def fees(self, on_date, side: str, notional: Decimal) -> dict[str, Decimal]:
        on_date = _date(on_date, "fee date")
        if not isinstance(side, str) or side not in {"BUY", "SELL"}:
            raise ExecutionInputError("fee side must be BUY or SELL")
        notional = _money(_decimal(notional, "notional", nonnegative=True))
        if notional == 0:
            return {"commission": ZERO, "transfer_fee": ZERO, "stamp_tax": ZERO}
        commission = _money(max(notional * self.commission_rate, self.commission_minimum))
        transfer = _money(notional * self.transfer_fee_rate)
        stamp = _money(notional * self.stamp_rate(on_date)) if side == "SELL" else ZERO
        return {"commission": commission, "transfer_fee": transfer, "stamp_tax": stamp}


@dataclass(frozen=True)
class LotRule:
    """Order minima/steps; sell all available shares may include a final odd lot.

    Ordinary example: (100, 100, 100, sell_min_shares=100).
    STAR example: (200, 1, 1, sell_min_shares=200). No board is inferred.
    """

    buy_min_shares: int
    buy_step_shares: int
    sell_step_shares: int
    data_kind: str = "synthetic"
    sell_min_shares: int = 100

    def __post_init__(self):
        _synthetic(self.data_kind, "LotRule")
        for name in ("buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares"):
            _integer(getattr(self, name), name, minimum=1)

    def buy_quantity(self, requested: int) -> int:
        _integer(requested, "requested buy shares")
        if requested < self.buy_min_shares:
            return 0
        return (
            self.buy_min_shares
            + (requested - self.buy_min_shares) // self.buy_step_shares * self.buy_step_shares
        )

    def sell_quantity(self, requested: int, available: int) -> int:
        _integer(requested, "requested sell shares")
        _integer(available, "available sell shares")
        if requested <= 0 or available <= 0:
            return 0
        if requested >= available:
            return available  # legal odd-lot liquidation
        if requested < self.sell_min_shares:
            return 0
        return self.sell_min_shares + (
            (requested - self.sell_min_shares) // self.sell_step_shares * self.sell_step_shares
        )


@dataclass(frozen=True)
class HoldingLot:
    shares: int
    available_from: date
    unit_cost: Decimal

    def __post_init__(self):
        _integer(self.shares, "holding lot shares", minimum=1)
        object.__setattr__(self, "available_from", _date(self.available_from, "available_from"))
        object.__setattr__(
            self, "unit_cost", _money(_decimal(self.unit_cost, "unit_cost", nonnegative=True))
        )


@dataclass
class Account:
    cash: Decimal
    lots: dict[str, list[HoldingLot]] = field(default_factory=dict)
    data_kind: str = "synthetic"

    def __post_init__(self):
        _synthetic(self.data_kind, "Account")
        self.cash = _money(_decimal(self.cash, "cash", nonnegative=True))
        normalized: dict[str, list[HoldingLot]] = {}
        for symbol, rows in self.lots.items():
            symbol = _symbol(symbol)
            if not isinstance(rows, (list, tuple)) or not all(
                isinstance(lot, HoldingLot) for lot in rows
            ):
                raise ExecutionInputError("account lots must contain HoldingLot objects")
            if rows:
                normalized[symbol] = list(rows)
        self.lots = normalized

    def clone(self) -> "Account":
        return Account(
            self.cash,
            {symbol: list(rows) for symbol, rows in self.lots.items()},
            data_kind=self.data_kind,
        )

    def total_shares(self, symbol: str) -> int:
        return sum(lot.shares for lot in self.lots.get(_symbol(symbol), []))

    def available_shares(self, symbol: str, on_date) -> int:
        on_date = _date(on_date, "execution date")
        return sum(
            lot.shares
            for lot in self.lots.get(_symbol(symbol), [])
            if lot.available_from <= on_date
        )

    def add_buy(self, symbol: str, shares: int, available_from: date, unit_cost: Decimal) -> None:
        lot = HoldingLot(shares, available_from, unit_cost)
        self.lots.setdefault(_symbol(symbol), []).append(lot)

    def remove_sell(self, symbol: str, shares: int, on_date) -> None:
        symbol, on_date = _symbol(symbol), _date(on_date, "execution date")
        _integer(shares, "sell shares", minimum=1)
        remaining = shares
        result = []
        for lot in self.lots.get(symbol, []):
            if lot.available_from <= on_date and remaining:
                used = min(lot.shares, remaining)
                remaining -= used
                if lot.shares > used:
                    result.append(replace(lot, shares=lot.shares - used))
            else:
                result.append(lot)
        if remaining:
            raise ExecutionInputError("sell exceeds available T+1 shares")
        if result:
            self.lots[symbol] = result
        else:
            self.lots.pop(symbol, None)


@dataclass(frozen=True)
class MarketObservation:
    symbol: str
    event_date: date
    open_price: Decimal | None
    status: str = "unknown"  # open, suspended, unknown
    limit_up: Decimal | None = None
    limit_down: Decimal | None = None
    open_capacity_shares: int | None = None
    data_kind: str = "synthetic"
    no_price_limits: bool = False

    def __post_init__(self):
        _synthetic(self.data_kind, "MarketObservation")
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "event_date", _date(self.event_date, "market date"))
        if self.status not in {"open", "suspended", "unknown"}:
            raise ExecutionInputError("market status must be open, suspended or unknown")
        if self.open_price is not None:
            price = _decimal(self.open_price, "open_price", nonnegative=True)
            if price <= ZERO:
                raise ExecutionInputError("open_price must be positive")
            object.__setattr__(self, "open_price", price)
        for name in ("limit_up", "limit_down"):
            value = getattr(self, name)
            if value is not None:
                value = _decimal(value, name, nonnegative=True)
                if value <= ZERO:
                    raise ExecutionInputError(f"{name} must be positive")
                object.__setattr__(self, name, value)
        if type(self.no_price_limits) is not bool:
            raise ExecutionInputError("no_price_limits must be an explicit bool")
        if self.no_price_limits and (self.limit_up is not None or self.limit_down is not None):
            raise ExecutionInputError("no_price_limits conflicts with supplied daily limits")
        if (
            self.limit_up is not None
            and self.limit_down is not None
            and self.limit_down >= self.limit_up
        ):
            raise ExecutionInputError("limit_down must be below limit_up")
        if self.open_capacity_shares is not None:
            _integer(self.open_capacity_shares, "open capacity")


@dataclass(frozen=True)
class ActionGuard:
    event_date: date
    data_kind: str = "synthetic"
    known_no_actions: bool = False
    unresolved_symbols: tuple[str, ...] = ()

    def __post_init__(self):
        _synthetic(self.data_kind, "ActionGuard")
        object.__setattr__(self, "event_date", _date(self.event_date, "action date"))
        object.__setattr__(
            self, "unresolved_symbols", tuple(sorted({_symbol(s) for s in self.unresolved_symbols}))
        )
        if type(self.known_no_actions) is not bool:
            raise ExecutionInputError("known_no_actions must be an explicit bool")

    def permits(self, symbol: str) -> bool:
        return self.known_no_actions and _symbol(symbol) not in self.unresolved_symbols


@dataclass(frozen=True)
class Order:
    order_id: str
    symbol: str
    side: str
    shares: int
    submitted_date: date
    execute_date: date
    data_kind: str = "synthetic"

    def __post_init__(self):
        _synthetic(self.data_kind, "Order")
        if (
            not isinstance(self.order_id, str)
            or not self.order_id.strip()
            or self.order_id != self.order_id.strip()
        ):
            raise ExecutionInputError("order_id must be a non-empty, trimmed string")
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if not isinstance(self.side, str):
            raise ExecutionInputError("order side must be BUY or SELL")
        object.__setattr__(self, "side", self.side.upper())
        if self.side not in {"BUY", "SELL"}:
            raise ExecutionInputError("order side must be BUY or SELL")
        if not isinstance(self.shares, int) or isinstance(self.shares, bool) or self.shares <= 0:
            raise ExecutionInputError("order shares must be a positive integer")
        object.__setattr__(self, "submitted_date", _date(self.submitted_date, "submitted date"))
        object.__setattr__(self, "execute_date", _date(self.execute_date, "execute date"))
        if self.submitted_date >= self.execute_date:
            raise ExecutionInputError("order must be submitted before its execution date")


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    symbol: str
    side: str
    requested_shares: int
    filled_shares: int
    status: str
    reason: str | None
    execution_price: Decimal | None = None
    gross_notional: Decimal = ZERO
    commission: Decimal = ZERO
    transfer_fee: Decimal = ZERO
    stamp_tax: Decimal = ZERO
    cash_delta: Decimal = ZERO


@dataclass(frozen=True)
class ExecutionResult:
    account: Account
    orders: tuple[OrderResult, ...]
    ledger: tuple[dict, ...]
    event_date: date
    data_kind: str = "synthetic"

    @property
    def filled(self) -> tuple[OrderResult, ...]:
        return tuple(row for row in self.orders if row.filled_shares > 0)


@dataclass(frozen=True)
class CloseObservation:
    symbol: str
    event_date: date
    close_price: Decimal
    available_time: datetime
    data_kind: str = "synthetic"

    def __post_init__(self):
        _synthetic(self.data_kind, "CloseObservation")
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "event_date", _date(self.event_date, "close date"))
        object.__setattr__(self, "close_price", _decimal(self.close_price, "close price"))
        if self.close_price <= ZERO:
            raise ExecutionInputError("close price must be positive")
        available = _aware(self.available_time, "close available_time")
        if available < _close_boundary(self.event_date):
            raise ExecutionInputError(
                "close available_time cannot precede the 15:00 Shanghai close"
            )
        object.__setattr__(self, "available_time", available)


def _capacity(market: MarketObservation, participation_rate: Decimal) -> int:
    if market.open_capacity_shares is None:
        return 0
    return int(
        (Decimal(market.open_capacity_shares) * participation_rate).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )


def _validate_observations(observations, expected_type):
    for symbol, observation in observations.items():
        _symbol(symbol)
        if not isinstance(observation, expected_type) or observation.symbol != symbol:
            raise ExecutionInputError("observation mapping key and observation symbol differ")


def _result(order, status, reason, *, filled=0, **kwargs):
    return OrderResult(
        order.order_id, order.symbol, order.side, order.shares, filled, status, reason, **kwargs
    )


def _reject(order, reason):
    return _result(order, "rejected", reason)


def _fee_fields(fees: Mapping[str, Decimal]) -> dict[str, Decimal]:
    return {
        "commission": fees["commission"],
        "transfer_fee": fees["transfer_fee"],
        "stamp_tax": fees["stamp_tax"],
    }


def execute_orders(
    account: Account,
    orders: Sequence[Order],
    market: Mapping[str, MarketObservation],
    calendar: ExecutionCalendar,
    costs: CostModel,
    lot_rules: Mapping[str, LotRule],
    action_guard: ActionGuard,
    *,
    participation_rate: Decimal = Decimal("1"),
) -> ExecutionResult:
    """Execute one explicit open session, sells before buys, preserving every rejection."""
    if not isinstance(account, Account) or account.data_kind != "synthetic":
        raise ExecutionInputError("account must be a synthetic Account")
    if not all(isinstance(order, Order) for order in orders):
        raise ExecutionInputError("orders must contain Order objects")
    ids = [order.order_id for order in orders]
    if len(ids) != len(set(ids)) or any(not value.strip() for value in ids):
        raise ExecutionInputError("order_id must be unique and non-empty within the session")
    for order in orders:
        if calendar.next(order.submitted_date) != order.execute_date:
            raise ExecutionInputError(
                "execution date must be the next trading date after submission"
            )
    _validate_observations(market, MarketObservation)
    execute_dates = {order.execute_date for order in orders}
    if len(execute_dates) > 1:
        raise ExecutionInputError("one execution session must have one execute_date")
    if not execute_dates:
        raise ExecutionInputError("execution session requires at least one order")
    on_date = next(iter(execute_dates))
    calendar.index(on_date)
    if action_guard.event_date != on_date:
        raise ExecutionInputError("corporate-action guard date differs from execution date")
    participation_rate = _decimal(participation_rate, "participation_rate", nonnegative=True)
    if participation_rate > Decimal("1"):
        raise ExecutionInputError("participation_rate cannot exceed 1")
    current = account.clone()
    results, ledger = [], []
    ordered = sorted(enumerate(orders), key=lambda pair: (pair[1].side != "SELL", pair[0]))
    # No corporate-action implementation exists. A held or ordered unresolved symbol
    # blocks the session; do not value or trade around an unapplied cash/share event.
    relevant_symbols = set(current.lots) | {o.symbol for o in orders}
    if any(not action_guard.permits(symbol) for symbol in relevant_symbols):
        return ExecutionResult(
            current, tuple(_reject(o, "unresolved_company_action") for _, o in ordered), (), on_date
        )
    remaining_capacity = {
        symbol: _capacity(observation, participation_rate) for symbol, observation in market.items()
    }
    for _, order in ordered:
        if order.execute_date != on_date:
            raise ExecutionInputError("mixed execution dates")
        if not action_guard.permits(order.symbol):
            results.append(_reject(order, "unresolved_company_action"))
            continue
        observation = market.get(order.symbol)
        rule = lot_rules.get(order.symbol)
        if rule is None:
            results.append(_reject(order, "missing_lot_rule"))
            continue
        if not isinstance(rule, LotRule):
            raise ExecutionInputError("lot_rules must contain LotRule objects")
        if observation is None or observation.event_date != on_date:
            results.append(_reject(order, "unknown_market_status"))
            continue
        if observation.status == "unknown":
            results.append(_reject(order, "unknown_market_status"))
            continue
        if observation.status == "suspended":
            results.append(_reject(order, "suspended"))
            continue
        if observation.open_price is None:
            results.append(_reject(order, "missing_open_price"))
            continue
        if not observation.no_price_limits and (
            observation.limit_up is None or observation.limit_down is None
        ):
            results.append(_reject(order, "unknown_price_limits"))
            continue
        if order.side == "BUY":
            try:
                settlement_date = calendar.next(on_date)
            except ExecutionInputError:
                results.append(_reject(order, "missing_settlement_date"))
                continue
        slip = costs.sell_slippage_bps if order.side == "SELL" else costs.buy_slippage_bps
        factor = (BPS - slip) / BPS if order.side == "SELL" else (BPS + slip) / BPS
        execution_price = _money(observation.open_price * factor)
        if execution_price <= ZERO:
            results.append(_reject(order, "invalid_slippage_price"))
            continue
        if (
            order.side == "BUY"
            and observation.limit_up is not None
            and max(observation.open_price, execution_price) >= observation.limit_up
        ):
            results.append(_reject(order, "buy_at_limit_up"))
            continue
        if (
            order.side == "SELL"
            and observation.limit_down is not None
            and min(observation.open_price, execution_price) <= observation.limit_down
        ):
            results.append(_reject(order, "sell_at_limit_down"))
            continue
        if (
            observation.limit_down is not None
            and observation.open_price < observation.limit_down
            or observation.limit_up is not None
            and observation.open_price > observation.limit_up
        ):
            results.append(_reject(order, "open_price_outside_daily_limits"))
            continue
        capacity = remaining_capacity[order.symbol]
        if observation.open_capacity_shares is None:
            results.append(_reject(order, "unknown_open_capacity"))
            continue
        if capacity <= 0:
            results.append(_reject(order, "zero_open_capacity"))
            continue
        if order.side == "SELL":
            available = current.available_shares(order.symbol, on_date)
            requested = min(order.shares, capacity, available)
            fill_shares = rule.sell_quantity(requested, available)
            if fill_shares <= 0:
                results.append(
                    _reject(
                        order, "no_available_t1_shares" if available <= 0 else "below_sell_minimum"
                    )
                )
                continue
            available_before_fill = available
        else:
            requested = min(order.shares, capacity)
            fill_shares = rule.buy_quantity(requested)
            if fill_shares <= 0:
                results.append(_reject(order, "below_buy_minimum"))
                continue
        pre_cash_shares = fill_shares
        if order.side == "BUY":
            # Search the largest legal lot affordable with all explicit fees.
            max_k = (fill_shares - rule.buy_min_shares) // rule.buy_step_shares
            lo, hi = 0, max_k
            while lo < hi:
                mid = (lo + hi + 1) // 2
                candidate = rule.buy_min_shares + mid * rule.buy_step_shares
                notional = _money(execution_price * candidate)
                fees = costs.fees(on_date, order.side, notional)
                if notional + sum(fees.values()) <= current.cash:
                    lo = mid
                else:
                    hi = mid - 1
            fill_shares = rule.buy_min_shares + lo * rule.buy_step_shares if lo >= 0 else 0
            if (
                fill_shares <= 0
                or _money(execution_price * fill_shares)
                + sum(
                    costs.fees(on_date, order.side, _money(execution_price * fill_shares)).values()
                )
                > current.cash
            ):
                results.append(_reject(order, "insufficient_cash"))
                continue
        notional = _money(execution_price * fill_shares)
        fees = costs.fees(on_date, order.side, notional)
        total_fees = sum(fees.values(), ZERO)
        cash_before = current.cash
        shares_before = current.total_shares(order.symbol)
        if order.side == "SELL":
            cash_delta = notional - total_fees
            if current.cash + cash_delta < 0:
                results.append(_reject(order, "insufficient_cash_for_fees"))
                continue
            current.remove_sell(order.symbol, fill_shares, on_date)
            current.cash = _money(current.cash + cash_delta)
        else:
            current.cash = _money(current.cash - notional - total_fees)
            current.add_buy(order.symbol, fill_shares, settlement_date, execution_price)
            cash_delta = _money(-notional - total_fees)
        remaining_capacity[order.symbol] -= fill_shares
        status = "filled" if fill_shares == order.shares else "partial"
        if status == "filled":
            reason = None
        elif fill_shares < pre_cash_shares:
            reason = "partial_cash"
        elif order.side == "SELL" and available_before_fill < min(capacity, order.shares):
            reason = "partial_available_shares"
        elif capacity < order.shares:
            reason = "partial_capacity"
        else:
            reason = "partial_lot"
        result = _result(
            order,
            status,
            reason,
            filled=fill_shares,
            execution_price=execution_price,
            gross_notional=notional,
            **_fee_fields(fees),
            cash_delta=cash_delta,
        )
        results.append(result)
        ledger.append(
            {
                "order_id": order.order_id,
                "event_date": on_date,
                "symbol": order.symbol,
                "side": order.side,
                "shares": fill_shares,
                "price": execution_price,
                "cash_delta": cash_delta,
                "fees": fees,
                "cash_before": cash_before,
                "cash_after": current.cash,
                "shares_before": shares_before,
                "shares_after": current.total_shares(order.symbol),
                "available_shares_after": current.available_shares(order.symbol, on_date),
                "remaining_open_capacity": remaining_capacity[order.symbol],
                "data_kind": "synthetic",
            }
        )
    return ExecutionResult(current, tuple(results), tuple(ledger), on_date)


def build_equal_weight_orders(
    *,
    account: Account,
    universe: Sequence[str],
    close_observations: Mapping[str, CloseObservation],
    signal_date,
    signal_time: datetime,
    execution_date,
    calendar: ExecutionCalendar,
    lot_rules: Mapping[str, LotRule],
) -> tuple[Order, ...]:
    """Create next-open target orders from closes visible at an explicit signal time."""
    signal_date, execution_date = (
        _date(signal_date, "signal date"),
        _date(execution_date, "execution date"),
    )
    if calendar.next(signal_date) != execution_date:
        raise ExecutionInputError("execution date must be the next trading date after signal")
    signal_time = _after_close(signal_time, signal_date, "signal_time")
    _validate_observations(close_observations, CloseObservation)
    account = account.clone()
    symbols = tuple(dict.fromkeys(_symbol(s) for s in universe))
    if not symbols:
        raise ExecutionInputError("equal-weight universe cannot be empty")
    held_symbols = tuple(sorted(s for s in account.lots if account.total_shares(s) > 0))
    required = tuple(dict.fromkeys((*symbols, *held_symbols)))
    observations = {}
    for symbol in required:
        observation = close_observations.get(symbol)
        if observation is None or observation.event_date != signal_date:
            raise ExecutionInputError(f"missing signal close for {symbol}")
        if observation.available_time > signal_time:
            raise ExecutionInputError(f"future close observation for {symbol}")
        observations[symbol] = observation
    for symbol in required:
        if symbol not in lot_rules:
            raise ExecutionInputError(f"missing lot rule for {symbol}")
        if not isinstance(lot_rules[symbol], LotRule):
            raise ExecutionInputError("lot_rules must contain LotRule objects")
    nav = account.cash
    for symbol in held_symbols:
        nav += observations[symbol].close_price * account.total_shares(symbol)
    nav = _money(nav)
    weight = Decimal("1") / Decimal(len(symbols))
    orders = []
    for symbol in required:
        current = account.total_shares(symbol)
        target = 0
        if symbol in symbols:
            target = int(
                (nav * weight / observations[symbol].close_price).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
        delta = target - current
        if delta > 0:
            shares = lot_rules[symbol].buy_quantity(delta)
            if shares == 0:
                continue
            orders.append(
                Order(
                    f"ew-{signal_date}-{symbol}-buy",
                    symbol,
                    "BUY",
                    shares,
                    signal_date,
                    execution_date,
                )
            )
        elif delta < 0:
            shares = lot_rules[symbol].sell_quantity(
                -delta, account.available_shares(symbol, execution_date)
            )
            if shares == 0:
                continue
            orders.append(
                Order(
                    f"ew-{signal_date}-{symbol}-sell",
                    symbol,
                    "SELL",
                    shares,
                    signal_date,
                    execution_date,
                )
            )
    return tuple(orders)


def five_day_rebalance_pairs(
    calendar: ExecutionCalendar, start, end
) -> tuple[tuple[date, date], ...]:
    return calendar.pairs(start, end, interval=5)


def mark_to_market(
    account: Account,
    close_observations: Mapping[str, CloseObservation],
    on_date,
    *,
    asof: datetime,
) -> Decimal:
    """Return NAV only when every held position has an explicit visible close."""
    on_date = _date(on_date, "valuation date")
    asof = _after_close(asof, on_date, "valuation asof")
    _validate_observations(close_observations, CloseObservation)
    account = account.clone()
    nav = account.cash
    for symbol in sorted(account.lots):
        if account.total_shares(symbol) == 0:
            continue
        observation = close_observations.get(symbol)
        if observation is None or observation.event_date != on_date:
            raise ExecutionInputError(f"missing close for held position {symbol}")
        if observation.available_time > asof:
            raise ExecutionInputError(f"future close for held position {symbol}")
        nav += observation.close_price * account.total_shares(symbol)
    return _money(nav)
