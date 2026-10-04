"""Synthetic corporate-action rights and settlement; never a real tax or action adapter."""

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from ashare_lab.execution import (
    Account,
    ActionGuard,
    CostModel,
    ExecutionCalendar,
    HoldingLot,
    LotRule,
    MarketObservation,
    Order,
    execute_orders,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
ZERO = Decimal("0")
CENT = Decimal("0.01")
COST_POLICY = "original_lots_unchanged_new_shares_zero_cost"


class CorporateActionError(ValueError):
    """Synthetic rights or source/time/account identity cannot be established."""


def require(condition, message):
    if not condition:
        raise CorporateActionError(message)


def _kind(value):
    require(value == "synthetic", "only explicit synthetic input is accepted")


def _text(value, name):
    require(isinstance(value, str) and value.strip() == value and bool(value), f"invalid {name}")
    return value


def _symbol(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", value), "invalid symbol")
    return value


def _integer(value, name, minimum=0):
    require(type(value) is int and value >= minimum, f"{name} must be an integer >= {minimum}")
    return value


def _day(value, name):
    require(type(value) is date, f"{name} must be a date, not a timestamp/string")
    return value


def _instant(value, name):
    require(
        isinstance(value, datetime) and value.utcoffset() is not None,
        f"{name} must be timezone aware",
    )
    return value.astimezone(timezone.utc)


def _decimal(value, name):
    require(
        type(value) in {Decimal, str, int},
        f"{name} must be exact Decimal, text or int, not float/bool",
    )
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise CorporateActionError(f"invalid {name}") from exc
    require(result.is_finite() and result >= ZERO, f"{name} must be finite and nonnegative")
    return result


def _money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _json(value):
    def convert(item):
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(type(item).__name__)

    return json.dumps(
        value,
        default=convert,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CashDividend:
    gross_cash_per_eligible_share: Decimal
    payment_date: date
    tax_mode: str
    withholding_rate: Decimal
    data_kind: str
    ex_price_deduction_per_total_share: Decimal | None = None

    def __post_init__(self):
        _kind(self.data_kind)
        object.__setattr__(
            self,
            "gross_cash_per_eligible_share",
            _decimal(self.gross_cash_per_eligible_share, "gross cash"),
        )
        _day(self.payment_date, "payment date")
        rate = _decimal(self.withholding_rate, "withholding rate")
        require(rate <= 1, "withholding rate cannot exceed one")
        require(
            self.tax_mode in {"synthetic_no_tax", "synthetic_flat_withholding"},
            "unknown tax policy",
        )
        require(
            self.tax_mode != "synthetic_no_tax" or rate == ZERO,
            "no-tax policy conflicts with nonzero tax",
        )
        object.__setattr__(self, "withholding_rate", rate)
        if self.ex_price_deduction_per_total_share is not None:
            object.__setattr__(
                self,
                "ex_price_deduction_per_total_share",
                _decimal(self.ex_price_deduction_per_total_share, "ex-price deduction"),
            )


@dataclass(frozen=True)
class ShareDistribution:
    kind: str
    new_shares_per_old_share: Decimal
    credit_date: date
    listing_date: date
    explicitly_sellable_from: date
    allocation_policy: str
    cost_policy: str
    data_kind: str

    def __post_init__(self):
        _kind(self.data_kind)
        require(
            self.kind in {"stock_bonus", "capital_reserve_conversion"},
            "rights/splits/unknown share action unsupported",
        )
        ratio = _decimal(self.new_shares_per_old_share, "share ratio")
        require(ratio > ZERO, "share ratio must be positive")
        object.__setattr__(self, "new_shares_per_old_share", ratio)
        for name in ("credit_date", "listing_date", "explicitly_sellable_from"):
            _day(getattr(self, name), name)
        require(
            self.allocation_policy == "integer_only", "unknown or unsupported share rounding policy"
        )
        require(self.cost_policy == COST_POLICY, "unknown synthetic book cost policy")


@dataclass(frozen=True)
class CorporateAction:
    action_id: str
    version: int
    symbol: str
    record_date: date
    ex_date: date
    publish_time: datetime
    available_time: datetime
    stage: str
    certified: bool
    data_kind: str
    cash: CashDividend | None = None
    shares: ShareDistribution | None = None

    def __post_init__(self):
        _kind(self.data_kind)
        _text(self.action_id, "action ID")
        _integer(self.version, "action version", 1)
        _symbol(self.symbol)
        _day(self.record_date, "record date")
        _day(self.ex_date, "ex date")
        require(self.record_date < self.ex_date, "record date must precede ex date")
        publish = _instant(self.publish_time, "publication time")
        available = _instant(self.available_time, "availability")
        require(publish <= available, "availability precedes publication")
        object.__setattr__(self, "publish_time", publish)
        object.__setattr__(self, "available_time", available)
        require(
            self.stage == "implementation" and self.certified is True,
            "proposal or uncertified version rejected",
        )
        require(
            self.cash is None or isinstance(self.cash, CashDividend), "cash terms must be explicit"
        )
        require(
            self.shares is None or isinstance(self.shares, ShareDistribution),
            "share terms must be explicit",
        )
        require(
            self.cash is not None or self.shares is not None,
            "empty action is not proof of no actions",
        )
        if self.cash:
            require(self.cash.payment_date >= self.ex_date, "payment date precedes ex date")
        if self.shares:
            require(
                self.shares.credit_date >= self.ex_date
                and self.shares.listing_date >= self.ex_date
                and self.shares.explicitly_sellable_from >= self.ex_date,
                "share lifecycle precedes ex date",
            )


@dataclass(frozen=True)
class RecordSnapshot:
    snapshot_id: str
    account_id: str
    symbol: str
    record_date: date
    captured_at: datetime
    holdings: tuple[HoldingLot, ...]
    shares: int
    account_digest: str
    data_kind: str = "synthetic"


@dataclass(frozen=True)
class Entitlement:
    action: CorporateAction
    snapshot_id: str
    eligible_shares: int
    gross_cash: Decimal
    tax: Decimal
    net_cash: Decimal
    new_shares: int


@dataclass(frozen=True)
class JournalEntry:
    namespace: str
    identity: str
    stage: str
    content_digest: str
    applied_at: datetime
    cash_before: Decimal
    cash_after: Decimal
    cash_delta: Decimal
    gross_receivable_delta: Decimal
    tax_payable_delta: Decimal
    tax_remitted: Decimal
    symbol: str | None
    shares_delta: int
    account_before_digest: str
    account_after_digest: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ActionState:
    account_id: str
    account: Account
    calendar: ExecutionCalendar
    clock: datetime
    snapshots: tuple[RecordSnapshot, ...]
    entitlements: tuple[Entitlement, ...]
    journal: tuple[JournalEntry, ...]
    integrity: str
    data_kind: str = "synthetic"


@dataclass(frozen=True)
class ActionStep:
    action: CorporateAction
    snapshot_id: str
    stage: str
    at: datetime

    def __post_init__(self):
        require(
            isinstance(self.action, CorporateAction), "step requires certified synthetic action"
        )
        _text(self.snapshot_id, "snapshot ID")
        require(
            self.stage in {"register", "ex_accrual", "cash_payment", "share_credit"},
            "unsupported action stage",
        )
        object.__setattr__(self, "at", _instant(self.at, "operation time"))


def _state_payload(state):
    result = asdict(state)
    result.pop("integrity")
    return result


def _seal(state, **changes):
    account = changes.pop("account", state.account).clone()
    result = replace(state, account=account, **changes)
    return replace(result, integrity=_digest(_state_payload(result)))


def _validated(state):
    require(isinstance(state, ActionState), "expected ActionState")
    _kind(state.data_kind)
    require(
        _digest(_state_payload(state)) == state.integrity,
        "state/account/snapshot identity changed outside lifecycle API",
    )
    _kind(state.account.data_kind)
    require(
        isinstance(state.calendar, ExecutionCalendar) and state.calendar.data_kind == "synthetic",
        "calendar provenance differs",
    )
    state.account.clone()
    return state


def create_state(account_id, account, calendar, *, asof, data_kind):
    _kind(data_kind)
    _text(account_id, "account ID")
    require(
        isinstance(account, Account) and account.data_kind == "synthetic",
        "synthetic Account required",
    )
    require(
        isinstance(calendar, ExecutionCalendar) and calendar.data_kind == "synthetic",
        "synthetic calendar required",
    )
    state = ActionState(
        account_id, account.clone(), calendar, _instant(asof, "initial clock"), (), (), (), ""
    )
    return _seal(state)


def capture_record_close(state, snapshot_id, symbol, record_date, *, at):
    _validated(state)
    _text(snapshot_id, "snapshot ID")
    _symbol(symbol)
    _day(record_date, "snapshot record date")
    at = _instant(at, "snapshot time")
    state.calendar.index(record_date)
    local = at.astimezone(SHANGHAI)
    require(
        local.date() == record_date and local.time() >= time(15),
        "record snapshot requires same-day Shanghai close",
    )
    existing = next((s for s in state.snapshots if s.snapshot_id == snapshot_id), None)
    if existing:
        require(
            (existing.account_id, existing.symbol, existing.record_date, existing.captured_at)
            == (state.account_id, symbol, record_date, at),
            "snapshot ID reused for a different identity/time",
        )
        return _seal(state)
    require(at >= state.clock, "historical record snapshot/backfill rejected")
    require(
        not any(
            right.action.symbol == symbol
            and right.new_shares > 0
            and not _seen(state, "action", right.action.action_id, "share_credit")
            for right in state.entitlements
        ),
        "overlapping record snapshot with uncredited share entitlement is unsupported",
    )
    require(
        not any(s.symbol == symbol and s.record_date == record_date for s in state.snapshots),
        "record-date holdings already frozen under another snapshot ID",
    )
    holdings = tuple(state.account.lots.get(symbol, ()))
    snapshot = RecordSnapshot(
        snapshot_id,
        state.account_id,
        symbol,
        record_date,
        at,
        holdings,
        sum(lot.shares for lot in holdings),
        _digest(asdict(state.account)),
    )
    return _seal(state, clock=at, snapshots=(*state.snapshots, snapshot))


def _seen(state, namespace, identity, stage):
    return next(
        (
            j
            for j in state.journal
            if (j.namespace, j.identity, j.stage) == (namespace, identity, stage)
        ),
        None,
    )


def cash_claims(state):
    """Gross/net receivables are distinct from spendable cash; not portfolio NAV."""
    _validated(state)
    gross = tax = ZERO
    pending = {}
    for right in state.entitlements:
        identity = right.action.action_id
        if not _seen(state, "action", identity, "ex_accrual"):
            continue
        if right.action.cash and not _seen(state, "action", identity, "cash_payment"):
            gross += right.gross_cash
            tax += right.tax
        if right.action.shares and not _seen(state, "action", identity, "share_credit"):
            pending[right.action.symbol] = pending.get(right.action.symbol, 0) + right.new_shares
    return {
        "spendable_cash": state.account.cash,
        "gross_cash_receivable": gross,
        "withholding_payable": tax,
        "net_cash_receivable": gross - tax,
        "uncredited_shares": pending,
        "complete_portfolio_valuation_available": False,
    }


def _journal(
    state,
    *,
    account,
    namespace,
    identity,
    stage,
    digest,
    at,
    symbol=None,
    gross_delta=ZERO,
    tax_delta=ZERO,
    remitted=ZERO,
    shares_delta=0,
    notes=(),
):
    return JournalEntry(
        namespace,
        identity,
        stage,
        digest,
        at,
        state.account.cash,
        account.cash,
        account.cash - state.account.cash,
        gross_delta,
        tax_delta,
        remitted,
        symbol,
        shares_delta,
        _digest(asdict(state.account)),
        _digest(asdict(account)),
        tuple(notes),
    )


def _apply(state, step):
    require(isinstance(step, ActionStep), "batch contains a non-ActionStep")
    step = replace(step)
    action, at = step.action, step.at
    # Constructor validation is repeated to catch deliberately mutated nested inputs.
    action = replace(
        action,
        cash=replace(action.cash) if action.cash else None,
        shares=replace(action.shares) if action.shares else None,
    )
    require(action.available_time <= at, "action availability is after operation time")
    digest = _digest({"action": asdict(action), "snapshot_id": step.snapshot_id})
    previous = _seen(state, "action", action.action_id, step.stage)
    if previous:
        require(previous.content_digest == digest, "processed action ID/version/content conflict")
        require(at >= previous.applied_at, "replay predates the original processing time")
        return _seal(state)  # Preserve the later state clock and the original journal.
    require(at >= state.clock, "historical operation/backfill rejected")
    state.calendar.index(action.record_date)
    state.calendar.index(action.ex_date)
    snapshot = next((s for s in state.snapshots if s.snapshot_id == step.snapshot_id), None)
    require(snapshot is not None, "record snapshot is absent from this account state")
    require(
        (snapshot.account_id, snapshot.symbol, snapshot.record_date)
        == (state.account_id, action.symbol, action.record_date),
        "snapshot account/security/date mismatch",
    )
    require(
        snapshot.captured_at <= at and action.available_time <= snapshot.captured_at,
        "future action or snapshot cannot establish record-date entitlement",
    )
    right = next((e for e in state.entitlements if e.action.action_id == action.action_id), None)
    if right:
        require(
            _digest({"action": asdict(right.action), "snapshot_id": right.snapshot_id}) == digest,
            "registered action ID/version/content conflict",
        )
    local_day = at.astimezone(SHANGHAI).date()
    account = state.account.clone()
    gross_delta = tax_delta = remitted = ZERO
    shares_delta = 0
    notes = []
    entitlements = state.entitlements
    if step.stage == "register":
        require(
            local_day == action.record_date and right is None,
            "registration must occur on record day exactly once",
        )
        require(
            not any(
                e.action.symbol == action.symbol
                and e.action.record_date == action.record_date
                and e.action.ex_date == action.ex_date
                for e in entitlements
            ),
            "same issuer/record/ex event already registered under another ID",
        )
        gross = (
            _money(snapshot.shares * action.cash.gross_cash_per_eligible_share)
            if action.cash
            else ZERO
        )
        tax = _money(gross * action.cash.withholding_rate) if action.cash else ZERO
        quantity = (
            snapshot.shares * action.shares.new_shares_per_old_share if action.shares else ZERO
        )
        require(
            quantity == quantity.to_integral_value(),
            "fractional entitlement rejected by integer_only allocation",
        )
        right = Entitlement(
            action, step.snapshot_id, snapshot.shares, gross, tax, gross - tax, int(quantity)
        )
        entitlements = (*entitlements, right)
        notes.append(
            "Entitlement comes only from the frozen record-close total holdings, including T+1-locked lots."
        )
    else:
        require(right is not None, "action is not registered")
        if step.stage == "ex_accrual":
            require(
                local_day == action.ex_date,
                "ex accrual requires its actual ex date; no historical fill",
            )
            gross_delta, tax_delta = right.gross_cash, right.tax
            notes.append("Receivables accrued; no spendable cash or tradable shares are credited.")
        else:
            require(
                _seen(state, "action", action.action_id, "ex_accrual"),
                "ex accrual must precede settlement",
            )
            if step.stage == "cash_payment":
                require(action.cash is not None, "action has no cash distribution")
                require(
                    local_day >= action.cash.payment_date,
                    "cash receipt is before declared payment date",
                )
                account.cash = _money(account.cash + right.net_cash)
                gross_delta, tax_delta, remitted = -right.gross_cash, -right.tax, right.tax
                require(
                    right.gross_cash == right.net_cash + remitted,
                    "cash and tax do not conserve gross entitlement",
                )
                notes.extend(
                    [
                        "Actual receipt time is the applied_at timestamp; delayed cash is not backdated.",
                        action.cash.tax_mode
                        + ": explicit synthetic assumption, not A-share holding-period taxation.",
                    ]
                )
            else:
                require(action.shares is not None, "action has no share distribution")
                require(
                    local_day >= action.shares.credit_date,
                    "share receipt is before declared credit date",
                )
                available = max(
                    local_day, action.shares.listing_date, action.shares.explicitly_sellable_from
                )
                state.calendar.index(available)
                before_cost = sum(
                    (lot.shares * lot.unit_cost for lots in account.lots.values() for lot in lots),
                    ZERO,
                )
                shares_delta = right.new_shares
                if shares_delta:
                    account.add_buy(action.symbol, shares_delta, available, ZERO)
                after_cost = sum(
                    (lot.shares * lot.unit_cost for lots in account.lots.values() for lot in lots),
                    ZERO,
                )
                require(after_cost == before_cost, "synthetic aggregate acquisition cost changed")
                notes.extend(
                    [
                        COST_POLICY,
                        "Unit costs are a synthetic bookkeeping convention, not a real tax basis.",
                    ]
                )
    require(account.cash >= ZERO, "cash cannot become negative")
    entry = _journal(
        state,
        account=account,
        namespace="action",
        identity=action.action_id,
        stage=step.stage,
        digest=digest,
        at=at,
        symbol=action.symbol,
        gross_delta=gross_delta,
        tax_delta=tax_delta,
        remitted=remitted,
        shares_delta=shares_delta,
        notes=notes,
    )
    return _seal(
        state, account=account, clock=at, entitlements=entitlements, journal=(*state.journal, entry)
    )


def apply_actions(state, steps):
    """Apply an ordered batch atomically to copies; exceptions leave the input untouched."""
    _validated(state)
    require(
        isinstance(steps, (tuple, list)) and bool(steps), "nonempty explicit action batch required"
    )
    working = _seal(state)
    for step in steps:
        working = _apply(working, step)
    return working


def execute_session(
    state,
    session_id,
    *,
    at,
    orders,
    market,
    costs,
    lot_rules,
    action_guard,
    participation_rate=Decimal("1"),
):
    """Thin synthetic open-session composition; the caller must supply an ActionGuard."""
    _validated(state)
    _text(session_id, "session ID")
    at = _instant(at, "execution time")
    local = at.astimezone(SHANGHAI)
    require(local.time() == time(9, 30), "synthetic execution session must be Shanghai 09:30")
    require(
        isinstance(orders, (list, tuple)) and bool(orders),
        "explicit nonempty order sequence required",
    )
    require(all(isinstance(o, Order) for o in orders), "orders must contain Order objects")
    require(isinstance(action_guard, ActionGuard), "explicit ActionGuard required")
    require(isinstance(costs, CostModel), "explicit CostModel required")
    require(
        isinstance(market, dict) and all(isinstance(v, MarketObservation) for v in market.values()),
        "explicit market mapping required",
    )
    require(
        isinstance(lot_rules, dict) and all(isinstance(v, LotRule) for v in lot_rules.values()),
        "explicit lot rule mapping required",
    )
    orders = tuple(replace(o) for o in orders)
    market = {k: replace(v) for k, v in market.items()}
    lot_rules = {k: replace(v) for k, v in lot_rules.items()}
    action_guard = replace(action_guard)
    costs = replace(costs, tax_schedule=tuple(replace(r) for r in costs.tax_schedule))
    participation_rate = _decimal(participation_rate, "participation rate")
    require(participation_rate <= 1, "participation rate cannot exceed one")
    payload = {
        "orders": [asdict(o) for o in orders],
        "market": {k: asdict(v) for k, v in market.items()},
        "costs": asdict(costs),
        "lot_rules": {k: asdict(v) for k, v in lot_rules.items()},
        "guard": asdict(action_guard),
        "participation_rate": participation_rate,
        "at": at,
    }
    digest = _digest(payload)
    previous = _seen(state, "execution", session_id, "open")
    if previous:
        require(
            previous.content_digest == digest, "execution session ID reused with different input"
        )
        return _seal(state)
    require(at >= state.clock, "historical execution rejected")
    require(
        all(o.execute_date == local.date() for o in orders),
        "execution timestamp/order dates differ",
    )
    require(
        not any(j.namespace == "execution" and j.applied_at == at for j in state.journal),
        "open session already processed under another ID",
    )
    require(
        not any(
            e.action.ex_date <= local.date()
            and not _seen(state, "action", e.action.action_id, "ex_accrual")
            for e in state.entitlements
        ),
        "unapplied ex accrual blocks later trading",
    )
    result = execute_orders(
        state.account,
        orders,
        market,
        state.calendar,
        costs,
        lot_rules,
        action_guard,
        participation_rate=participation_rate,
    )
    entry = _journal(
        state,
        account=result.account,
        namespace="execution",
        identity=session_id,
        stage="open",
        digest=digest,
        at=at,
        notes=(
            _json({"orders": [asdict(o) for o in result.orders], "ledger": result.ledger}),
            "Only spendable Account cash and credited, date-available lots enter execute_orders.",
        ),
    )
    return _seal(state, account=result.account, clock=at, journal=(*state.journal, entry))


def export_state(state):
    """Full JSON-compatible audit state, including stage identities needed for replay."""
    _validated(state)
    return {
        "schema_version": "synthetic-corporate-action-state-v1",
        "research_eligible": False,
        "complete_portfolio_valuation_available": False,
        "state": json.loads(_json(asdict(state))),
        "cash_claims": json.loads(_json(cash_claims(state))),
    }


def _checkpoint_invariants(state):
    _text(state.account_id, "checkpoint account ID")
    _instant(state.clock, "checkpoint clock")
    snapshot_ids, record_keys, action_ids, event_keys, journal_keys, opens = (
        set(),
        set(),
        set(),
        set(),
        set(),
        set(),
    )
    previous_capture = None
    for snapshot in state.snapshots:
        _text(snapshot.snapshot_id, "snapshot ID")
        _kind(snapshot.data_kind)
        _symbol(snapshot.symbol)
        _integer(snapshot.shares, "snapshot shares")
        state.calendar.index(snapshot.record_date)
        local = _instant(snapshot.captured_at, "snapshot time").astimezone(SHANGHAI)
        require(
            snapshot.account_id == state.account_id
            and local.date() == snapshot.record_date
            and local.time() >= time(15)
            and snapshot.captured_at <= state.clock,
            "checkpoint snapshot identity or clock mismatch",
        )
        require(
            previous_capture is None or snapshot.captured_at >= previous_capture,
            "checkpoint snapshot times reverse",
        )
        previous_capture = snapshot.captured_at
        require(
            snapshot.shares == sum(lot.shares for lot in snapshot.holdings),
            "snapshot quantity mismatch",
        )
        require(
            re.fullmatch(r"[0-9a-f]{64}", snapshot.account_digest),
            "invalid snapshot account digest",
        )
        key = (snapshot.symbol, snapshot.record_date)
        require(
            snapshot.snapshot_id not in snapshot_ids and key not in record_keys,
            "duplicate snapshot identity",
        )
        snapshot_ids.add(snapshot.snapshot_id)
        record_keys.add(key)
    for right in state.entitlements:
        action = right.action
        key = (action.symbol, action.record_date, action.ex_date)
        require(
            action.action_id not in action_ids and key not in event_keys,
            "duplicate registered action identity",
        )
        action_ids.add(action.action_id)
        event_keys.add(key)
        snapshot = next((s for s in state.snapshots if s.snapshot_id == right.snapshot_id), None)
        require(
            snapshot
            and snapshot.symbol == action.symbol
            and snapshot.record_date == action.record_date
            and action.available_time <= snapshot.captured_at,
            "checkpoint entitlement snapshot mismatch",
        )
        state.calendar.index(action.ex_date)
        _integer(right.eligible_shares, "eligible shares")
        _integer(right.new_shares, "new shares")
        gross = (
            _money(snapshot.shares * action.cash.gross_cash_per_eligible_share)
            if action.cash
            else ZERO
        )
        tax = _money(gross * action.cash.withholding_rate) if action.cash else ZERO
        quantity = (
            snapshot.shares * action.shares.new_shares_per_old_share if action.shares else ZERO
        )
        require(
            (right.eligible_shares, right.gross_cash, right.tax, right.net_cash, right.new_shares)
            == (snapshot.shares, gross, tax, gross - tax, quantity),
            "checkpoint entitlement amounts differ",
        )
        require(
            _seen(state, "action", action.action_id, "register"),
            "entitlement has no registration journal",
        )
        if right.new_shares:
            registration = _seen(state, "action", action.action_id, "register")
            credit = _seen(state, "action", action.action_id, "share_credit")
            require(
                not any(
                    s.symbol == action.symbol
                    and s.snapshot_id != right.snapshot_id
                    and s.captured_at >= registration.applied_at
                    and (credit is None or credit.applied_at > s.captured_at)
                    for s in state.snapshots
                ),
                "checkpoint overlaps an uncredited share entitlement",
            )
    prior = None
    for entry in state.journal:
        _text(entry.identity, "journal identity")
        require(all(isinstance(n, str) for n in entry.notes), "journal notes must be text")
        key = (entry.namespace, entry.identity, entry.stage)
        require(key not in journal_keys, "duplicate journal stage")
        journal_keys.add(key)
        at = _instant(entry.applied_at, "journal time")
        require(
            at <= state.clock and (prior is None or at >= prior.applied_at),
            "journal time exceeds/reverses clock",
        )
        for value in (
            entry.content_digest,
            entry.account_before_digest,
            entry.account_after_digest,
        ):
            require(re.fullmatch(r"[0-9a-f]{64}", value), "invalid journal digest")
        amounts = (
            entry.cash_before,
            entry.cash_after,
            entry.cash_delta,
            entry.gross_receivable_delta,
            entry.tax_payable_delta,
            entry.tax_remitted,
        )
        require(
            all(type(v) is Decimal and v.is_finite() and v == _money(v) for v in amounts),
            "journal amounts must be finite cents",
        )
        require(
            entry.cash_after >= ZERO
            and entry.cash_before >= ZERO
            and entry.cash_after - entry.cash_before == entry.cash_delta,
            "journal cash mismatch",
        )
        require(
            prior is None
            or (
                entry.cash_before == prior.cash_after
                and entry.account_before_digest == prior.account_after_digest
            ),
            "account journal chain differs",
        )
        _integer(entry.shares_delta, "journal shares delta")
        if entry.namespace == "action":
            right = next(
                (r for r in state.entitlements if r.action.action_id == entry.identity), None
            )
            require(right is not None, "action journal has no entitlement")
            action = right.action
            require(
                entry.symbol == action.symbol
                and entry.content_digest
                == _digest({"action": asdict(action), "snapshot_id": right.snapshot_id}),
                "action journal identity differs",
            )
            day = at.astimezone(SHANGHAI).date()
            require(
                entry.stage in {"register", "ex_accrual", "cash_payment", "share_credit"},
                "invalid journal stage",
            )
            if entry.stage == "register":
                snapshot = next(s for s in state.snapshots if s.snapshot_id == right.snapshot_id)
                require(
                    day == action.record_date and at >= snapshot.captured_at,
                    "registration timing differs",
                )
                expected = (ZERO, ZERO, ZERO, ZERO, 0)
            else:
                require(
                    ("action", entry.identity, "register") in journal_keys,
                    "journal precedes registration",
                )
                if entry.stage == "ex_accrual":
                    require(day == action.ex_date, "ex-accrual timing differs")
                    expected = (ZERO, right.gross_cash, right.tax, ZERO, 0)
                else:
                    require(
                        ("action", entry.identity, "ex_accrual") in journal_keys,
                        "settlement precedes ex accrual",
                    )
                    if entry.stage == "cash_payment":
                        require(
                            action.cash and day >= action.cash.payment_date,
                            "payment timing differs",
                        )
                        expected = (right.net_cash, -right.gross_cash, -right.tax, right.tax, 0)
                    else:
                        require(
                            action.shares and day >= action.shares.credit_date,
                            "share credit timing differs",
                        )
                        state.calendar.index(
                            max(
                                day,
                                action.shares.listing_date,
                                action.shares.explicitly_sellable_from,
                            )
                        )
                        expected = (ZERO, ZERO, ZERO, ZERO, right.new_shares)
            require(
                (
                    entry.cash_delta,
                    entry.gross_receivable_delta,
                    entry.tax_payable_delta,
                    entry.tax_remitted,
                    entry.shares_delta,
                )
                == expected,
                "stage accounting differs",
            )
        else:
            local = at.astimezone(SHANGHAI)
            state.calendar.index(local.date())
            require(
                entry.namespace == "execution"
                and entry.stage == "open"
                and entry.symbol is None
                and local.time() == time(9, 30)
                and at not in opens,
                "invalid execution journal identity",
            )
            require(
                all(
                    v == ZERO
                    for v in (
                        entry.gross_receivable_delta,
                        entry.tax_payable_delta,
                        entry.tax_remitted,
                        entry.shares_delta,
                    )
                ),
                "execution changed action claims",
            )
            require(
                not any(
                    r.action.ex_date <= local.date()
                    and ("action", r.action.action_id, "ex_accrual") not in journal_keys
                    for r in state.entitlements
                ),
                "execution precedes due ex accrual",
            )
            opens.add(at)
        prior = entry
    if prior:
        require(
            prior.cash_after == state.account.cash
            and prior.account_after_digest == _digest(asdict(state.account)),
            "final account differs from journal",
        )


def restore_state(payload, *, expected_integrity):
    """Restore an exported checkpoint against an independently retained state checksum.

    The caller must retain the checksum separately from the checkpoint. This is
    content integrity, not authentication, event certification or storage locking.
    """
    require(
        isinstance(expected_integrity, str) and re.fullmatch(r"[0-9a-f]{64}", expected_integrity),
        "independently retained SHA-256 state integrity is required",
    )
    require(
        isinstance(payload, dict)
        and set(payload)
        == {
            "schema_version",
            "research_eligible",
            "complete_portfolio_valuation_available",
            "state",
            "cash_claims",
        },
        "invalid checkpoint envelope",
    )
    require(
        payload["schema_version"] == "synthetic-corporate-action-state-v1"
        and payload["research_eligible"] is False
        and payload["complete_portfolio_valuation_available"] is False,
        "unsupported checkpoint schema or provenance",
    )
    raw = payload["state"]
    require(
        isinstance(raw, dict) and raw.get("integrity") == expected_integrity,
        "checkpoint differs from independently retained integrity",
    )
    require(
        _digest({k: v for k, v in raw.items() if k != "integrity"}) == expected_integrity,
        "checkpoint content digest differs",
    )

    def lots(rows):
        return [
            HoldingLot(
                **dict(
                    row,
                    available_from=date.fromisoformat(row["available_from"]),
                    unit_cost=Decimal(row["unit_cost"]),
                )
            )
            for row in rows
        ]

    def action(row):
        cash = row["cash"]
        if cash is not None:
            cash = CashDividend(**dict(cash, payment_date=date.fromisoformat(cash["payment_date"])))
        shares = row["shares"]
        if shares is not None:
            shares = ShareDistribution(
                **dict(
                    shares,
                    **{
                        key: date.fromisoformat(shares[key])
                        for key in ("credit_date", "listing_date", "explicitly_sellable_from")
                    },
                )
            )
        return CorporateAction(
            **dict(
                row,
                cash=cash,
                shares=shares,
                record_date=date.fromisoformat(row["record_date"]),
                ex_date=date.fromisoformat(row["ex_date"]),
                publish_time=datetime.fromisoformat(row["publish_time"]),
                available_time=datetime.fromisoformat(row["available_time"]),
            )
        )

    try:
        account = Account(
            **dict(
                raw["account"],
                cash=Decimal(raw["account"]["cash"]),
                lots={s: lots(rows) for s, rows in raw["account"]["lots"].items()},
            )
        )
        calendar = ExecutionCalendar(**raw["calendar"])
        snapshots = tuple(
            RecordSnapshot(
                **dict(
                    row,
                    record_date=date.fromisoformat(row["record_date"]),
                    captured_at=datetime.fromisoformat(row["captured_at"]),
                    holdings=tuple(lots(row["holdings"])),
                )
            )
            for row in raw["snapshots"]
        )
        entitlements = tuple(
            Entitlement(
                **dict(
                    row,
                    action=action(row["action"]),
                    **{key: Decimal(row[key]) for key in ("gross_cash", "tax", "net_cash")},
                )
            )
            for row in raw["entitlements"]
        )
        journal = tuple(
            JournalEntry(
                **dict(
                    row,
                    applied_at=datetime.fromisoformat(row["applied_at"]),
                    notes=tuple(row["notes"]),
                    **{
                        key: Decimal(row[key])
                        for key in (
                            "cash_before",
                            "cash_after",
                            "cash_delta",
                            "gross_receivable_delta",
                            "tax_payable_delta",
                            "tax_remitted",
                        )
                    },
                )
            )
            for row in raw["journal"]
        )
        result = ActionState(
            **dict(
                raw,
                account=account,
                calendar=calendar,
                clock=datetime.fromisoformat(raw["clock"]),
                snapshots=snapshots,
                entitlements=entitlements,
                journal=journal,
            )
        )
        _validated(result)
        _checkpoint_invariants(result)
        require(export_state(result) == payload, "checkpoint is not a canonical lifecycle export")
        return result
    except (TypeError, KeyError, ValueError, InvalidOperation) as exc:
        if isinstance(exc, CorporateActionError):
            raise
        raise CorporateActionError("invalid typed checkpoint") from exc
