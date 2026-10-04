"""Boundaries for the synthetic execution and portfolio ledger."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ashare_lab.execution import (
    Account,
    ActionGuard,
    CloseObservation,
    CostModel,
    ExecutionCalendar,
    ExecutionInputError,
    HoldingLot,
    LotRule,
    MarketObservation,
    Order,
    TaxRate,
    build_equal_weight_orders,
    execute_orders,
    five_day_rebalance_pairs,
    mark_to_market,
)


SYMBOL = "000001.SZ"
STAR = "688001.SH"


@pytest.fixture
def calendar():
    return ExecutionCalendar(
        tuple(
            date.fromisoformat(d)
            for d in (
                "2023-01-03",
                "2023-01-04",
                "2023-01-05",
                "2023-01-06",
                "2023-01-09",
                "2023-01-10",
                "2023-01-11",
                "2023-01-12",
                "2023-01-13",
                "2023-01-16",
            )
        )
    )


@pytest.fixture
def costs():
    return CostModel(
        commission_rate=Decimal("0.001"),
        commission_minimum=Decimal("5"),
        transfer_fee_rate=Decimal("0.0001"),
        buy_slippage_bps=Decimal("10"),
        sell_slippage_bps=Decimal("10"),
        tax_schedule=(
            TaxRate(date(2023, 1, 1), Decimal("0.001")),
            TaxRate(date(2023, 8, 28), Decimal("0.0005")),
        ),
    )


def order(side="BUY", shares=100, symbol=SYMBOL, submitted="2023-01-03", execute="2023-01-04"):
    return Order(
        f"o-{side}-{symbol}-{shares}",
        symbol,
        side,
        shares,
        date.fromisoformat(submitted),
        date.fromisoformat(execute),
    )


def market(symbol=SYMBOL, price="10", **kwargs):
    kwargs.setdefault("open_capacity_shares", 10000)
    kwargs.setdefault("status", "open")
    if "limit_up" in kwargs or "limit_down" in kwargs:
        kwargs.setdefault("limit_up", Decimal("11"))
        kwargs.setdefault("limit_down", Decimal("9"))
    else:
        kwargs.setdefault("no_price_limits", True)
    return MarketObservation(
        symbol, date(2023, 1, 4), None if price is None else Decimal(price), **kwargs
    )


def rules(symbol=SYMBOL):
    return {symbol: LotRule(100, 100, 100, sell_min_shares=100)}


def run(account, orders, calendar, costs, market_rows=None, guard=None, lot_rules=None):
    return execute_orders(
        account,
        orders,
        market_rows or {SYMBOL: market()},
        calendar,
        costs,
        lot_rules or rules(),
        guard or ActionGuard(date(2023, 1, 4), known_no_actions=True),
    )


def test_buy_settles_next_trading_day_and_cannot_sell_same_session(calendar, costs):
    account = Account(Decimal("2000"))
    result = run(account, [order("BUY", 100), order("SELL", 100)], calendar, costs)
    assert [r.status for r in result.orders] == ["rejected", "filled"]
    # Sells are intentionally processed first; no starting holdings means rejection.
    assert result.orders[0].reason == "no_available_t1_shares"
    assert result.account.total_shares(SYMBOL) == 100
    assert result.account.available_shares(SYMBOL, date(2023, 1, 4)) == 0
    assert result.account.available_shares(SYMBOL, date(2023, 1, 5)) == 100


def test_holiday_next_date_is_used_for_settlement(calendar, costs):
    friday = date(2023, 1, 6)
    result = run(
        Account(Decimal("2000")),
        [order("BUY", 100, submitted="2023-01-05", execute="2023-01-06")],
        calendar,
        costs,
        {SYMBOL: replace(market(), event_date=friday)},
        guard=ActionGuard(friday, known_no_actions=True),
    )
    assert result.account.lots[SYMBOL][0].available_from == date(2023, 1, 9)
    assert result.account.available_shares(SYMBOL, date(2023, 1, 7)) == 0
    assert result.account.available_shares(SYMBOL, date(2023, 1, 8)) == 0
    assert result.account.available_shares(SYMBOL, date(2023, 1, 9)) == 100


def test_sells_are_processed_before_buys_and_cash_can_fund_buy(calendar, costs):
    account = Account(Decimal("0"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("8"))]})
    sell = order("SELL", 100)
    buy = order("BUY", 200, symbol=STAR)
    market_rows = {SYMBOL: market(price="10"), STAR: market(STAR, price="4")}
    result = run(
        account,
        [buy, sell],
        calendar,
        costs,
        market_rows,
        lot_rules={SYMBOL: LotRule(100, 100, 100), STAR: LotRule(200, 1, 1, sell_min_shares=200)},
    )
    assert [r.side for r in result.orders] == ["SELL", "BUY"]
    assert result.orders[0].status == "filled"
    assert result.orders[1].status == "filled" and result.orders[1].filled_shares == 200
    assert result.account.total_shares(STAR) == 200
    assert result.account.total_shares(SYMBOL) == 0
    assert result.ledger[1]["cash_before"] == result.ledger[0]["cash_delta"]
    assert result.account.cash > 0


@pytest.mark.parametrize(
    "status,reason", [("suspended", "suspended"), ("unknown", "unknown_market_status")]
)
def test_status_and_missing_open_price_are_deterministic_rejections(
    calendar, costs, status, reason
):
    result = run(
        Account(Decimal("2000")), [order()], calendar, costs, {SYMBOL: market(status=status)}
    )
    assert result.orders[0].status == "rejected" and result.orders[0].reason == reason
    result = run(Account(Decimal("2000")), [order()], calendar, costs, {SYMBOL: market(price=None)})
    assert result.orders[0].reason == "missing_open_price"


def test_limit_up_down_reject_without_intraday_inference(calendar, costs):
    buy = run(
        Account(Decimal("2000")),
        [order()],
        calendar,
        costs,
        {SYMBOL: market(limit_up=Decimal("10"))},
    )
    assert buy.orders[0].reason == "buy_at_limit_up"
    held = Account(Decimal("0"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("8"))]})
    sell = run(held, [order("SELL")], calendar, costs, {SYMBOL: market(limit_down=Decimal("10"))})
    assert sell.orders[0].reason == "sell_at_limit_down"
    slipped = run(
        Account(Decimal("2000")),
        [order()],
        calendar,
        CostModel(
            buy_slippage_bps=Decimal("20"),
            tax_schedule=(TaxRate(date(2023, 1, 1), Decimal("0.001")),),
        ),
        {SYMBOL: market(limit_up=Decimal("10.01"))},
    )
    assert slipped.orders[0].reason == "buy_at_limit_up"


def test_capacity_unknown_zero_and_partial_are_distinct(calendar, costs):
    unknown = run(
        Account(Decimal("2000")),
        [order()],
        calendar,
        costs,
        {
            SYMBOL: MarketObservation(
                SYMBOL, date(2023, 1, 4), Decimal("10"), status="open", no_price_limits=True
            )
        },
    )
    assert unknown.orders[0].reason == "unknown_open_capacity"
    zero = run(
        Account(Decimal("2000")),
        [order()],
        calendar,
        costs,
        {SYMBOL: market(open_capacity_shares=0)},
    )
    assert zero.orders[0].reason == "zero_open_capacity"
    partial = run(
        Account(Decimal("2000")),
        [order("BUY", 300)],
        calendar,
        costs,
        {SYMBOL: market(open_capacity_shares=150)},
    )
    assert partial.orders[0].status == "partial" and partial.orders[0].filled_shares == 100
    assert partial.orders[0].reason == "partial_capacity"
    lot_partial = run(Account(Decimal("5000")), [order("BUY", 150)], calendar, costs)
    assert lot_partial.orders[0].status == "partial" and lot_partial.orders[0].filled_shares == 100
    assert lot_partial.orders[0].reason == "partial_lot"


def test_board_lot_rules_allow_star_minimum_and_sell_odd_lot(calendar, costs):
    star_order = order("BUY", 200, STAR)
    result = run(
        Account(Decimal("5000")),
        [star_order],
        calendar,
        costs,
        {STAR: market(STAR, price="10")},
        lot_rules={STAR: LotRule(200, 1, 1, sell_min_shares=200)},
    )
    assert result.orders[0].status == "filled" and result.account.total_shares(STAR) == 200
    account = Account(Decimal("0"), {STAR: [HoldingLot(201, date(2023, 1, 1), Decimal("10"))]})
    sell = run(
        account,
        [order("SELL", 201, STAR)],
        calendar,
        costs,
        {STAR: market(STAR, price="10")},
        lot_rules={STAR: LotRule(200, 1, 1, sell_min_shares=200)},
    )
    assert sell.orders[0].status == "filled" and sell.orders[0].filled_shares == 201


def test_costs_minimum_stamp_date_and_cash_rejection(calendar, costs):
    result = run(Account(Decimal("1000")), [order("BUY", 100)], calendar, costs)
    assert result.orders[0].reason == "insufficient_cash"
    account = Account(Decimal("0"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("8"))]})
    result = run(account, [order("SELL", 100)], calendar, costs)
    assert result.orders[0].stamp_tax > 0 and result.orders[0].commission >= Decimal("5")


def test_unknown_corporate_action_rejects_without_mutating_account(calendar, costs):
    account = Account(Decimal("2000"))
    result = run(
        account,
        [order()],
        calendar,
        costs,
        guard=ActionGuard(date(2023, 1, 4), known_no_actions=False, unresolved_symbols=(SYMBOL,)),
    )
    assert result.orders[0].reason == "unresolved_company_action"
    assert result.account.cash == Decimal("2000.00")


def test_non_synthetic_inputs_are_closed_at_boundary(calendar, costs):
    with pytest.raises(ExecutionInputError, match="synthetic"):
        Account(Decimal("1"), data_kind="real_candidate")
    with pytest.raises(ExecutionInputError, match="synthetic"):
        execute_orders(
            Account(Decimal("2000")),
            [order()],
            {SYMBOL: market()},
            calendar,
            costs,
            rules(),
            ActionGuard(date(2023, 1, 4), data_kind="real_candidate"),
        )


@pytest.mark.parametrize("entrypoint", ["signal", "valuation"])
def test_mutated_account_provenance_cannot_be_reset_by_cloning(calendar, entrypoint):
    account = Account(Decimal("1000"))
    account.data_kind = "real_candidate"
    args = signal_kwargs(calendar, account)
    with pytest.raises(ExecutionInputError, match="synthetic"):
        if entrypoint == "signal":
            build_equal_weight_orders(**args)
        else:
            mark_to_market(
                account,
                args["close_observations"],
                args["signal_date"],
                asof=args["signal_time"],
            )
    assert account.data_kind == "real_candidate"


def test_fullwidth_digits_are_rejected_in_symbols():
    with pytest.raises(ExecutionInputError, match="symbol"):
        order(symbol="\uff10\uff10\uff10\uff10\uff10\uff11.SZ")


def test_equal_weight_requires_signal_time_visible_prices_and_next_open(calendar):
    account = Account(Decimal("1000"))
    signal_time = datetime(2023, 1, 3, 15, 1, tzinfo=timezone.utc)
    observations = {SYMBOL: CloseObservation(SYMBOL, date(2023, 1, 3), Decimal("10"), signal_time)}
    orders = build_equal_weight_orders(
        account=account,
        universe=(SYMBOL,),
        close_observations=observations,
        signal_date=date(2023, 1, 3),
        signal_time=signal_time,
        execution_date=date(2023, 1, 4),
        calendar=calendar,
        lot_rules=rules(),
    )
    assert orders[0].side == "BUY" and orders[0].submitted_date == date(2023, 1, 3)
    future = dict(observations)
    future[SYMBOL] = CloseObservation(
        SYMBOL, date(2023, 1, 3), Decimal("10"), datetime(2023, 1, 4, 9, 0, tzinfo=timezone.utc)
    )
    with pytest.raises(ExecutionInputError, match="future"):
        build_equal_weight_orders(
            account=account,
            universe=(SYMBOL,),
            close_observations=future,
            signal_date=date(2023, 1, 3),
            signal_time=signal_time,
            execution_date=date(2023, 1, 4),
            calendar=calendar,
            lot_rules=rules(),
        )


def test_equal_weight_missing_held_close_is_not_zero_filled(calendar):
    account = Account(Decimal("100"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("1"))]})
    with pytest.raises(ExecutionInputError, match="missing signal close"):
        build_equal_weight_orders(
            account=account,
            universe=("688001.SH",),
            close_observations={},
            signal_date=date(2023, 1, 3),
            signal_time=datetime(2023, 1, 3, 15, tzinfo=timezone.utc),
            execution_date=date(2023, 1, 4),
            calendar=calendar,
            lot_rules={},
        )


def test_mark_to_market_rejects_missing_or_future_close_and_does_not_zero_fill():
    account = Account(Decimal("100"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("1"))]})
    close = CloseObservation(
        SYMBOL, date(2023, 1, 3), Decimal("10"), datetime(2023, 1, 3, 7, tzinfo=timezone.utc)
    )
    assert mark_to_market(
        account,
        {SYMBOL: close},
        date(2023, 1, 3),
        asof=datetime(2023, 1, 3, 8, tzinfo=timezone.utc),
    ) == Decimal("1100.00")
    with pytest.raises(ExecutionInputError, match="missing close"):
        mark_to_market(
            account, {}, date(2023, 1, 3), asof=datetime(2023, 1, 3, 8, tzinfo=timezone.utc)
        )
    future = CloseObservation(
        SYMBOL, date(2023, 1, 3), Decimal("10"), datetime(2023, 1, 3, 8, tzinfo=timezone.utc)
    )
    with pytest.raises(ExecutionInputError, match="future"):
        mark_to_market(
            account,
            {SYMBOL: future},
            date(2023, 1, 3),
            asof=datetime(2023, 1, 3, 7, tzinfo=timezone.utc),
        )


def test_five_day_rebalance_pairs_use_explicit_calendar(calendar):
    pairs = five_day_rebalance_pairs(calendar, date(2023, 1, 3), date(2023, 1, 16))
    assert pairs == ((date(2023, 1, 3), date(2023, 1, 4)), (date(2023, 1, 10), date(2023, 1, 11)))


@pytest.mark.parametrize(
    "bad",
    [
        ("unsorted", (date(2023, 1, 4), date(2023, 1, 3))),
        ("duplicate", (date(2023, 1, 3), date(2023, 1, 3))),
    ],
)
def test_calendar_requires_explicit_order(bad):
    with pytest.raises(ExecutionInputError):
        ExecutionCalendar(bad[1])


@pytest.mark.parametrize(
    "submitted,executed", [("2023-01-02", "2023-01-04"), ("2023-01-03", "2023-01-05")]
)
def test_regression_direct_execution_requires_next_explicit_trading_date(
    calendar, costs, submitted, executed
):
    o = order(submitted=submitted, execute=executed)
    with pytest.raises(ExecutionInputError, match="calendar|next trading"):
        run(
            Account(Decimal("5000")),
            [o],
            calendar,
            costs,
            {SYMBOL: replace(market(), event_date=o.execute_date)},
            guard=ActionGuard(o.execute_date, known_no_actions=True),
        )


def test_regression_duplicate_or_empty_order_identity_rejected(calendar, costs):
    with pytest.raises(ExecutionInputError, match="order_id"):
        run(Account(Decimal("5000")), [order(), order()], calendar, costs)
    with pytest.raises(ExecutionInputError, match="order_id"):
        run(Account(Decimal("5000")), [replace(order(), order_id=" ")], calendar, costs)


@pytest.mark.parametrize("first_side", ["BUY", "SELL"])
def test_regression_open_capacity_is_shared_across_orders_and_sides(calendar, costs, first_side):
    account = Account(Decimal("5000"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("9"))]})
    first = replace(order(first_side), order_id="first")
    second = replace(order(), order_id="second")
    result = run(
        account, [first, second], calendar, costs, {SYMBOL: market(open_capacity_shares=100)}
    )
    assert sum(r.filled_shares for r in result.orders) == 100
    assert result.orders[1].reason == "zero_open_capacity"


def test_regression_market_mapping_identity_is_validated(calendar, costs):
    with pytest.raises(ExecutionInputError, match="symbol"):
        run(Account(Decimal("5000")), [order()], calendar, costs, {SYMBOL: market(STAR)})


def signal_kwargs(calendar, account, universe=(SYMBOL,), closes=None, lot_rules=None):
    signal_time = datetime(2023, 1, 3, 8, tzinfo=timezone.utc)
    if closes is None:
        closes = {
            s: CloseObservation(s, date(2023, 1, 3), Decimal("10"), signal_time) for s in universe
        }
    return dict(
        account=account,
        universe=universe,
        close_observations=closes,
        signal_date=date(2023, 1, 3),
        signal_time=signal_time,
        execution_date=date(2023, 1, 4),
        calendar=calendar,
        lot_rules=rules() if lot_rules is None else lot_rules,
    )


def test_regression_close_mapping_identity_is_validated_for_signal_and_nav(calendar):
    account = Account(Decimal("500"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("9"))]})
    wrong = {
        SYMBOL: CloseObservation(
            STAR, date(2023, 1, 3), Decimal("10"), datetime(2023, 1, 3, 8, tzinfo=timezone.utc)
        )
    }
    with pytest.raises(ExecutionInputError, match="symbol"):
        build_equal_weight_orders(**signal_kwargs(calendar, account, closes=wrong))
    with pytest.raises(ExecutionInputError, match="symbol"):
        mark_to_market(
            account, wrong, date(2023, 1, 3), asof=datetime(2023, 1, 3, 8, tzinfo=timezone.utc)
        )


def test_regression_liquidation_removes_empty_positions_and_closes_not_required(calendar, costs):
    account = Account(Decimal("0"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("9"))]})
    result = run(account, [order("SELL")], calendar, costs)
    assert SYMBOL not in result.account.lots
    assert (
        mark_to_market(
            result.account, {}, date(2023, 1, 4), asof=datetime(2023, 1, 4, 8, tzinfo=timezone.utc)
        )
        == result.account.cash
    )


def test_regression_valuation_requires_explicit_asof():
    with pytest.raises((ExecutionInputError, TypeError), match="asof"):
        mark_to_market(Account(Decimal("10")), {}, date(2023, 1, 3))


def test_regression_clock_uses_shanghai_date_and_after_close(calendar):
    args = signal_kwargs(calendar, Account(Decimal("1000")))
    args["signal_time"] = datetime(2023, 1, 3, 6, 59, tzinfo=timezone.utc)
    args["close_observations"] = {}
    with pytest.raises(ExecutionInputError, match="15:00|close"):
        build_equal_weight_orders(**args)
    args = signal_kwargs(calendar, Account(Decimal("1000")))
    args["signal_time"] = datetime(2023, 1, 3, 17, tzinfo=timezone.utc)  # Shanghai January 4.
    with pytest.raises(ExecutionInputError, match="signal.*date"):
        build_equal_weight_orders(**args)
    with pytest.raises(ExecutionInputError, match="date"):
        mark_to_market(
            Account(Decimal("1000")),
            {},
            date(2023, 1, 3),
            asof=datetime(2023, 1, 3, 17, tzinfo=timezone.utc),
        )


def test_regression_close_cannot_be_available_before_exchange_close():
    with pytest.raises(ExecutionInputError, match="close|15:00"):
        CloseObservation(
            SYMBOL,
            date(2023, 1, 3),
            Decimal("10"),
            datetime(2023, 1, 3, 6, 59, tzinfo=timezone.utc),
        )


def test_regression_equal_weight_rounds_difference_not_total_target(calendar):
    account = Account(Decimal("450"), {STAR: [HoldingLot(150, date(2023, 1, 1), Decimal("10"))]})
    args = signal_kwargs(calendar, account, universe=(STAR,), lot_rules={STAR: LotRule(200, 1, 1)})
    assert (
        build_equal_weight_orders(**args) == ()
    )  # target 195; increment 45 cannot meet buy minimum.


def test_regression_exiting_holding_also_needs_explicit_lot_rule(calendar):
    account = Account(Decimal("450"), {STAR: [HoldingLot(150, date(2023, 1, 1), Decimal("10"))]})
    closes = {
        s: CloseObservation(
            s, date(2023, 1, 3), Decimal("10"), datetime(2023, 1, 3, 8, tzinfo=timezone.utc)
        )
        for s in (SYMBOL, STAR)
    }
    with pytest.raises(ExecutionInputError, match="lot rule"):
        build_equal_weight_orders(**signal_kwargs(calendar, account, closes=closes))


def test_regression_final_cent_rounding_cannot_reach_disallowed_limit(calendar):
    no_cost = CostModel(
        commission_rate=Decimal("0"), commission_minimum=Decimal("0"), buy_slippage_bps=Decimal("6")
    )
    result = run(
        Account(Decimal("5000")),
        [order()],
        calendar,
        no_cost,
        {SYMBOL: market(limit_up=Decimal("10.01"), limit_down=Decimal("9"))},
    )
    assert result.orders[0].reason == "buy_at_limit_up"  # 10.006 rounds to 10.01.


def test_regression_invalid_daily_limits_rejected():
    with pytest.raises(ExecutionInputError, match="limit"):
        market(limit_up=Decimal("9"), limit_down=Decimal("10"))


def test_regression_absent_limits_do_not_mean_unlimited(calendar, costs):
    result = run(
        Account(Decimal("5000")),
        [order()],
        calendar,
        costs,
        {
            SYMBOL: MarketObservation(
                SYMBOL, date(2023, 1, 4), Decimal("10"), status="open", open_capacity_shares=1000
            )
        },
    )
    assert result.orders[0].reason == "unknown_price_limits"


def test_regression_default_market_and_action_status_are_unknown(calendar, costs):
    assert MarketObservation(SYMBOL, date(2023, 1, 4), Decimal("10")).status == "unknown"
    guard = ActionGuard(date(2023, 1, 4))
    assert not guard.known_no_actions
    result = run(Account(Decimal("5000")), [order()], calendar, costs, guard=guard)
    assert result.orders[0].reason == "unresolved_company_action"


def test_regression_tiny_sale_cannot_make_cash_negative(calendar, costs):
    account = Account(Decimal("0"), {SYMBOL: [HoldingLot(1, date(2023, 1, 1), Decimal("1"))]})
    result = run(account, [order("SELL", 1)], calendar, costs, {SYMBOL: market(price="1")})
    assert result.orders[0].status == "rejected"
    assert result.orders[0].reason == "insufficient_cash_for_fees"
    assert result.account.cash == Decimal("0.00") and result.account.total_shares(SYMBOL) == 1


def test_regression_cash_binds_below_open_capacity_and_partial_counts_as_fill(calendar, costs):
    result = run(
        Account(Decimal("1100")),
        [order("BUY", 500)],
        calendar,
        costs,
        {SYMBOL: market(open_capacity_shares=300)},
    )
    assert result.orders[0].filled_shares == 100 and result.orders[0].reason == "partial_cash"
    assert result.filled == result.orders


@pytest.mark.parametrize("field", ["lot", "capacity", "account"])
def test_regression_invalid_holdings_and_bool_shares_are_rejected(field):
    with pytest.raises(ExecutionInputError):
        if field == "lot":
            HoldingLot(True, date(2023, 1, 1), Decimal("10"))
        elif field == "capacity":
            market(open_capacity_shares=True)
        else:
            Account(Decimal("10"), {SYMBOL: [object()]})


@pytest.mark.parametrize("side,notional", [("SHORT", Decimal("100")), ("SELL", Decimal("-10"))])
def test_regression_fee_input_validation(costs, side, notional):
    with pytest.raises(ExecutionInputError):
        costs.fees(date(2023, 1, 4), side, notional)


def test_regression_sell_slippage_cannot_remove_entire_price():
    with pytest.raises(ExecutionInputError, match="slippage"):
        CostModel(sell_slippage_bps=Decimal("10000"))


@pytest.mark.parametrize(
    "minimum,step,available,requested,expected",
    [
        (100, 100, 250, 150, 100),
        (100, 100, 150, 150, 150),
        (100, 100, 50, 50, 50),
        (100, 100, 50, 25, 0),
        (200, 1, 400, 199, 0),
        (200, 1, 400, 201, 201),
        (200, 1, 150, 100, 0),
        (200, 1, 150, 150, 150),
    ],
)
def test_explicit_sell_minimum_increment_and_whole_available_odd_lot(
    calendar, costs, minimum, step, available, requested, expected
):
    rule = LotRule(minimum, step, step, sell_min_shares=minimum)
    account = Account(
        Decimal("10"), {STAR: [HoldingLot(available, date(2023, 1, 1), Decimal("10"))]}
    )
    result = run(
        account,
        [order("SELL", requested, STAR)],
        calendar,
        costs,
        {STAR: market(STAR)},
        lot_rules={STAR: rule},
    )
    assert result.orders[0].filled_shares == expected
    assert result.account.total_shares(STAR) == available - expected
    if expected == 0:
        assert result.orders[0].reason == "below_sell_minimum"


def test_mixed_t1_lots_sell_only_available_shares_and_preserve_future_lot(calendar, costs):
    unlocked = HoldingLot(150, date(2023, 1, 1), Decimal("10"))
    locked = HoldingLot(200, date(2023, 1, 5), Decimal("11"))
    account = Account(Decimal("10"), {STAR: [locked, unlocked]})
    result = run(
        account,
        [order("SELL", 350, STAR)],
        calendar,
        costs,
        {STAR: market(STAR)},
        lot_rules={STAR: LotRule(200, 1, 1, sell_min_shares=200)},
    )
    assert result.orders[0].filled_shares == 150
    assert result.orders[0].reason == "partial_available_shares"
    assert result.account.lots[STAR] == [locked]
    assert account.lots[STAR] == [locked, unlocked]
    assert result.account.available_shares(STAR, date(2023, 1, 4)) == 0
    assert result.account.available_shares(STAR, date(2023, 1, 5)) == 200


def test_equal_weight_delta_lot_rounding_keeps_existing_odd_lot(calendar):
    account = Account(Decimal("3000"), {SYMBOL: [HoldingLot(150, date(2023, 1, 1), Decimal("10"))]})
    orders = build_equal_weight_orders(**signal_kwargs(calendar, account))
    assert len(orders) == 1 and orders[0].side == "BUY" and orders[0].shares == 300
    empty = Account(Decimal("1000"), {STAR: []})
    assert STAR not in empty.lots
    assert build_equal_weight_orders(**signal_kwargs(calendar, empty))[0].symbol == SYMBOL


def test_stamp_tax_changes_on_configured_effective_trade_date(costs):
    friday, monday = date(2023, 8, 25), date(2023, 8, 28)
    calendar = ExecutionCalendar((date(2023, 8, 24), friday, monday, date(2023, 8, 29)))
    seen = []
    for submitted, executed in ((date(2023, 8, 24), friday), (friday, monday)):
        account = Account(
            Decimal("0"), {SYMBOL: [HoldingLot(1000, date(2023, 1, 1), Decimal("8"))]}
        )
        orders = [Order("tax-sale", SYMBOL, "SELL", 1000, submitted, executed)]
        result = run(
            account,
            orders,
            calendar,
            replace(costs, sell_slippage_bps=Decimal("0")),
            {SYMBOL: replace(market(), event_date=executed)},
            guard=ActionGuard(executed, known_no_actions=True),
        )
        seen.append(result.orders[0].stamp_tax)
    assert seen == [Decimal("10.00"), Decimal("5.00")]
    assert costs.fees(monday, "BUY", Decimal("10000"))["stamp_tax"] == 0


def test_decimal_half_up_rounding_is_applied_to_execution_and_each_fee(calendar):
    costs = CostModel(
        commission_rate=Decimal("0.0005"),
        commission_minimum=Decimal("0"),
        transfer_fee_rate=Decimal("0.0005"),
        buy_slippage_bps=Decimal("5"),
        tax_schedule=(TaxRate(date(2023, 1, 1), Decimal("0.0005")),),
    )
    assert costs.fees(date(2023, 1, 4), "SELL", Decimal("10")) == {
        "commission": Decimal("0.01"),
        "transfer_fee": Decimal("0.01"),
        "stamp_tax": Decimal("0.01"),
    }
    result = run(Account(Decimal("5000")), [order()], calendar, costs)
    assert result.orders[0].execution_price == Decimal("10.01")  # 10.005 -> 10.01.
    assert result.orders[0].gross_notional == Decimal("1001.00")
    assert result.orders[0].commission == Decimal("0.50")
    assert result.orders[0].transfer_fee == Decimal("0.50")
    assert result.account.cash == Decimal("3998.00")


def test_sell_rounding_into_lower_limit_is_rejected(calendar):
    held = Account(Decimal("10"), {SYMBOL: [HoldingLot(100, date(2023, 1, 1), Decimal("9"))]})
    costs = CostModel(sell_slippage_bps=Decimal("6"))
    result = run(
        held,
        [order("SELL")],
        calendar,
        costs,
        {SYMBOL: market(limit_up=Decimal("11"), limit_down=Decimal("9.99"))},
    )
    assert result.orders[0].reason == "sell_at_limit_down"  # 9.994 -> 9.99.


def test_participation_cap_is_floored_once_and_rejected_order_does_not_consume_it(calendar, costs):
    requested = [
        replace(order(shares=50), order_id="below-min"),
        replace(order(shares=300), order_id="first-fill"),
        replace(order(), order_id="after-cap"),
    ]
    result = execute_orders(
        Account(Decimal("5000")),
        requested,
        {SYMBOL: market(open_capacity_shares=999)},
        calendar,
        costs,
        rules(),
        ActionGuard(date(2023, 1, 4), known_no_actions=True),
        participation_rate=Decimal("0.2"),
    )
    assert [r.filled_shares for r in result.orders] == [0, 100, 0]
    assert result.ledger[0]["remaining_open_capacity"] == 99


def test_unresolved_action_on_existing_holding_blocks_other_orders(calendar, costs):
    account = Account(Decimal("3000"), {STAR: [HoldingLot(200, date(2023, 1, 1), Decimal("9"))]})
    result = run(
        account,
        [order()],
        calendar,
        costs,
        guard=ActionGuard(date(2023, 1, 4), known_no_actions=True, unresolved_symbols=(STAR,)),
    )
    assert result.orders[0].reason == "unresolved_company_action"
    assert result.account == account and not result.ledger


def test_explicit_no_price_limit_override_is_not_combined_with_limits():
    with pytest.raises(ExecutionInputError, match="conflicts"):
        market(limit_up=Decimal("11"), limit_down=Decimal("9"), no_price_limits=True)


@pytest.mark.parametrize(
    "case",
    [
        "sell_min_bool",
        "buy_step_bool",
        "minimum_fee",
        "fee_rate",
        "tax_rate",
        "tax_object",
        "buy_slip",
        "zero_close",
        "naive_close",
        "account_scalar",
    ],
)
def test_additional_invalid_inputs_fail_closed(case):
    with pytest.raises(ExecutionInputError):
        if case == "sell_min_bool":
            LotRule(100, 100, 100, sell_min_shares=True)
        elif case == "buy_step_bool":
            LotRule(100, True, 100)
        elif case == "minimum_fee":
            CostModel(commission_minimum=Decimal("-0.001"))
        elif case == "fee_rate":
            CostModel(transfer_fee_rate=Decimal("NaN"))
        elif case == "tax_rate":
            TaxRate(date(2023, 1, 1), Decimal("1"))
        elif case == "tax_object":
            CostModel(tax_schedule=(object(),))
        elif case == "buy_slip":
            CostModel(buy_slippage_bps=Decimal("10000"))
        elif case == "zero_close":
            CloseObservation(
                SYMBOL, date(2023, 1, 3), Decimal("0"), datetime(2023, 1, 3, 8, tzinfo=timezone.utc)
            )
        elif case == "naive_close":
            CloseObservation(SYMBOL, date(2023, 1, 3), Decimal("10"), datetime(2023, 1, 3, 15))
        else:
            Account(Decimal("10"), {SYMBOL: 5})


def test_shanghai_clock_accepts_equal_instants_in_other_timezones(calendar):
    args = signal_kwargs(calendar, Account(Decimal("1000")))
    args["signal_time"] = datetime(2023, 1, 3, 18, tzinfo=timezone(timedelta(hours=10)))
    assert build_equal_weight_orders(**args)[0].shares == 100
