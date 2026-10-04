"""Lifecycle rights, settlement, persistence and execution boundaries, all synthetic."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.corporate_actions import (
    COST_POLICY,
    ActionStep,
    CashDividend,
    CorporateAction,
    CorporateActionError,
    ShareDistribution,
    apply_actions,
    capture_record_close,
    cash_claims,
    create_state,
    execute_session,
    export_state,
    restore_state,
)
from ashare_lab.execution import (
    Account,
    ActionGuard,
    CostModel,
    ExecutionCalendar,
    ExecutionInputError,
    HoldingLot,
    LotRule,
    MarketObservation,
    Order,
    TaxRate,
)


SYMBOL = "000001.SZ"
OTHER = "600001.SH"
D = Decimal
SH = ZoneInfo("Asia/Shanghai")
DATES = tuple(date(2023, 1, d) for d in (3, 4, 5, 6, 9, 10, 11, 12))


def at(day, hour=9, minute=0):
    return datetime(2023, 1, day, hour, minute, tzinfo=SH)


def cash(**changes):
    return replace(
        CashDividend(D("1.235"), DATES[3], "synthetic_flat_withholding", D("0.1"), "synthetic"),
        **changes,
    )


def shares(**changes):
    return replace(
        ShareDistribution(
            "stock_bonus",
            D("0.2"),
            DATES[2],
            DATES[4],
            DATES[5],
            "integer_only",
            COST_POLICY,
            "synthetic",
        ),
        **changes,
    )


def action(**changes):
    return replace(
        CorporateAction(
            "div-1",
            1,
            SYMBOL,
            DATES[1],
            DATES[2],
            at(3),
            at(3, 10),
            "implementation",
            True,
            "synthetic",
            cash(),
            shares(),
        ),
        **changes,
    )


def initial(cash_amount="0", quantity=100, available=None, account_id="account-A"):
    lots = {SYMBOL: [HoldingLot(quantity, available or DATES[0], D("10"))]} if quantity else {}
    return create_state(
        account_id,
        Account(D(cash_amount), lots),
        ExecutionCalendar(DATES),
        asof=at(3),
        data_kind="synthetic",
    )


def captured(state=None, snapshot_id="record-1", symbol=SYMBOL, day=4):
    return capture_record_close(
        state or initial(), snapshot_id, symbol, date(2023, 1, day), at=at(day, 15)
    )


def run_step(state, stage, when, terms=None, snapshot="record-1"):
    return apply_actions(state, [ActionStep(terms or action(), snapshot, stage, when)])


def registered(state=None, terms=None):
    return run_step(captured(state), "register", at(4, 15), terms)


def accrued(state=None, terms=None):
    return run_step(registered(state, terms), "ex_accrual", at(5), terms)


def trade(
    state, day, side="SELL", quantity=100, *, session=None, symbol=SYMBOL, guard=None, price="10"
):
    when = date(2023, 1, day)
    prior = DATES[DATES.index(when) - 1]
    return execute_session(
        state,
        session or f"session-{day}",
        at=at(day, 9, 30),
        orders=[Order(f"{side}-{symbol}-{day}", symbol, side, quantity, prior, when)],
        market={
            symbol: MarketObservation(
                symbol,
                when,
                D(price),
                status="open",
                no_price_limits=True,
                open_capacity_shares=10000,
            )
        },
        costs=CostModel(
            commission_rate=D(0), commission_minimum=D(0), tax_schedule=(TaxRate(DATES[0], D(0)),)
        ),
        lot_rules={symbol: LotRule(100, 100, 100)},
        action_guard=guard if guard is not None else ActionGuard(when, known_no_actions=True),
    )


def order_result(state):
    return json.loads(state.journal[-1].notes[0])["orders"][0]


def test_selling_after_record_does_not_lose_dividend_or_bonus_rights():
    state = trade(accrued(), 5)
    assert state.account.total_shares(SYMBOL) == 0
    assert state.account.cash == D("1000")
    state = run_step(state, "share_credit", at(5, 16))
    state = run_step(state, "cash_payment", at(6))
    assert state.account.cash == D("1111.15")
    assert state.account.total_shares(SYMBOL) == 20
    assert state.entitlements[0].eligible_shares == 100
    assert cash_claims(state)["net_cash_receivable"] == 0


def test_buying_after_record_does_not_create_extra_entitlement():
    state = trade(accrued(initial("1000")), 5, "BUY")
    assert state.account.total_shares(SYMBOL) == 200
    assert state.entitlements[0].eligible_shares == 100
    state = run_step(state, "cash_payment", at(6))
    assert state.account.cash == D("111.15")


def test_no_record_position_cannot_claim_after_ex_date_purchase():
    state = trade(accrued(initial("1000", 0)), 5, "BUY")
    state = run_step(state, "cash_payment", at(6))
    state = run_step(state, "share_credit", at(6, 10))
    assert state.account.cash == 0
    assert state.account.total_shares(SYMBOL) == 100
    assert state.entitlements[0].new_shares == 0
    assert state.entitlements[0].net_cash == 0


def test_ex_accrual_creates_claims_only_and_receivable_cannot_fund_trade():
    registered_state = registered()
    assert cash_claims(registered_state)["net_cash_receivable"] == 0
    state = run_step(registered_state, "ex_accrual", at(5))
    assert cash_claims(state) == {
        "spendable_cash": D(0),
        "gross_cash_receivable": D("123.50"),
        "withholding_payable": D("12.35"),
        "net_cash_receivable": D("111.15"),
        "uncredited_shares": {SYMBOL: 20},
        "complete_portfolio_valuation_available": False,
    }
    assert state.account.total_shares(SYMBOL) == 100
    result = trade(state, 5, "BUY", symbol=OTHER, price="1")
    assert order_result(result)["filled_shares"] == 0
    assert "cash" in order_result(result)["reason"]
    assert result.account.cash == 0


def test_delayed_cash_payment_is_recorded_at_actual_receipt():
    state = accrued()
    paid = run_step(state, "cash_payment", at(10, 16))
    assert paid.journal[-1].applied_at == at(10, 16)
    assert paid.account.cash == D("111.15")
    assert state.account.cash == 0 and cash_claims(state)["net_cash_receivable"] == D("111.15")
    with pytest.raises(CorporateActionError, match="historical"):
        trade(paid, 10, "BUY", price="1")


@pytest.mark.parametrize(
    "tax_mode,rate,gross,tax,net",
    [
        ("synthetic_no_tax", "0", "123.50", "0.00", "123.50"),
        ("synthetic_flat_withholding", "0.1", "123.50", "12.35", "111.15"),
        ("synthetic_flat_withholding", "1", "123.50", "123.50", "0.00"),
    ],
)
def test_cash_tax_conservation_and_rounding(tax_mode, rate, gross, tax, net):
    terms = action(cash=cash(tax_mode=tax_mode, withholding_rate=D(rate)))
    state = run_step(accrued(terms=terms), "cash_payment", at(6), terms)
    right = state.entitlements[0]
    assert (right.gross_cash, right.tax, right.net_cash) == (D(gross), D(tax), D(net))
    assert state.account.cash == D(net)
    assert state.account.cash + sum(j.tax_remitted for j in state.journal) == D(gross)
    assert sum(j.gross_receivable_delta for j in state.journal) == 0
    assert sum(j.tax_payable_delta for j in state.journal) == 0
    assert sum(j.cash_delta for j in state.journal) == state.account.cash


def test_entitlement_uses_eligible_share_cash_not_ex_price_deduction():
    terms = action(cash=cash(ex_price_deduction_per_total_share=D("0.9")))
    state = accrued(terms=terms)
    assert state.entitlements[0].gross_cash == D("123.50")
    rounded = action(cash=cash(gross_cash_per_eligible_share=D("0.00005")))
    right = registered(terms=rounded).entitlements[0]
    assert right.gross_cash == D("0.01") and right.tax == D("0.00")


@pytest.mark.parametrize("kind", ["stock_bonus", "capital_reserve_conversion"])
def test_share_credit_keeps_original_cost_and_uses_latest_restriction(kind):
    terms = action(shares=shares(kind=kind))
    state = accrued(initial(available=DATES[3]), terms)
    credited = run_step(state, "share_credit", at(5), terms)
    assert credited.account.available_shares(SYMBOL, DATES[2]) == 0
    assert credited.account.available_shares(SYMBOL, DATES[3]) == 100
    assert credited.account.available_shares(SYMBOL, DATES[4]) == 100
    assert credited.account.available_shares(SYMBOL, DATES[5]) == 120
    assert credited.account.lots[SYMBOL][0] == state.account.lots[SYMBOL][0]
    assert credited.account.lots[SYMBOL][1].unit_cost == 0
    assert sum(lot.shares * lot.unit_cost for lot in credited.account.lots[SYMBOL]) == D("1000")
    assert credited.entitlements[0].eligible_shares == 100  # Locked old shares still earn rights.


def test_uncredited_and_not_listed_shares_cannot_be_sold():
    state = trade(accrued(), 5)
    blocked = trade(state, 6, quantity=20)
    assert order_result(blocked)["filled_shares"] == 0
    credited = run_step(blocked, "share_credit", at(6, 16))
    blocked_again = trade(credited, 9, quantity=20)
    assert order_result(blocked_again)["filled_shares"] == 0
    sold = trade(blocked_again, 10, quantity=20)
    assert order_result(sold)["filled_shares"] == 20


def test_late_share_credit_cannot_make_shares_available_in_the_past():
    state = run_step(accrued(), "share_credit", at(11, 16))
    assert state.account.lots[SYMBOL][-1].available_from == DATES[6]
    with pytest.raises(CorporateActionError, match="historical"):
        trade(state, 11)


def test_fractional_entitlements_are_rejected_without_rounding_or_mutation():
    terms = action(shares=shares(new_shares_per_old_share=D("0.123")))
    state = captured()
    before = export_state(state)
    with pytest.raises(CorporateActionError, match="fractional"):
        run_step(state, "register", at(4, 15), terms)
    assert export_state(state) == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.account.lots[SYMBOL].clear(),
        lambda s: setattr(s.account, "cash", D("200")),
        lambda s: object.__setattr__(s.snapshots[0], "account_id", "other-account"),
        lambda s: object.__setattr__(s.snapshots[0], "captured_at", at(4, 16)),
        lambda s: object.__setattr__(s.snapshots[0], "shares", 999),
        lambda s: object.__setattr__(s, "account_id", "other-account"),
    ],
)
def test_external_state_and_snapshot_mutations_fail_integrity(mutate):
    state = captured()
    mutate(state)
    with pytest.raises(CorporateActionError, match="outside lifecycle API"):
        run_step(state, "register", at(4, 15))


@pytest.mark.parametrize(
    "symbol,day,snapshot,when,match",
    [
        (OTHER, 4, "record-1", at(4, 15), "different identity"),
        (SYMBOL, 5, "record-1", at(5, 15), "different identity"),
        (SYMBOL, 4, "record-1", at(4, 16), "different identity"),
        (SYMBOL, 4, "different", at(4, 15), "already frozen"),
    ],
)
def test_snapshot_cannot_be_replaced_with_other_holdings(symbol, day, snapshot, when, match):
    with pytest.raises(CorporateActionError, match=match):
        capture_record_close(captured(), snapshot, symbol, date(2023, 1, day), at=when)


@pytest.mark.parametrize("when", [at(4, 14, 59), at(5, 15)])
def test_snapshot_requires_record_day_close(when):
    with pytest.raises(CorporateActionError, match="same-day Shanghai close"):
        capture_record_close(initial(), "record-1", SYMBOL, DATES[1], at=when)


def test_missing_other_account_security_date_or_future_snapshot_cannot_establish_rights():
    with pytest.raises(CorporateActionError, match="absent"):
        run_step(initial(account_id="account-B"), "register", at(4, 15))
    with pytest.raises(CorporateActionError, match="mismatch"):
        run_step(captured(symbol=OTHER), "register", at(4, 15))
    with pytest.raises(CorporateActionError, match="mismatch"):
        run_step(captured(day=3), "register", at(4, 15))
    with pytest.raises(CorporateActionError, match="historical"):
        run_step(captured(), "register", at(4, 14))
    with pytest.raises(CorporateActionError, match="future action"):
        run_step(captured(), "register", at(4, 16), action(available_time=at(4, 16)))


@pytest.mark.parametrize(
    "stage,when,match",
    [
        ("ex_accrual", at(4, 16), "actual ex date"),
        ("ex_accrual", at(6), "actual ex date"),
        ("cash_payment", at(6), "ex accrual must precede"),
        ("share_credit", at(6), "ex accrual must precede"),
    ],
)
def test_stage_prerequisites_and_no_historical_accrual(stage, when, match):
    with pytest.raises(CorporateActionError, match=match):
        run_step(registered(), stage, when)


def test_registration_required_and_duplicate_event_id_rejected():
    with pytest.raises(CorporateActionError, match="not registered"):
        run_step(captured(), "ex_accrual", at(5))
    with pytest.raises(CorporateActionError, match="another ID"):
        run_step(registered(), "register", at(4, 15), action(action_id="second-id"))
    with pytest.raises(CorporateActionError, match="record day"):
        run_step(captured(), "register", at(5))


def test_early_cash_and_share_settlement_rejected():
    with pytest.raises(CorporateActionError, match="before declared payment"):
        run_step(accrued(), "cash_payment", at(5, 10))
    terms = action(shares=shares(credit_date=DATES[3]))
    with pytest.raises(CorporateActionError, match="before declared credit"):
        run_step(accrued(terms=terms), "share_credit", at(5, 10), terms)


def test_missing_cash_or_share_terms_cannot_settle_that_leg():
    only_cash = action(shares=None)
    with pytest.raises(CorporateActionError, match="no share"):
        run_step(accrued(terms=only_cash), "share_credit", at(6), only_cash)
    only_shares = action(cash=None)
    with pytest.raises(CorporateActionError, match="no cash"):
        run_step(accrued(terms=only_shares), "cash_payment", at(6), only_shares)


def test_replay_is_idempotent_and_preserves_later_clock():
    state = run_step(accrued(), "cash_payment", at(6))
    before = export_state(state)
    for stage, when in (("register", at(4, 15)), ("ex_accrual", at(5)), ("cash_payment", at(10))):
        state = run_step(state, stage, when)
    state = capture_record_close(state, "record-1", SYMBOL, DATES[1], at=at(4, 15))
    assert export_state(state) == before
    with pytest.raises(CorporateActionError, match="replay predates"):
        run_step(state, "cash_payment", at(5))


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"ex_date": DATES[3], "shares": shares(credit_date=DATES[3])},
        {"cash": cash(gross_cash_per_eligible_share=D("2"))},
        {"cash": cash(payment_date=DATES[4])},
        {"shares": shares(new_shares_per_old_share=D("0.3"))},
    ],
)
def test_action_id_reuse_with_different_terms_is_rejected(changes):
    state = accrued()
    terms = action(**changes)
    with pytest.raises(CorporateActionError, match="conflict"):
        run_step(state, "ex_accrual", at(6), terms)
    with pytest.raises(CorporateActionError, match="conflict"):
        run_step(state, "cash_payment", at(10), terms)


def test_atomic_batch_failure_does_not_mutate_input():
    state = registered()
    before = export_state(state)
    with pytest.raises(CorporateActionError, match="before declared"):
        apply_actions(
            state,
            [
                ActionStep(action(), "record-1", "ex_accrual", at(5)),
                ActionStep(action(), "record-1", "cash_payment", at(5, 10)),
            ],
        )
    assert export_state(state) == before


def test_unprocessed_ex_accrual_blocks_trading_even_with_explicit_guard():
    with pytest.raises(CorporateActionError, match="unapplied ex accrual"):
        trade(registered(), 5)


def test_action_guard_is_not_automatically_promoted():
    result = trade(accrued(), 5, guard=ActionGuard(DATES[2]))
    assert order_result(result)["reason"] == "unresolved_company_action"
    assert result.account.total_shares(SYMBOL) == 100


def test_execution_sessions_cannot_duplicate_open_or_reuse_changed_content():
    state = trade(accrued(), 5)
    assert export_state(trade(state, 5)) == export_state(state)
    with pytest.raises(CorporateActionError, match="different input"):
        trade(state, 5, price="11")
    with pytest.raises(CorporateActionError, match="another ID"):
        trade(state, 5, session="other-id")


@pytest.mark.parametrize(
    "changes",
    [
        {"data_kind": "real"},
        {"stage": "proposal"},
        {"certified": False},
        {"certified": 1},
        {"version": True},
        {"version": 0},
        {"available_time": None},
        {"available_time": datetime(2023, 1, 3)},
        {"available_time": at(2)},
        {"record_date": DATES[2]},
        {"record_date": "2023-01-04"},
        {"cash": None, "shares": None},
        {"symbol": "０００００１.SZ"},
        {"cash": cash(payment_date=DATES[0])},
        {"shares": shares(listing_date=DATES[0])},
    ],
)
def test_uncertified_unknown_or_invalid_actions_are_rejected(changes):
    with pytest.raises(CorporateActionError):
        action(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"gross_cash_per_eligible_share": 0.1},
        {"gross_cash_per_eligible_share": True},
        {"gross_cash_per_eligible_share": "NaN"},
        {"gross_cash_per_eligible_share": "-1"},
        {"gross_cash_per_eligible_share": "Infinity"},
        {"gross_cash_per_eligible_share": "oops"},
        {"withholding_rate": "1.1"},
        {"tax_mode": "actual_a_share_tax"},
        {"tax_mode": "synthetic_no_tax"},
        {"data_kind": "real"},
    ],
)
def test_unsupported_cash_and_tax_inputs_are_rejected(changes):
    with pytest.raises(CorporateActionError):
        cash(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "rights_issue"},
        {"kind": "split"},
        {"allocation_policy": "floor"},
        {"cost_policy": "tax_basis"},
        {"new_shares_per_old_share": "0"},
        {"credit_date": None},
        {"listing_date": None},
        {"explicitly_sellable_from": None},
    ],
)
def test_unknown_share_policies_and_dates_are_rejected(changes):
    with pytest.raises(CorporateActionError):
        shares(**changes)


def test_future_availability_and_mutated_steps_are_rejected():
    with pytest.raises(CorporateActionError, match="after operation"):
        run_step(captured(), "register", at(4, 15), action(available_time=at(5)))
    step = ActionStep(action(), "record-1", "register", at(4, 15))
    object.__setattr__(step, "stage", "unsupported")
    with pytest.raises(CorporateActionError, match="unsupported"):
        apply_actions(captured(), [step])


def test_actual_share_sellable_day_must_be_in_calendar():
    terms = action(shares=shares(listing_date=DATES[3], explicitly_sellable_from=date(2023, 1, 8)))
    with pytest.raises(ExecutionInputError, match="absent"):
        run_step(accrued(terms=terms), "share_credit", at(5), terms)


def test_checkpoint_round_trip_restores_stage_deduplication_and_detaches_account():
    state = trade(accrued(), 5)
    state = run_step(state, "share_credit", at(5, 16))
    state = run_step(state, "cash_payment", at(6))
    payload = json.loads(json.dumps(export_state(state)))
    restored = restore_state(payload, expected_integrity=state.integrity)
    assert asdict(restored) == asdict(state)
    assert restored.account is not state.account
    assert export_state(run_step(restored, "cash_payment", at(10))) == payload
    assert export_state(trade(restored, 5)) == payload
    result = trade(restored, 10, quantity=20)
    assert result.account.total_shares(SYMBOL) == 0
    assert state.account.total_shares(SYMBOL) == 20
    assert result.account.cash == D("1311.15")
    assert restored.clock.astimezone(timezone.utc) == at(6).astimezone(timezone.utc)


@pytest.mark.parametrize("kind", ["initial", "snapshot", "registered", "accrued"])
def test_checkpoint_round_trip_at_intermediate_stages(kind):
    state = {
        "initial": initial,
        "snapshot": captured,
        "registered": registered,
        "accrued": accrued,
    }[kind]()
    assert export_state(
        restore_state(export_state(state), expected_integrity=state.integrity)
    ) == export_state(state)


@pytest.mark.parametrize(
    "field,value",
    [
        ("research_eligible", True),
        ("complete_portfolio_valuation_available", True),
        ("schema_version", "unknown"),
    ],
)
def test_restore_rejects_changed_envelope(field, value):
    state = accrued()
    payload = export_state(state)
    payload[field] = value
    with pytest.raises(CorporateActionError):
        restore_state(payload, expected_integrity=state.integrity)


def checksum(payload):
    raw = {k: v for k, v in payload["state"].items() if k != "integrity"}
    value = hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["state"]["integrity"] = value
    return value


def test_restore_checks_independent_checksum_and_derived_claims():
    state = accrued()
    original = export_state(state)
    changed = deepcopy(original)
    changed["state"]["account"]["cash"] = "999.00"
    with pytest.raises(CorporateActionError, match="digest differs"):
        restore_state(changed, expected_integrity=state.integrity)
    checksum(changed)
    with pytest.raises(CorporateActionError, match="independently retained"):
        restore_state(changed, expected_integrity=state.integrity)
    original["cash_claims"]["net_cash_receivable"] = "999.00"
    with pytest.raises(CorporateActionError, match="canonical"):
        restore_state(original, expected_integrity=state.integrity)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["state"].update(account_id="account-B"),
        lambda p: p["state"].update(clock=at(3).isoformat()),
        lambda p: p["state"]["snapshots"][0].update(shares=200),
        lambda p: p["state"]["entitlements"][0].update(net_cash="999.00"),
        lambda p: p["state"]["journal"].pop(0),
        lambda p: p["state"]["journal"][1].update(gross_receivable_delta="999.00"),
        lambda p: p["state"]["account"].update(cash="999.00"),
        lambda p: p["state"]["journal"].append(deepcopy(p["state"]["journal"][1])),
    ],
)
def test_restore_validates_account_clock_entitlement_and_stage_invariants(mutate):
    payload = export_state(accrued())
    mutate(payload)
    expected = checksum(payload)  # Even a supplied new checksum cannot validate an invalid state.
    with pytest.raises(CorporateActionError):
        restore_state(payload, expected_integrity=expected)


def test_later_record_snapshot_rejects_overlapping_uncredited_share_rights():
    state = accrued()
    with pytest.raises(CorporateActionError, match="overlapping"):
        captured(state, "next-record", day=5)
    # Replaying the existing snapshot does not reinterpret its shares.
    assert export_state(captured(state)) == export_state(state)
    unrelated = captured(state, "other-record", symbol=OTHER, day=5)
    assert unrelated.snapshots[-1].shares == 0
    credited = run_step(state, "share_credit", at(5))
    next_record = captured(credited, "next-record", day=5)
    assert next_record.snapshots[-1].shares == 120  # Credited but unsellable shares count.
    assert export_state(
        restore_state(export_state(next_record), expected_integrity=next_record.integrity)
    ) == export_state(next_record)


def test_pending_cash_only_does_not_block_another_record_snapshot():
    terms = action(shares=None)
    state = captured(accrued(terms=terms), "next-record", day=5)
    assert state.snapshots[-1].shares == 100


def test_restore_rejects_reversed_snapshot_order_and_broken_cash_chain():
    state = captured(captured(), "other-record", symbol=OTHER, day=5)
    payload = export_state(state)
    payload["state"]["snapshots"].reverse()
    with pytest.raises(CorporateActionError, match="times reverse"):
        restore_state(payload, expected_integrity=checksum(payload))
    payload = export_state(accrued())
    payload["state"]["journal"][1].update(cash_before="1.00", cash_after="1.00")
    with pytest.raises(CorporateActionError, match="chain differs"):
        restore_state(payload, expected_integrity=checksum(payload))
