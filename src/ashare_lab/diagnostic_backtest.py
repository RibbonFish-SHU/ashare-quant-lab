"""Conditional equal-weight diagnostics using fractional adjusted-price units.

This does not certify historical membership, adjustment factors, tradeability,
or executable A-share returns. No synthetic execution objects are involved.
"""

from datetime import date, datetime
import math

import numpy as np


class DiagnosticBacktestError(ValueError):
    """The supplied diagnostic path cannot be evaluated without inventing data."""


DEFAULT_CONFIG = {
    "scenario_name": "unspecified",
    "initial_cash": 10_000_000.0,
    "invest_fraction": 0.99,
    "rebalance_every": 5,
    "commission_rate": 0.0,
    "commission_minimum": 0.0,
    "transfer_fee_rate": 0.0,
    "buy_slippage_bps": 0.0,
    "sell_slippage_bps": 0.0,
    "stamp_tax_schedule": [{"effective_date": "1900-01-01", "rate": 0.0}],
}


def _require(condition, message):
    if not condition:
        raise DiagnosticBacktestError(message)


def _day(value):
    _require(not isinstance(value, datetime), "dates must not contain timestamps")
    try:
        return value if type(value) is date else date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise DiagnosticBacktestError("dates must be date objects or ISO dates") from exc


def _config(supplied, first_date):
    supplied = {} if supplied is None else supplied
    _require(isinstance(supplied, dict), "config must be a mapping")
    _require(not (set(supplied) - set(DEFAULT_CONFIG)), "unknown config key")
    result = {**DEFAULT_CONFIG, **supplied}
    for key in (
        "initial_cash",
        "invest_fraction",
        "commission_rate",
        "commission_minimum",
        "transfer_fee_rate",
        "buy_slippage_bps",
        "sell_slippage_bps",
    ):
        _require(not isinstance(result[key], bool), f"invalid {key}")
        try:
            result[key] = float(result[key])
        except (ValueError, TypeError) as exc:
            raise DiagnosticBacktestError(f"invalid {key}") from exc
        _require(math.isfinite(result[key]) and result[key] >= 0, f"invalid {key}")
    _require(result["initial_cash"] > 0, "initial_cash must be positive")
    _require(0 < result["invest_fraction"] <= 1, "invest_fraction must be in (0, 1]")
    _require(
        type(result["rebalance_every"]) is int and result["rebalance_every"] >= 1,
        "rebalance_every must be a positive integer",
    )
    for key in ("commission_rate", "transfer_fee_rate"):
        _require(result[key] < 1, f"{key} must be below one")
    for key in ("buy_slippage_bps", "sell_slippage_bps"):
        _require(result[key] < 10000, f"invalid {key}")
    _require(isinstance(result["scenario_name"], str), "scenario_name must be text")
    schedule = result["stamp_tax_schedule"]
    _require(
        isinstance(schedule, (list, tuple)) and bool(schedule), "explicit tax schedule required"
    )
    normalized = []
    for row in schedule:
        _require(
            isinstance(row, dict) and set(row) == {"effective_date", "rate"}, "invalid tax row"
        )
        effective = _day(row["effective_date"])
        try:
            rate = float(row["rate"])
        except (ValueError, TypeError) as exc:
            raise DiagnosticBacktestError("invalid stamp rate") from exc
        _require(
            not isinstance(row["rate"], bool) and math.isfinite(rate) and 0 <= rate < 1,
            "invalid stamp rate",
        )
        normalized.append({"effective_date": effective.isoformat(), "rate": rate})
    tax_dates = [row["effective_date"] for row in normalized]
    _require(tax_dates == sorted(set(tax_dates)), "tax schedule must be unique and ascending")
    _require(tax_dates[0] <= first_date.isoformat(), "tax schedule does not cover start date")
    result["stamp_tax_schedule"] = normalized
    return result


def _fees(notional, side, stamp_rate, config):
    return {
        "commission": max(config["commission_rate"] * notional, config["commission_minimum"]),
        "transfer_fee": config["transfer_fee_rate"] * notional,
        "stamp_tax": stamp_rate * notional if side == "SELL" else 0.0,
    }


def _metrics(daily, initial_cash, cumulative_turnover, final_units):
    nav = np.array([row["nav"] for row in daily], dtype=float)
    returns = np.array([row["net_return"] for row in daily[1:]], dtype=float)
    total = float(nav[-1] / initial_cash - 1)
    std = float(np.std(returns, ddof=1)) if len(returns) >= 2 else None
    annualized = math.expm1(math.log1p(total) * 252 / len(returns)) if len(returns) else None
    return {
        "total_return": total,
        "annualized_return": annualized,
        "annualized_volatility": std * math.sqrt(252) if std is not None else None,
        "sharpe_rf0": float(np.mean(returns)) / std * math.sqrt(252) if std else None,
        "max_drawdown": float(np.min(nav / np.maximum.accumulate(nav) - 1)),
        "return_observations": len(returns),
        "final_cash": daily[-1]["cash"],
        "final_holdings_market_value": daily[-1]["holdings_market_value"],
        "final_holding_count": int(np.count_nonzero(final_units > 0)),
        "final_adjusted_units": float(sum(final_units)),
        "cumulative_turnover": cumulative_turnover,
        "cumulative_one_way_turnover": cumulative_turnover / 2,
    }


def run_diagnostic_backtest(
    dates, symbols, adjusted_open, adjusted_close, membership, full_day_suspended, *, config=None
):
    """Return a JSON-compatible single-scenario diagnostic path.

    Signal targets use only the signal day's adjusted close and closing NAV.
    Missing target closes preserve that security's current units and produce a
    rejection record. A rejected or partial open order is not retried daily.
    Mark carry requires a known full-day suspension; otherwise held missing
    closes fail. The final portfolio remains invested, without hypothetical
    liquidation charges. Fees are continuous, unrounded diagnostic amounts.
    """
    days = tuple(_day(day) for day in dates)
    _require(bool(days) and list(days) == sorted(set(days)), "dates must be unique and ascending")
    symbols = tuple(symbols)
    _require(bool(symbols) and all(isinstance(s, str) for s in symbols), "symbols required")
    _require(list(symbols) == sorted(set(symbols)), "symbols must be unique and sorted")
    shape = (len(days), len(symbols))
    try:
        opens, closes = (
            np.asarray(adjusted_open, dtype=float),
            np.asarray(adjusted_close, dtype=float),
        )
    except (ValueError, TypeError) as exc:
        raise DiagnosticBacktestError("prices must be numeric arrays") from exc
    members, suspended = np.asarray(membership), np.asarray(full_day_suspended)
    _require(
        all(array.shape == shape for array in (opens, closes, members, suspended)), "shape mismatch"
    )
    _require(members.dtype == bool and suspended.dtype == bool, "condition matrices must be bool")
    for prices in (opens, closes):
        _require(
            np.all(np.isnan(prices) | (np.isfinite(prices) & (prices > 0))),
            "prices must be positive finite values or NaN",
        )
    cfg = _config(config, days[0])
    cash = cfg["initial_cash"]
    units = np.zeros(len(symbols))
    locked_units = np.zeros(len(symbols))
    unlock_day = np.zeros(len(symbols), dtype=int)
    last_close = np.full(len(symbols), np.nan)
    orders, trades, signals, daily, pending = [], [], [], [], []
    costs = {name: 0.0 for name in ("commission", "transfer_fee", "stamp_tax", "slippage")}
    cumulative_turnover = 0.0
    prior_nav = cash
    for i, day in enumerate(days):
        day_text = day.isoformat()
        stamp_rate = next(
            row["rate"]
            for row in reversed(cfg["stamp_tax_schedule"])
            if row["effective_date"] <= day_text
        )
        locked_units[unlock_day <= i] = 0
        traded_notional = 0.0
        for order in sorted(pending, key=lambda row: (row["side"] != "SELL", row["symbol"])):
            j = order.pop("_column")
            side = order["side"]
            reason = "full_day_suspension" if suspended[i, j] else None
            if reason is None and not np.isfinite(opens[i, j]):
                reason = "missing_open"
            if reason:
                order.update(status="unfilled", reason=reason)
                continue
            raw_open = float(opens[i, j])
            slip = cfg["buy_slippage_bps" if side == "BUY" else "sell_slippage_bps"] / 10000
            execution_price = raw_open * (1 + slip if side == "BUY" else 1 - slip)
            quantity = order["requested_units"]
            if side == "SELL":
                quantity = min(quantity, max(0.0, float(units[j] - locked_units[j])))
            else:
                affordable = max(
                    0.0,
                    min(
                        cash / (1 + cfg["transfer_fee_rate"] + cfg["commission_rate"]),
                        (cash - cfg["commission_minimum"]) / (1 + cfg["transfer_fee_rate"]),
                    ),
                )
                if affordable <= 8 * math.ulp(cfg["initial_cash"]):
                    affordable = 0.0  # Do not manufacture trades from floating-point cash dust.
                quantity = min(quantity, math.nextafter(affordable / execution_price, 0.0))
            if quantity <= 0:
                order.update(
                    status="unfilled", reason="t1_locked" if side == "SELL" else "insufficient_cash"
                )
                continue
            notional = quantity * execution_price
            fee = _fees(notional, side, stamp_rate, cfg)
            total_fee = sum(fee.values())
            if side == "BUY":
                # Make affordability strict even at the last floating-point ulp.
                while notional + total_fee > cash:
                    quantity = math.nextafter(quantity, 0.0)
                    notional = quantity * execution_price
                    fee = _fees(notional, side, stamp_rate, cfg)
                    total_fee = sum(fee.values())
                cash -= notional + total_fee
                units[j] += quantity
                locked_units[j] += quantity
                unlock_day[j] = i + 1
            else:
                if cash + notional < total_fee:
                    order.update(status="unfilled", reason="insufficient_cash_for_sell_fees")
                    continue
                cash += notional - total_fee
                units[j] -= quantity
            slippage_cost = abs(execution_price - raw_open) * quantity
            for name, amount in fee.items():
                costs[name] += amount
            costs["slippage"] += slippage_cost
            traded_notional += notional
            partial = quantity < order["requested_units"] * (1 - 1e-12)
            order.update(
                status="partial" if partial else "filled",
                filled_units=quantity,
                reason=("insufficient_cash" if side == "BUY" else "t1_locked") if partial else None,
            )
            trades.append(
                {
                    "order_id": order["order_id"],
                    "signal_date": order["signal_date"],
                    "execution_date": day_text,
                    "symbol": symbols[j],
                    "side": side,
                    "units": quantity,
                    "adjusted_open": raw_open,
                    "execution_price": execution_price,
                    "notional": notional,
                    **fee,
                    "slippage_cost": slippage_cost,
                    "cash_after": cash,
                    "sellable_from": days[i + 1].isoformat()
                    if side == "BUY" and i + 1 < len(days)
                    else None,
                }
            )
        pending = []
        valid = np.isfinite(closes[i])
        held = units > 0
        missing_held = held & ~valid
        unknown = missing_held & (~suspended[i] | ~np.isfinite(last_close))
        _require(
            not np.any(unknown),
            f"unexplained missing held close on {day_text}: "
            + ",".join(np.array(symbols)[unknown]),
        )
        last_close[valid] = closes[i, valid]
        market_value = float(np.dot(units[held], last_close[held]))
        nav = cash + market_value
        _require(
            math.isfinite(nav) and nav > 0 and cash >= 0 and np.all(units >= 0),
            "nonfinite or nonpositive NAV, negative cash or short position",
        )
        turnover = traded_notional / prior_nav
        cumulative_turnover += turnover
        daily.append(
            {
                "date": day_text,
                "nav": nav,
                "cash": cash,
                "holdings_market_value": market_value,
                "net_pnl": nav - prior_nav,
                "net_return": nav / prior_nav - 1,
                "stale_count": int(np.count_nonzero(missing_held)),
                "stale_symbols": [symbols[j] for j in np.flatnonzero(missing_held)],
                "holding_count": int(np.count_nonzero(held)),
                "traded_notional": traded_notional,
                "turnover": turnover,
                "cumulative_turnover": cumulative_turnover,
            }
        )
        if i % cfg["rebalance_every"] == 0:
            count = int(np.count_nonzero(members[i]))
            allocation = nav * cfg["invest_fraction"] / count if count else 0.0
            signal = {
                "date": day_text,
                "nav": nav,
                "member_count": count,
                "allocation_per_member": allocation,
                "targets": [],
                "rejected_targets": [],
            }
            for j, symbol in enumerate(symbols):
                if members[i, j] and not valid[j]:
                    signal["rejected_targets"].append(
                        {
                            "symbol": symbol,
                            "reason": "missing_signal_close",
                            "retained_units": float(units[j]),
                        }
                    )
                    continue
                target = allocation / closes[i, j] if members[i, j] else 0.0
                if members[i, j] or units[j] > 0:
                    signal["targets"].append(
                        {
                            "symbol": symbol,
                            "target_units": float(target),
                            "signal_close": float(closes[i, j]) if valid[j] else None,
                        }
                    )
                delta = float(target - units[j])
                if abs(delta) <= 1e-12 * max(1.0, float(units[j]), float(target)):
                    continue
                order = {
                    "order_id": f"order-{len(orders) + 1:06d}",
                    "signal_date": day_text,
                    "execution_date": days[i + 1].isoformat() if i + 1 < len(days) else None,
                    "symbol": symbol,
                    "side": "BUY" if delta > 0 else "SELL",
                    "requested_units": abs(delta),
                    "target_units": float(target),
                    "signal_close": float(closes[i, j]) if valid[j] else None,
                    "signal_nav": nav,
                    "filled_units": 0.0,
                    "reason": None,
                    "status": "pending" if i + 1 < len(days) else "expired_end_of_sample",
                }
                orders.append(order)
                if i + 1 < len(days):
                    order["_column"] = j
                    pending.append(order)
            signals.append(signal)
        prior_nav = nav
    costs["explicit_fees"] = costs["commission"] + costs["transfer_fee"] + costs["stamp_tax"]
    costs["including_slippage"] = costs["explicit_fees"] + costs["slippage"]
    positions = [
        {
            "symbol": symbols[j],
            "adjusted_units": float(units[j]),
            "valuation_price": float(last_close[j]),
            "market_value": float(units[j] * last_close[j]),
        }
        for j in np.flatnonzero(units > 0)
    ]
    return {
        "metadata": {
            "data_kind": "real_diagnostic",
            "research_eligible": False,
            "method": "conditional_equal_weight_fractional_adjusted_units",
            "assumptions": [
                "Adjustment prices are an uncertified total-return proxy; no separate dividends.",
                "Fractional adjusted units are not actual shares or lots.",
                "No opening capacity, accurate ST or price-limit filters; tradeability is not certified.",
                "Missing target prices retain current units; their allocation is not redistributed.",
                "Buys consume cash in sorted-symbol order, creating possible ordering bias.",
                "Even zero-cost orders may partially fill after an opening price gap.",
                "Cash below eight initial-cash floating-point ulps cannot fund a new buy.",
                "Unfilled open orders expire; suspended missing held closes carry the last valid close.",
                "Costs use adjusted-price notionals without cent rounding; no final liquidation.",
                "Turnover is two-sided traded notional / prior close NAV, summed over days.",
                "Returns exclude the first cash-only close; annualization uses 252 sessions, sample std, rf=0.",
            ],
        },
        "config": cfg,
        "daily": daily,
        "signals": signals,
        "orders": orders,
        "trades": trades,
        "costs": costs,
        "metrics": _metrics(daily, cfg["initial_cash"], cumulative_turnover, units),
        "final_positions": positions,
    }
