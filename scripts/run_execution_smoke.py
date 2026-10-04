"""Verify a deterministic synthetic execution ledger; this is not strategy performance."""

import argparse
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
import uuid
from zoneinfo import ZoneInfo

import ashare_lab.execution as execution
from ashare_lab.execution import (
    Account,
    ActionGuard,
    CloseObservation,
    CostModel,
    ExecutionCalendar,
    HoldingLot,
    LotRule,
    MarketObservation,
    Order,
    TaxRate,
    build_equal_weight_orders,
    execute_orders,
    mark_to_market,
)
from ashare_lab.runtime import code_identity, write_json


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run_fixture(output: Path) -> dict:
    """Persist explicit inputs and verify cash, shares, capacity and a halted sell."""
    shanghai = ZoneInfo("Asia/Shanghai")
    signal_date, execute_date = date(2023, 1, 3), date(2023, 1, 4)
    signal_time = datetime(2023, 1, 3, 16, tzinfo=shanghai)
    calendar = ExecutionCalendar(tuple(date(2023, 1, d) for d in (3, 4, 5, 6, 9)))
    account = Account(
        Decimal("10000"), {"000002.SZ": [HoldingLot(100, date(2022, 12, 30), Decimal("9"))]}
    )
    universe = ("000001.SZ", "688001.SH")
    observations = {
        symbol: CloseObservation(symbol, signal_date, Decimal(price), signal_time)
        for symbol, price in (("000001.SZ", "10"), ("000002.SZ", "11"), ("688001.SH", "20"))
    }
    rules = {
        "000001.SZ": LotRule(100, 100, 100, sell_min_shares=100),
        "000002.SZ": LotRule(100, 100, 100, sell_min_shares=100),
        "688001.SH": LotRule(200, 1, 1, sell_min_shares=200),
    }
    costs = CostModel(
        commission_rate=Decimal("0.0003"),
        commission_minimum=Decimal("5"),
        transfer_fee_rate=Decimal("0.00001"),
        buy_slippage_bps=Decimal("10"),
        sell_slippage_bps=Decimal("10"),
        tax_schedule=(
            TaxRate(date(2023, 1, 1), Decimal("0.001")),
            TaxRate(date(2023, 8, 28), Decimal("0.0005")),
        ),
    )
    market = {
        symbol: MarketObservation(
            symbol,
            execute_date,
            Decimal(price),
            status="open",
            limit_up=Decimal(up),
            limit_down=Decimal(down),
            open_capacity_shares=capacity,
        )
        for symbol, price, up, down, capacity in (
            ("000001.SZ", "10.10", "11.00", "9.00", 350),
            ("000002.SZ", "11.05", "12.10", "9.90", 1000),
            ("688001.SH", "20.10", "24.00", "16.00", 1000),
        )
    }
    guard = ActionGuard(execute_date, known_no_actions=True)
    closing_time = datetime(2023, 1, 4, 16, tzinfo=shanghai)
    closing_prices = {
        symbol: CloseObservation(symbol, execute_date, Decimal(price), closing_time)
        for symbol, price in (("000001.SZ", "11"), ("688001.SH", "21"))
    }
    next_date = date(2023, 1, 5)
    halted_order = Order("halted-sell", "000001.SZ", "SELL", 100, execute_date, next_date)
    halted_market = {
        "000001.SZ": MarketObservation("000001.SZ", next_date, None, status="suspended")
    }
    next_guard = ActionGuard(next_date, known_no_actions=True)
    participation_rate = Decimal("1")
    inputs = {
        "data_kind": "synthetic",
        "research_eligible": False,
        "calendar": asdict(calendar),
        "initial_account": asdict(account),
        "universe": universe,
        "signal_date": signal_date,
        "signal_time": signal_time,
        "execute_date": execute_date,
        "signal_closes": {s: asdict(v) for s, v in observations.items()},
        "lot_rules": {s: asdict(v) for s, v in rules.items()},
        "costs": asdict(costs),
        "market": {s: asdict(v) for s, v in market.items()},
        "action_guard": asdict(guard),
        "participation_rate": participation_rate,
        "closing_prices": {s: asdict(v) for s, v in closing_prices.items()},
        "valuation_asof": closing_time,
        "second_session": {
            "order": asdict(halted_order),
            "market": {s: asdict(v) for s, v in halted_market.items()},
            "action_guard": asdict(next_guard),
        },
    }
    write_json(output / "inputs.json", inputs)
    orders = build_equal_weight_orders(
        account=account,
        universe=universe,
        close_observations=observations,
        signal_date=signal_date,
        signal_time=signal_time,
        execution_date=execute_date,
        calendar=calendar,
        lot_rules=rules,
    )
    write_json(output / "orders.json", [asdict(row) for row in orders])
    result = execute_orders(
        account,
        orders,
        market,
        calendar,
        costs,
        rules,
        guard,
        participation_rate=participation_rate,
    )
    write_json(output / "ledger.json", result.ledger)
    require(
        [
            (r.symbol, r.side, r.requested_shares, r.filled_shares, r.status, r.reason)
            for r in result.orders
        ]
        == [
            ("000002.SZ", "SELL", 100, 100, "filled", None),
            ("000001.SZ", "BUY", 500, 300, "partial", "partial_capacity"),
            ("688001.SH", "BUY", 277, 277, "filled", None),
        ],
        "unexpected order quantities, ordering or outcomes",
    )
    require(len(result.ledger) == 3 and len(result.filled) == 3, "partial fill missing from ledger")
    require(result.account.cash == Decimal("2481.56"), "cash differs from hand-calculated ledger")
    require(
        account.cash + sum((r["cash_delta"] for r in result.ledger), Decimal("0"))
        == result.account.cash,
        "ledger cash does not reconcile",
    )
    require(result.account.total_shares("000002.SZ") == 0, "exited position remains")
    for symbol, shares in (("000001.SZ", 300), ("688001.SH", 277)):
        require(result.account.total_shares(symbol) == shares, "incorrect ending shares")
        require(result.account.available_shares(symbol, execute_date) == 0, "T+1 released early")
        require(result.account.available_shares(symbol, next_date) == shares, "T+1 not released")
    require(asdict(account) == inputs["initial_account"], "input account was mutated")
    nav = mark_to_market(result.account, closing_prices, execute_date, asof=closing_time)
    require(nav == Decimal("11598.56"), "cash plus position valuation does not reconcile")
    halted = execute_orders(
        result.account, [halted_order], halted_market, calendar, costs, rules, next_guard
    )
    require(
        len(halted.orders) == 1 and halted.orders[0].reason == "suspended", "halted sell filled"
    )
    require(
        not halted.ledger and asdict(halted.account) == asdict(result.account),
        "halt changed account",
    )
    return {
        "status": "synthetic_smoke_passed",
        "data_kind": "synthetic",
        "research_eligible": False,
        "order_count": len(orders),
        "ledger_count": len(result.ledger),
        "cash": result.account.cash,
        "nav_for_ledger_check_only": nav,
        "orders": [asdict(row) for row in result.orders],
        "account": asdict(result.account),
        "second_session_rejections": [asdict(row) for row in halted.orders],
        "verified": [
            "sell_first",
            "capacity_partial",
            "board_lots",
            "cent_rounded_costs",
            "cash_ledger",
            "t_plus_one",
            "complete_close_valuation",
            "suspended_position_preserved",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    output_root = (args.output_root or project_root / "artifacts/experiments/synthetic").resolve()
    started = datetime.now(timezone.utc)
    output = output_root / (started.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "synthetic-execution-smoke-v1",
        "status": "running",
        "started_at_utc": started.isoformat(),
        "data_kind": "synthetic",
        "research_eligible": False,
        "purpose": "execution accounting and constraints only; no strategy performance",
        "project_root": str(project_root),
        "command": [
            sys.executable,
            str(Path(__file__).resolve()),
            *(sys.argv[1:] if argv is None else argv),
        ],
        "python": sys.version,
        "executable": sys.executable,
    }
    write_json(output / "manifest.json", manifest)
    timer = time.perf_counter()
    exit_code = 1
    try:
        loaded_root = Path(execution.__file__).resolve().parents[3]
        require(loaded_root == project_root, "project-root differs from loaded execution package")
        require(
            Path(__file__).resolve().parents[1] == project_root,
            "script belongs to another worktree",
        )
        manifest["source"] = code_identity(project_root)
        write_json(output / "manifest.json", manifest)
        result = run_fixture(output)
        write_json(output / "result.json", result)
        manifest["status"] = "passed"
        exit_code = 0
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        (output / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["duration_seconds"] = time.perf_counter() - timer
        manifest["artifacts_sha256"] = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.iterdir())
            if path.is_file() and path.name != "manifest.json"
        }
        write_json(output / "manifest.json", manifest)
    print(json.dumps({"status": manifest["status"], "output": str(output)}, ensure_ascii=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
