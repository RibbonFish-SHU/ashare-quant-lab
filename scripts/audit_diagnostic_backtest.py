"""Independently reconcile persisted fills, frozen signals and end-of-day NAV."""

from collections import Counter
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


def close(actual, expected, name):
    if not np.allclose(actual, expected, atol=1e-6, rtol=1e-11):
        raise AssertionError(name)


def audit(output):
    config = json.loads((output / "frozen_config.json").read_text(encoding="utf-8"))
    arrays = np.load(output / "diagnostic_inputs.npz", allow_pickle=False)
    dates, symbols = arrays["dates"].tolist(), arrays["symbols"].tolist()
    opens, closes = arrays["adjusted_open"], arrays["adjusted_close"]
    members, halts = arrays["membership"], arrays["full_day_suspended"]
    date_column = {d: i for i, d in enumerate(dates)}
    symbol_column = {s: j for j, s in enumerate(symbols)}
    capital = config["strategy"]["initial_cash"]
    expected_signals = dates[:: config["strategy"]["rebalance_every"]]
    marks = pd.DataFrame(closes).ffill().to_numpy()
    answer = {}
    for scenario in config["scenarios"]:
        name = scenario["scenario_name"]
        root = output / name
        daily = pq.read_table(root / "daily.parquet").to_pandas()
        orders = pq.read_table(root / "orders.parquet").to_pylist()
        fills = pq.read_table(root / "trades.parquet").to_pylist()
        signals = pq.read_table(root / "signals.parquet").to_pylist()
        summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        assert daily.date.tolist() == dates
        assert [s["date"] for s in signals] == expected_signals
        assert all(
            len({f["symbol"] for f in fills if f["execution_date"] == d})
            == sum(f["execution_date"] == d for f in fills)
            for d in dates
        )
        by_id = {o["order_id"]: o for o in orders}
        assert len(by_id) == len(orders)
        delta_units = np.zeros(opens.shape)
        delta_cash = np.zeros(len(dates))
        traded_notional = np.zeros(len(dates))
        running_cash = np.longdouble(capital)
        fees = Counter()
        trade_days = set()
        cash_errors = []
        previous_sort_key = (-1, -1, "")
        for fill in fills:
            i = date_column[fill["execution_date"]]
            j = symbol_column[fill["symbol"]]
            signal_i = date_column[fill["signal_date"]]
            assert i == signal_i + 1 and signal_i % config["strategy"]["rebalance_every"] == 0
            assert not halts[i, j] and np.isfinite(opens[i, j])
            buy = fill["side"] == "BUY"
            key = (i, int(buy), fill["symbol"])
            assert key > previous_sort_key
            previous_sort_key = key
            slip = scenario["buy_slippage_bps" if buy else "sell_slippage_bps"] / 10000
            price = opens[i, j] * (1 + slip if buy else 1 - slip)
            close(fill["execution_price"], price, "execution price")
            notional = fill["units"] * price
            close(fill["notional"], notional, "notional")
            rate = next(
                s["rate"]
                for s in reversed(scenario["stamp_tax_schedule"])
                if s["effective_date"] <= dates[i]
            )
            expected_fee = {
                "commission": max(
                    notional * scenario["commission_rate"], scenario["commission_minimum"]
                ),
                "transfer_fee": notional * scenario["transfer_fee_rate"],
                "stamp_tax": 0 if buy else notional * rate,
                "slippage": abs(price - opens[i, j]) * fill["units"],
            }
            for field, amount in expected_fee.items():
                close(fill["slippage_cost" if field == "slippage" else field], amount, field)
                fees[field] += amount
            explicit = sum(v for k, v in expected_fee.items() if k != "slippage")
            change = (-notional if buy else notional) - explicit
            delta_cash[i] += change
            delta_units[i, j] += fill["units"] * (1 if buy else -1)
            traded_notional[i] += notional
            running_cash += np.longdouble(change)
            close(fill["cash_after"], float(running_cash), "cash after each fill")
            cash_errors.append(abs(fill["cash_after"] - float(running_cash)))
            assert fill["cash_after"] >= 0
            close(by_id[fill["order_id"]]["filled_units"], fill["units"], "order fill units")
            assert fill["units"] <= by_id[fill["order_id"]]["requested_units"] * (1 + 1e-12)
            trade_days.add(dates[i])
        positions = delta_units.cumsum(axis=0)
        assert positions.min() >= -1e-8
        missing_held = (positions > 0) & ~np.isfinite(closes)
        assert np.all(~missing_held | halts)
        assert np.all(~missing_held | np.isfinite(marks))
        # Only held quotes are used, after independently checking every stale mark above.
        market_value = np.where(positions > 0, positions * marks, 0).sum(axis=1)
        cash = capital + delta_cash.cumsum()
        nav = cash + market_value
        close(daily.cash, cash, "daily cash")
        close(daily.holdings_market_value, market_value, "daily held valuation")
        close(daily.nav, nav, "daily NAV")
        assert daily.stale_count.tolist() == missing_held.sum(axis=1).tolist()
        for signal in signals:
            i = date_column[signal["date"]]
            close(signal["nav"], nav[i], "signal NAV")
            assert signal["member_count"] == int(members[i].sum())
            allocation = nav[i] * config["strategy"]["invest_fraction"] / members[i].sum()
            close(signal["allocation_per_member"], allocation, "equal weight allocation")
            for target in signal["targets"]:
                j = symbol_column[target["symbol"]]
                expected = allocation / closes[i, j] if members[i, j] else 0.0
                close(target["target_units"], expected, "frozen target uses signal close")
        for order in orders:
            i, j = date_column[order["signal_date"]], symbol_column[order["symbol"]]
            close(
                order["requested_units"],
                abs(order["target_units"] - positions[i, j]),
                "requested units",
            )
            assert order["execution_date"] == dates[i + 1]
            if order["reason"] == "full_day_suspension":
                assert halts[i + 1, j] and order["filled_units"] == 0
        returns = nav[1:] / nav[:-1] - 1
        metrics = summary["metrics"]
        close(metrics["total_return"], nav[-1] / capital - 1, "total return")
        close(
            metrics["annualized_return"],
            (nav[-1] / capital) ** (252 / len(returns)) - 1,
            "annualized return",
        )
        close(metrics["annualized_volatility"], returns.std(ddof=1) * 252**0.5, "volatility")
        close(metrics["sharpe_rf0"], returns.mean() / returns.std(ddof=1) * 252**0.5, "Sharpe")
        close(metrics["max_drawdown"], (nav / np.maximum.accumulate(nav) - 1).min(), "drawdown")
        close(
            metrics["cumulative_turnover"],
            (traded_notional / np.r_[capital, nav[:-1]]).sum(),
            "turnover",
        )
        for key, total in fees.items():
            close(summary["costs"][key], total, "aggregate " + key)
        stale_records = [
            {"date": dates[i], "symbol": symbols[j]} for i, j in np.argwhere(missing_held)
        ]
        answer[name] = {
            "passed": True,
            "sessions": len(dates),
            "signals": len(signals),
            "execution_sessions": len(trade_days),
            "orders": len(orders),
            "fills": len(fills),
            "first_signal": expected_signals[0],
            "first_execution": min(trade_days),
            "last_execution": max(trade_days),
            "order_status_counts": dict(Counter(o["status"] for o in orders)),
            "order_reason_counts": dict(Counter(o["reason"] for o in orders if o["reason"])),
            "rejected_signal_targets": sum(len(s["rejected_targets"]) for s in signals),
            "stale_records": stale_records,
            "minimum_cash": float(min(daily.cash.min(), min(f["cash_after"] for f in fills))),
            "max_daily_cash_difference": float(np.max(np.abs(daily.cash - cash))),
            "max_daily_nav_difference": float(np.max(np.abs(daily.nav - nav))),
            "max_per_fill_cash_difference": max(cash_errors),
            "pre_tax_cut_sell_fills": sum(
                f["side"] == "SELL" and f["execution_date"] < "2023-08-28" for f in fills
            ),
            "post_tax_cut_sell_fills": sum(
                f["side"] == "SELL" and f["execution_date"] >= "2023-08-28" for f in fills
            ),
            "costs": summary["costs"],
            "metrics": metrics,
        }
    result = {
        "status": "passed",
        "method": "independent reconciliation from persisted fills and source matrices",
        "scenarios": answer,
    }
    (output / "independent_audit.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    audit(Path(sys.argv[1]).resolve())
