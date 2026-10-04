"""Synthetic close values across actual corporate-action and execution lifecycles."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, Inexact, Rounded, getcontext, localcontext
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.corporate_actions import (
    COST_POLICY,
    ActionStep,
    CashDividend,
    CorporateAction,
    ShareDistribution,
    apply_actions,
    capture_record_close,
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
    HoldingLot,
    LotRule,
    MarketObservation,
    Order,
    TaxRate,
)
from ashare_lab.valuation import (
    SAME_UNADJUSTED_CLOSE,
    ValuationInputError,
    ValuationPrice,
    value_at_close,
)


D = Decimal
SH = ZoneInfo("Asia/Shanghai")
SYMBOL = "000001.SZ"
OTHER = "600001.SH"
DATES = tuple(date(2023, 1, day) for day in (3, 4, 5, 6, 9, 10, 11, 12))


def at(day, hour=16, minute=0):
    return datetime(2023, 1, day, hour, minute, tzinfo=SH)


def initial(*, quantity=100, cash="0", available=None):
    lots = {SYMBOL: [HoldingLot(quantity, available or DATES[0], D("20"))]} if quantity else {}
    return create_state(
        "account-A",
        Account(D(cash), lots),
        ExecutionCalendar(DATES),
        asof=at(3, 9),
        data_kind="synthetic",
    )


def action(**changes):
    return replace(
        CorporateAction(
            "action-1",
            1,
            SYMBOL,
            DATES[1],
            DATES[2],
            at(3, 9),
            at(3, 10),
            "implementation",
            True,
            "synthetic",
            CashDividend(D("2"), DATES[3], "synthetic_flat_withholding", D("0.1"), "synthetic"),
            ShareDistribution(
                "stock_bonus",
                D("0.5"),
                DATES[4],
                DATES[5],
                DATES[6],
                "integer_only",
                COST_POLICY,
                "synthetic",
            ),
        ),
        **changes,
    )


def step(state, stage, when, terms=None, snapshot_id="record-1"):
    return apply_actions(state, [ActionStep(terms or action(), snapshot_id, stage, when)])


def registered(state=None, terms=None):
    state = capture_record_close(state or initial(), "record-1", SYMBOL, DATES[1], at=at(4, 15))
    return step(state, "register", at(4, 15), terms)


def accrued(state=None, terms=None):
    return step(registered(state, terms), "ex_accrual", at(5, 9), terms)


def price(day=5, close="12", **changes):
    return replace(
        ValuationPrice(
            SYMBOL, date(2023, 1, day), D(close), at(day, 15), "unadjusted", "synthetic"
        ),
        **changes,
    )


def value(state, day=5, close="12", prices=None, **changes):
    options = {
        "on_date": date(2023, 1, day),
        "asof": at(day),
        "state_processed_through": at(day),
        "corporate_action_scope_complete": True,
        "uncredited_share_policy": SAME_UNADJUSTED_CLOSE,
    }
    options.update(changes)
    return value_at_close(
        state, {SYMBOL: price(day, close)} if prices is None else prices, **options
    )


def sell(state, day=5, quantity=100, close="12"):
    when = date(2023, 1, day)
    prior = DATES[DATES.index(when) - 1]
    return execute_session(
        state,
        f"sell-{day}",
        at=at(day, 9, 30),
        orders=[Order(f"order-{day}", SYMBOL, "SELL", quantity, prior, when)],
        market={
            SYMBOL: MarketObservation(
                SYMBOL,
                when,
                D(close),
                status="open",
                no_price_limits=True,
                open_capacity_shares=10000,
            )
        },
        costs=CostModel(
            commission_rate=D(0), commission_minimum=D(0), tax_schedule=(TaxRate(DATES[0], D(0)),)
        ),
        lot_rules={SYMBOL: LotRule(100, 100, 100)},
        action_guard=ActionGuard(when, known_no_actions=True),
    )


@pytest.mark.parametrize("tax_rate,expected_tax", [("0", "0"), ("0.1", "20")])
def test_theoretical_ex_price_conserves_assets_except_explicit_tax(tax_rate, expected_tax):
    terms = action(cash=replace(action().cash, withholding_rate=D(tax_rate)))
    before = registered(terms=terms)
    before_value = value(before, 4, "20", uncredited_share_policy=None)
    assert before_value["nav"] == D("2000")
    assert before_value["gross_cash_receivable"] == 0
    assert before_value["uncredited_shares_market_value"] == 0

    state = step(before, "ex_accrual", at(5, 9), terms)
    after = value(state)
    assert after["credited_shares_market_value"] == D("1200")
    assert after["uncredited_shares_market_value"] == D("600")
    assert after["gross_cash_receivable"] == D("200")
    assert after["withholding_payable"] == D(expected_tax)
    assert after["nav"] + D(expected_tax) == before_value["nav"]

    # Declared payment/credit dates alone do not mean cash or shares have arrived.
    delayed = value(state, 9)
    assert delayed["spendable_cash"] == 0
    assert delayed["gross_cash_receivable"] == D("200")
    assert delayed["uncredited_shares_market_value"] == D("600")

    state = step(state, "share_credit", at(9, 16), terms)
    credited = value(state, 9)
    row = credited["securities"][SYMBOL]
    assert (row["credited_shares"], row["locked_credited_shares"]) == (150, 50)
    assert row["date_available_shares"] == 100
    assert credited["uncredited_shares_market_value"] == 0
    assert credited["credited_shares_market_value"] == D("1800")

    state = step(state, "cash_payment", at(10, 9), terms)
    paid = value(state, 10, uncredited_share_policy=None)
    assert paid["gross_cash_receivable"] == paid["withholding_payable"] == 0
    assert paid["spendable_cash"] == D("200") - D(expected_tax)
    unlocked = value(state, 11, uncredited_share_policy=None)
    assert unlocked["securities"][SYMBOL]["date_available_shares"] == 150
    assert unlocked["securities"][SYMBOL]["locked_credited_shares"] == 0
    assert after["nav"] == delayed["nav"] == credited["nav"] == paid["nav"] == unlocked["nav"]


def test_sold_record_holdings_keep_cash_and_uncredited_share_values():
    sold = sell(accrued())
    result = value(sold)
    row = result["securities"][SYMBOL]
    assert row["credited_shares"] == row["date_available_shares"] == 0
    assert row["uncredited_shares"] == 50
    assert result["spendable_cash"] == D("1200")
    assert result["nav"] == D("1980")
    with pytest.raises(ValuationInputError, match="missing close price"):
        value(sold, prices={})

    locked = step(sold, "share_credit", at(9, 9))
    locked_result = value(locked, 9)
    row = locked_result["securities"][SYMBOL]
    assert (row["credited_shares"], row["locked_credited_shares"]) == (50, 50)
    assert row["date_available_shares"] == 0
    assert locked_result["nav"] == result["nav"]

    paid = step(locked, "cash_payment", at(10, 9))
    released = value(paid, 11)
    assert released["securities"][SYMBOL]["date_available_shares"] == 50
    final = value(sell(paid, 11, 50), 11, prices={}, uncredited_share_policy=None)
    assert final["spendable_cash"] == final["nav"] == result["nav"]
    assert final["credited_shares_market_value"] == 0


def test_cash_only_claim_without_holdings_requires_no_quote():
    terms = action(shares=None)
    sold = sell(accrued(terms=terms), close="18")
    result = value(sold, prices={}, uncredited_share_policy=None)
    row = result["securities"][SYMBOL]
    assert row["price"] is None
    assert row["credited_shares"] == row["uncredited_shares"] == 0
    assert row["gross_cash_receivable"] == D("200")
    assert row["net_cash_receivable"] == D("180")
    assert result["nav"] == D("1980")
    settled = step(sold, "cash_payment", at(10, 9), terms)
    final = value(settled, 10, prices={}, uncredited_share_policy=None)
    assert final["nav"] == result["nav"]
    assert final["securities"][SYMBOL]["price"] is None


def test_zero_entitlement_does_not_create_price_or_policy_requirement():
    result = value(
        accrued(initial(quantity=0, cash="321")), prices={}, uncredited_share_policy=None
    )
    assert result["nav"] == D("321")
    assert result["securities"][SYMBOL]["price"] is None
    assert result["securities"][SYMBOL]["uncredited_shares"] == 0


def test_cash_account_with_no_operations_can_be_valued_at_later_close():
    state = initial(quantity=0, cash="321.10")
    result = value(state, 12, prices={}, uncredited_share_policy=None)
    assert result["nav"] == D("321.10")
    assert result["state_clock"] == at(3, 9)
    assert result["securities"] == {}


def test_all_credited_lots_are_valued_even_if_locked():
    state = initial(available=DATES[-1])
    result = value(state, uncredited_share_policy=None)
    row = result["securities"][SYMBOL]
    assert row["credited_shares"] == row["locked_credited_shares"] == 100
    assert row["date_available_shares"] == 0
    assert row["credited_shares_market_value"] == D("1200")
    with pytest.raises(ValuationInputError, match="missing close price"):
        value(state, prices={})


def test_multi_security_decomposition_reconciles_each_asset_and_total():
    state = create_state(
        "portfolio-B",
        Account(
            D("1000"),
            {
                SYMBOL: [HoldingLot(100, DATES[0], D("20"))],
                OTHER: [HoldingLot(200, DATES[0], D("10"))],
            },
        ),
        ExecutionCalendar(DATES),
        asof=at(3, 9),
        data_kind="synthetic",
    )
    terms = action(
        action_id="action-2",
        symbol=OTHER,
        shares=None,
        cash=replace(action().cash, gross_cash_per_eligible_share=D("3")),
    )
    state = capture_record_close(state, "record-1", SYMBOL, DATES[1], at=at(4, 15))
    state = capture_record_close(state, "record-2", OTHER, DATES[1], at=at(4, 15))
    state = step(state, "register", at(4, 15))
    state = step(state, "register", at(4, 15), terms, "record-2")
    state = step(state, "ex_accrual", at(5, 9))
    state = step(state, "ex_accrual", at(5, 9), terms, "record-2")
    with localcontext() as caller_context:
        caller_context.prec = 2
        result = value(state, prices={SYMBOL: price(), OTHER: price(close="7", symbol=OTHER)})
    first, second = result["securities"][SYMBOL], result["securities"][OTHER]
    assert first["value_excluding_spendable_cash"] == D("1980")
    assert second["value_excluding_spendable_cash"] == D("1940")
    assert result["credited_shares_market_value"] == D("2600")
    assert result["gross_cash_receivable"] == D("800")
    assert result["withholding_payable"] == D("80")
    assert result["nav"] == D("4920")
    assert result["nav"] == result["spendable_cash"] + sum(
        row["value_excluding_spendable_cash"] for row in result["securities"].values()
    )


def test_multiple_unpaid_cash_events_for_one_security_are_summed():
    first = action(shares=None, cash=replace(action().cash, payment_date=DATES[-1]))
    second = action(
        action_id="action-2",
        record_date=DATES[3],
        ex_date=DATES[4],
        shares=None,
        cash=replace(first.cash, gross_cash_per_eligible_share=D("3")),
    )
    state = accrued(terms=first)
    state = capture_record_close(state, "record-2", SYMBOL, DATES[3], at=at(6, 15))
    state = step(state, "register", at(6, 15), second, "record-2")
    state = step(state, "ex_accrual", at(9, 9), second, "record-2")
    result = value(state, 9, "15", uncredited_share_policy=None)
    assert result["gross_cash_receivable"] == D("500")
    assert result["withholding_payable"] == D("50")
    assert result["securities"][SYMBOL]["net_cash_receivable"] == D("450")


def test_no_additional_per_security_or_pending_share_rounding():
    state = accrued(initial(quantity=2, cash="1.23"))
    before = value(state, 9, "12.3456789")
    after = value(step(state, "share_credit", at(9, 9)), 9, "12.3456789")
    assert before["credited_shares_market_value"] == D("24.6913578")
    assert before["uncredited_shares_market_value"] == D("12.3456789")
    assert after["credited_shares_market_value"] == D("37.0370367")
    assert before["nav"] == after["nav"] == D("41.8670367")


@pytest.mark.parametrize("ambient_precision", [6, 28])
def test_long_price_and_caller_precision_do_not_change_grouped_asset_value(ambient_precision):
    state = accrued(initial(quantity=2, cash="1.23"))
    credited = step(state, "share_credit", at(9, 9))
    long_price = "12.3456789012345678901234567890123456789"
    with localcontext() as expected_context:
        expected_context.prec = 100
        expected = 3 * D(long_price) + D("4.83")
    with localcontext() as caller_context:
        caller_context.prec = ambient_precision
        flags_before = dict(caller_context.flags)
        before = value(state, 9, long_price)
        after = value(credited, 9, long_price)
        assert before["nav"] == after["nav"] == expected
        assert getcontext().prec == ambient_precision
        assert caller_context.flags == flags_before


@pytest.mark.parametrize("amount", [D("1e129"), D("1e-129"), D("1." + "2" * 128)])
def test_price_outside_supported_decimal_size_fails_without_rounding(amount):
    with pytest.raises(ValuationInputError, match="numeric range"):
        value(initial(), prices={SYMBOL: price(close_price=amount)})


def test_ledger_cent_rounding_survives_strict_caller_context_without_changing_it():
    terms = action(cash=replace(action().cash, gross_cash_per_eligible_share=D("1.235")))
    state = accrued(initial(quantity=2, cash="1.23"), terms)
    before = export_state(state)
    with localcontext() as caller_context:
        caller_context.prec = 2
        caller_context.traps[Inexact] = True
        caller_context.traps[Rounded] = True
        flags, traps = dict(caller_context.flags), dict(caller_context.traps)
        result = value(state, close="12.34")
        assert caller_context.prec == 2
        assert caller_context.flags == flags and caller_context.traps == traps
    assert result["gross_cash_receivable"] == D("2.47")
    assert result["withholding_payable"] == D("0.25")
    assert result["nav"] == D("40.47")
    assert export_state(state) == before


@pytest.mark.parametrize("amount", [D("1e128"), D("1e-128"), D("1." + "2" * 127)])
def test_supported_price_boundaries_are_valued_exactly(amount):
    result = value(initial(quantity=1), prices={SYMBOL: price(close_price=amount)})
    assert result["nav"] == amount


def test_integer_quantity_range_is_explicit_and_checked():
    result = value(initial(quantity=10**127), close="1e-128")
    assert result["nav"] == D("0.1")
    with pytest.raises(ValuationInputError, match="integer.*numeric range"):
        value(initial(quantity=10**128))


def test_state_amount_range_is_checked_before_restore_arithmetic():
    with localcontext() as construction_context:
        construction_context.prec = 200
        state = initial(quantity=0, cash="1e127")
    with pytest.raises(ValuationInputError, match="numeric range"):
        value(state, prices={})


def test_replay_and_checkpoint_restore_preserve_valuation():
    state = accrued()
    expected = value(state)
    replayed = step(state, "ex_accrual", at(5, 16))
    restored = restore_state(export_state(state), expected_integrity=state.integrity)
    assert value(replayed) == value(restored) == expected


def test_price_state_options_and_legacy_export_are_not_modified():
    state = accrued()
    prices = {SYMBOL: price()}
    options = {
        "on_date": DATES[2],
        "asof": at(5),
        "state_processed_through": at(5),
        "corporate_action_scope_complete": True,
        "uncredited_share_policy": SAME_UNADJUSTED_CLOSE,
    }
    before_state, before_prices, before_options = (
        export_state(state),
        deepcopy(prices),
        deepcopy(options),
    )
    result = value_at_close(state, prices, **options)
    assert result["data_kind"] == "synthetic" and result["research_eligible"] is False
    assert result["supported_synthetic_scope_valued"] is True
    assert result["complete_portfolio_valuation_available"] is False
    assert result["state_integrity"] == state.integrity
    assert result["account_id"] == state.account_id
    assert result["on_date"] == DATES[2]
    assert result["asof"] == at(5).astimezone(timezone.utc)
    assert result["price_adjustment_basis"] == "unadjusted"
    assert result["uncredited_share_policy"] == SAME_UNADJUSTED_CLOSE
    result["securities"][SYMBOL]["price"]["close_price"] = D("999")
    result["securities"].clear()
    assert export_state(state) == before_state
    assert prices == before_prices and options == before_options
    assert before_state["complete_portfolio_valuation_available"] is False
    assert before_state["cash_claims"]["complete_portfolio_valuation_available"] is False


def test_equivalent_timezone_instants_satisfy_processed_through_declaration():
    result = value(initial(), state_processed_through=at(5).astimezone(timezone.utc))
    assert result["state_processed_through"] == result["asof"]


@pytest.mark.parametrize("scope", [False, None, 1, "complete"])
def test_unknown_or_truthy_scope_is_rejected(scope):
    with pytest.raises(ValuationInputError, match="scope"):
        value(initial(), corporate_action_scope_complete=scope)


def test_scope_is_unknown_by_default():
    with pytest.raises(ValuationInputError, match="scope"):
        value_at_close(initial(), {}, on_date=DATES[2], asof=at(5), state_processed_through=at(5))


@pytest.mark.parametrize("quantity", [0, 100])
def test_due_unaccrued_action_cannot_be_overridden_by_completeness_declaration(quantity):
    state = registered(initial(quantity=quantity))
    with pytest.raises(ValuationInputError, match="action-1.*no ex_accrual"):
        value(state, prices={})


def test_pending_shares_require_explicit_assumption():
    with pytest.raises(ValuationInputError, match="explicit synthetic price assumption"):
        value(accrued(), uncredited_share_policy=None)


@pytest.mark.parametrize("policy", ["fair_value", "", False, []])
def test_unknown_share_policy_fails_even_without_pending_shares(policy):
    with pytest.raises(ValuationInputError, match="unknown uncredited-share"):
        value(initial(), uncredited_share_policy=policy)


@pytest.mark.parametrize("processed", [at(5, 15), at(5, 17), datetime(2023, 1, 5, 16)])
def test_processed_through_must_be_aware_and_match_query(processed):
    with pytest.raises(ValuationInputError, match="state_processed_through"):
        value(initial(), state_processed_through=processed)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"asof": at(5, 14, 59)}, "15:00"),
        ({"asof": at(6)}, "Shanghai date"),
        ({"asof": datetime(2023, 1, 5, 16)}, "timezone aware"),
        ({"on_date": "2023-01-05"}, "must be a date"),
        ({"on_date": at(5)}, "must be a date"),
        ({"on_date": None}, "must be a date"),
    ],
)
def test_invalid_query_boundaries_fail(changes, message):
    with pytest.raises(ValuationInputError, match=message):
        value(initial(), **changes)


def test_nontrading_date_fails_even_with_same_day_quote():
    with pytest.raises(ValuationInputError, match="trading calendar"):
        value(initial(), 7)


def test_future_state_is_rejected_without_mutation():
    state = capture_record_close(initial(), "later", SYMBOL, DATES[3], at=at(6, 15))
    before = export_state(state)
    with pytest.raises(ValuationInputError, match="state clock.*future"):
        value(state)
    assert export_state(state) == before


def test_non_state_and_tampered_state_are_rejected():
    with pytest.raises(ValuationInputError, match="ActionState"):
        value({})
    state = initial()
    state.account.cash += D("1")
    with pytest.raises(ValuationInputError, match="invalid action state"):
        value(state)


@pytest.mark.parametrize("basis", ["qfq", "hfq", "adjusted", "unknown", None, ""])
def test_adjusted_or_unknown_quote_basis_fails(basis):
    with pytest.raises(ValuationInputError, match="unadjusted"):
        price(adjustment_basis=basis)


@pytest.mark.parametrize("kind", [None, "real", "unknown"])
def test_price_requires_explicit_synthetic_provenance(kind):
    with pytest.raises(ValuationInputError, match="synthetic"):
        price(data_kind=kind)


@pytest.mark.parametrize(
    "amount", ["0", "-1", "NaN", "sNaN", "Infinity", "-Infinity", "x", True, 1.2, None]
)
def test_invalid_price_amounts_fail(amount):
    with pytest.raises(ValuationInputError, match="close price"):
        price(close_price=amount)


@pytest.mark.parametrize("symbol", ["", "000001", "000001.XX", "ABCDEF.SZ", None])
def test_invalid_price_symbols_fail(symbol):
    with pytest.raises(ValuationInputError, match="price symbol"):
        price(symbol=symbol)


@pytest.mark.parametrize("when", [at(5, 14, 59), at(4, 16), datetime(2023, 1, 5, 15)])
def test_price_cannot_be_known_before_close_or_have_naive_time(when):
    with pytest.raises(ValuationInputError, match="close|timezone aware"):
        price(available_time=when)


def test_future_quote_fails_and_preserves_inputs():
    prices = {SYMBOL: price(available_time=at(5, 16, 1))}
    before = deepcopy(prices)
    state = accrued()
    checkpoint = export_state(state)
    with pytest.raises(ValuationInputError, match="not available"):
        value(state, prices=prices)
    assert prices == before and export_state(state) == checkpoint


@pytest.mark.parametrize("day", [4, 6])
def test_yesterday_and_future_event_dates_are_not_substitutes(day):
    with pytest.raises(ValuationInputError, match="event date differs"):
        value(initial(), prices={SYMBOL: price(day)})


def test_wrong_security_and_untyped_quotes_are_rejected():
    with pytest.raises(ValuationInputError, match="key differs"):
        value(initial(), prices={SYMBOL: price(symbol=OTHER)})
    with pytest.raises(ValuationInputError, match="missing close price"):
        value(initial(), prices={OTHER: price(symbol=OTHER)})
    with pytest.raises(ValuationInputError, match="ValuationPrice"):
        value(initial(), prices={SYMBOL: D("12")})
    with pytest.raises(ValuationInputError, match="mapping"):
        value(initial(), prices=[])


def test_tampered_frozen_price_is_revalidated():
    quote = price()
    object.__setattr__(quote, "adjustment_basis", "qfq")
    with pytest.raises(ValuationInputError, match="unadjusted"):
        value(initial(), prices={SYMBOL: quote})


def test_timezone_conversion_overflow_is_a_valuation_error():
    with pytest.raises(ValuationInputError, match="Shanghai"):
        value(initial(), asof=datetime.max.replace(tzinfo=timezone.utc))
    with pytest.raises(ValuationInputError, match="Shanghai"):
        price(available_time=datetime.max.replace(tzinfo=timezone.utc))


def test_exact_close_boundary_is_accepted():
    result = value(initial(), asof=at(5, 15), state_processed_through=at(5, 15))
    assert result["nav"] == D("1200")
