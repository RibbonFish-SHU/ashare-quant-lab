"""Short, hand-reconcilable paths for the real-price diagnostic simulator."""

from copy import deepcopy
from datetime import date, timedelta
import json
import math

import numpy as np
import pytest

from ashare_lab.diagnostic_backtest import DiagnosticBacktestError, run_diagnostic_backtest


SYMBOLS = ["000001.SZ", "600001.SH"]


def inputs(n=7, k=2):
    return {
        "dates": [date(2023, 1, 3) + timedelta(days=i) for i in range(n)],
        "symbols": SYMBOLS[:k],
        "adjusted_open": np.tile([10.0, 20.0][:k], (n, 1)),
        "adjusted_close": np.tile([10.0, 20.0][:k], (n, 1)),
        "membership": np.ones((n, k), dtype=bool),
        "full_day_suspended": np.zeros((n, k), dtype=bool),
    }


def run(data, **config):
    return run_diagnostic_backtest(**data, config={"initial_cash": 1000, **config})


def test_next_open_execution_and_future_open_cannot_change_frozen_signal():
    original = inputs(k=1)
    changed = deepcopy(original)
    changed["adjusted_open"][1, 0] = 20
    first, second = run(original), run(changed)
    assert first["signals"][0] == second["signals"][0]
    assert first["orders"][0]["requested_units"] == second["orders"][0]["requested_units"] == 99
    assert first["trades"][0]["execution_date"] == original["dates"][1].isoformat()
    assert second["trades"][0]["units"] == pytest.approx(50)
    assert second["orders"][0]["status"] == "partial"
    assert first["daily"][0]["cash"] == first["daily"][0]["nav"] == 1000


def test_signal_frequency_is_zero_five_ten_and_exit_waits_for_rebalance():
    data = inputs(n=12, k=1)
    data["membership"][2:8] = False
    result = run(data)
    assert [s["date"] for s in result["signals"]] == [
        data["dates"][i].isoformat() for i in (0, 5, 10)
    ]
    assert [(t["execution_date"], t["side"]) for t in result["trades"]] == [
        (data["dates"][1].isoformat(), "BUY"),
        (data["dates"][6].isoformat(), "SELL"),
        (data["dates"][11].isoformat(), "BUY"),
    ]
    assert result["daily"][5]["holding_count"] == 1
    assert result["trades"][0]["sellable_from"] == data["dates"][2].isoformat()


def test_sells_precede_buys_and_fees_reduce_fills_without_borrowing():
    data = inputs()
    data["membership"][:5] = [False, True]
    data["membership"][5:] = [True, False]
    result = run(data, commission_minimum=10)
    day_six = [t for t in result["trades"] if t["execution_date"] == data["dates"][6].isoformat()]
    assert [(t["symbol"], t["side"]) for t in day_six] == [
        (SYMBOLS[1], "SELL"),
        (SYMBOLS[0], "BUY"),
    ]
    assert all(t["cash_after"] >= 0 for t in result["trades"])
    buy = next(order for order in result["orders"] if order["symbol"] == SYMBOLS[0])
    assert buy["status"] == "partial"
    assert buy["reason"] == "insufficient_cash"
    assert result["daily"][-1]["nav"] == pytest.approx(970)
    assert result["costs"]["commission"] == 30


def test_stable_symbol_priority_for_cash_limited_buys():
    data = inputs(n=2)
    data["adjusted_open"][1] *= 3
    result = run(data)
    assert [t["symbol"] for t in result["trades"]] == SYMBOLS[:1]
    assert result["orders"][1]["status"] == "unfilled"
    assert result["orders"][1]["reason"] == "insufficient_cash"


def test_cost_categories_slippage_and_effective_stamp_tax():
    data = inputs(n=12)
    data["dates"] = [
        date.fromisoformat(d)
        for d in (
            "2023-08-17",
            "2023-08-18",
            "2023-08-21",
            "2023-08-22",
            "2023-08-23",
            "2023-08-24",
            "2023-08-25",
            "2023-08-28",
            "2023-08-29",
            "2023-08-30",
            "2023-08-31",
            "2023-09-01",
        )
    ]
    data["membership"][:5] = [True, False]
    data["membership"][5:10] = [False, True]
    data["membership"][10:] = [True, False]
    result = run(
        data,
        commission_rate=0.001,
        commission_minimum=1,
        transfer_fee_rate=0.0001,
        buy_slippage_bps=10,
        sell_slippage_bps=20,
        stamp_tax_schedule=[
            {"effective_date": "2023-01-01", "rate": 0.001},
            {"effective_date": "2023-08-28", "rate": 0.0005},
        ],
    )
    for trade in result["trades"]:
        notional = trade["notional"]
        assert trade["commission"] == pytest.approx(max(notional * 0.001, 1))
        assert trade["transfer_fee"] == pytest.approx(notional * 0.0001)
        rate = 0.001 if trade["execution_date"] < "2023-08-28" else 0.0005
        assert trade["stamp_tax"] == pytest.approx(
            notional * rate if trade["side"] == "SELL" else 0
        )
        assert trade["slippage_cost"] == pytest.approx(
            trade["units"] * trade["adjusted_open"] * (0.001 if trade["side"] == "BUY" else 0.002)
        )
    assert len([t for t in result["trades"] if t["side"] == "SELL"]) == 2
    for name in ("commission", "transfer_fee", "stamp_tax"):
        assert result["costs"][name] == pytest.approx(sum(t[name] for t in result["trades"]))
    assert result["costs"]["including_slippage"] == pytest.approx(
        result["costs"]["explicit_fees"] + result["costs"]["slippage"]
    )


@pytest.mark.parametrize("blocked_by", ["full_day_suspension", "missing_open"])
def test_blocked_open_is_not_fabricated_or_retried_before_next_signal(blocked_by):
    data = inputs(k=1)
    if blocked_by == "full_day_suspension":
        data["full_day_suspended"][1, 0] = True
    else:
        data["adjusted_open"][1, 0] = np.nan
    result = run(data)
    assert result["orders"][0]["reason"] == blocked_by
    assert result["orders"][0]["filled_units"] == 0
    assert len(result["trades"]) == 1
    assert result["trades"][0]["execution_date"] == data["dates"][6].isoformat()


def test_known_suspension_carries_mark_and_keeps_departed_holding_until_executed():
    data = inputs(k=1)
    data["membership"][2:] = False
    data["adjusted_close"][2:6, 0] = np.nan
    data["adjusted_open"][2:6, 0] = np.nan
    data["full_day_suspended"][2:6, 0] = True
    original = data["adjusted_close"].copy()
    result = run(data)
    for row in result["daily"][2:6]:
        assert row["nav"] == pytest.approx(1000)
        assert row["stale_count"] == 1 and row["stale_symbols"] == SYMBOLS[:1]
    assert result["daily"][6]["holding_count"] == 0
    assert len(result["trades"]) == 2
    np.testing.assert_array_equal(data["adjusted_close"], original)


def test_unexplained_missing_close_fails_even_after_security_leaves_membership():
    data = inputs(k=1)
    data["membership"][2:] = False
    data["adjusted_close"][2, 0] = np.nan
    with pytest.raises(DiagnosticBacktestError, match="unexplained missing held close.*000001"):
        run(data)


def test_missing_unheld_target_does_not_drop_day_or_redistribute_allocation():
    data = inputs(n=2)
    data["adjusted_close"][0, 1] = np.nan
    result = run(data)
    assert len(result["daily"]) == 2
    assert result["signals"][0]["member_count"] == 2
    assert result["signals"][0]["rejected_targets"][0]["symbol"] == SYMBOLS[1]
    assert result["trades"][0]["units"] == 49.5
    assert result["daily"][-1]["cash"] == 505


def test_daily_conservation_metrics_and_no_terminal_liquidation():
    data = inputs(n=5, k=1)
    data["adjusted_close"][:, 0] = [10, 11, 9, 12, 10]
    result = run(data)
    expected_nav = np.array([1000, 1099, 901, 1198, 1000], dtype=float)
    np.testing.assert_allclose([d["nav"] for d in result["daily"]], expected_nav)
    returns = expected_nav[1:] / expected_nav[:-1] - 1
    for row in result["daily"]:
        assert row["nav"] == pytest.approx(row["cash"] + row["holdings_market_value"])
    metrics = result["metrics"]
    assert metrics["total_return"] == metrics["annualized_return"] == 0
    assert metrics["annualized_volatility"] == pytest.approx(
        np.std(returns, ddof=1) * math.sqrt(252)
    )
    assert metrics["sharpe_rf0"] == pytest.approx(
        np.mean(returns) / np.std(returns, ddof=1) * math.sqrt(252)
    )
    assert metrics["max_drawdown"] == pytest.approx(901 / 1099 - 1)
    assert metrics["cumulative_turnover"] == pytest.approx(0.99)
    assert metrics["final_holding_count"] == 1
    assert metrics["final_adjusted_units"] == 99
    assert len(result["trades"]) == 1 and result["trades"][0]["side"] == "BUY"


def test_serializable_output_and_all_caller_inputs_remain_unchanged():
    data = inputs()
    before = deepcopy(data)
    config = {
        "initial_cash": 1000,
        "stamp_tax_schedule": [{"effective_date": "2000-01-01", "rate": 0.001}],
    }
    before_config = deepcopy(config)
    result = run_diagnostic_backtest(**data, config=config)
    json.dumps(result, allow_nan=False)
    assert result["metadata"]["data_kind"] == "real_diagnostic"
    assert result["metadata"]["research_eligible"] is False
    for key in ("adjusted_open", "adjusted_close", "membership", "full_day_suspended"):
        np.testing.assert_array_equal(data[key], before[key])
    assert config == before_config


def test_last_close_signal_is_recorded_without_a_fake_execution():
    result = run(inputs(n=1, k=1))
    assert result["orders"][0]["status"] == "expired_end_of_sample"
    assert result["trades"] == []
    assert result["metrics"]["annualized_return"] is None
    assert result["metrics"]["sharpe_rf0"] is None
