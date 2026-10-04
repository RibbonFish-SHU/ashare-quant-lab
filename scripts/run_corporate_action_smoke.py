"""Verify synthetic cash-dividend and integer-bonus settlement; no strategy performance."""

import argparse
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid
from zoneinfo import ZoneInfo

from ashare_lab.corporate_actions import (
    COST_POLICY,
    ActionStep,
    CashDividend,
    CorporateAction,
    ShareDistribution,
    apply_actions,
    capture_record_close,
    cash_claims,
    create_state,
    execute_session,
    export_state,
    restore_state,
)
from ashare_lab.runtime import write_json
from ashare_lab.state_evidence.common import execution_identity
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


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def source_identity(project_root):
    script = Path(__file__).resolve()
    require(script.parents[1] == project_root, "script belongs to another project-root/worktree")
    source = execution_identity(project_root)
    relative = script.relative_to(project_root).as_posix()
    committed = subprocess.check_output(
        ["git", "-C", str(project_root), "show", f"{source['commit']}:{relative}"],
        timeout=15,
    )
    actual = script.read_bytes()
    require(
        actual.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"),
        "smoke script differs from source commit",
    )
    source["smoke_script"] = {
        "path": relative,
        "sha256": hashlib.sha256(actual).hexdigest(),
        "commit_blob_sha256": hashlib.sha256(committed).hexdigest(),
    }
    return source


def run_fixture(output):
    shanghai = ZoneInfo("Asia/Shanghai")

    def at(day, hour=9, minute=0):
        return datetime(2023, 1, day, hour, minute, tzinfo=shanghai)

    dates = tuple(date(2023, 1, d) for d in (3, 4, 5, 6, 9, 10, 11))
    calendar = ExecutionCalendar(dates)
    symbol, other = "000001.SZ", "600001.SH"
    account = Account(Decimal("0"), {symbol: [HoldingLot(200, dates[0], Decimal("10"))]})
    action = CorporateAction(
        "synthetic-dividend-bonus-1",
        1,
        symbol,
        dates[1],
        dates[2],
        at(3),
        at(3, 10),
        "implementation",
        True,
        "synthetic",
        CashDividend(
            Decimal("1.25"),
            dates[3],
            "synthetic_flat_withholding",
            Decimal("0.10"),
            "synthetic",
            ex_price_deduction_per_total_share=Decimal("1.20"),
        ),
        ShareDistribution(
            "stock_bonus",
            Decimal("0.5"),
            dates[2],
            dates[4],
            dates[5],
            "integer_only",
            COST_POLICY,
            "synthetic",
        ),
    )
    costs = CostModel(
        commission_rate=Decimal("0"),
        commission_minimum=Decimal("0"),
        tax_schedule=(TaxRate(dates[0], Decimal("0")),),
    )

    def session(day, side, quantity, security, price):
        on_date = date(2023, 1, day)
        prior = dates[dates.index(on_date) - 1]
        return {
            "session_id": f"synthetic-open-{day}",
            "at": at(day, 9, 30),
            "orders": [Order(f"synthetic-{day}-{side}", security, side, quantity, prior, on_date)],
            "market": {
                security: MarketObservation(
                    security,
                    on_date,
                    Decimal(price),
                    status="open",
                    no_price_limits=True,
                    open_capacity_shares=10000,
                )
            },
            "costs": costs,
            "lot_rules": {security: LotRule(100, 100, 100)},
            "action_guard": ActionGuard(on_date, known_no_actions=True),
            "participation_rate": Decimal("1"),
        }

    buy = session(5, "BUY", 100, other, "1")
    sell_old = session(6, "SELL", 300, symbol, "10")
    sell_bonus = session(10, "SELL", 100, symbol, "9")

    def session_payload(value):
        return {
            k: (
                [asdict(o) for o in v]
                if k == "orders"
                else {s: asdict(o) for s, o in v.items()}
                if k in {"market", "lot_rules"}
                else asdict(v)
                if k in {"costs", "action_guard"}
                else v
            )
            for k, v in value.items()
        }

    inputs = {
        "data_kind": "synthetic",
        "research_eligible": False,
        "account_id": "synthetic-account-1",
        "initial_account": asdict(account),
        "calendar": asdict(calendar),
        "initial_time": at(3),
        "snapshot_id": "record-close-1",
        "capture_at": at(4, 15),
        "action": asdict(action),
        "action_stages": [
            {"stage": "register", "at": at(4, 15)},
            {"stage": "ex_accrual", "at": at(5)},
            {"stage": "share_credit", "at": at(5, 16)},
            {"stage": "cash_payment", "at": at(9, 16)},
        ],
        "sessions": [session_payload(value) for value in (buy, sell_old, sell_bonus)],
        "assumptions": [
            "fixed 10% synthetic withholding, not actual A-share taxation",
            "original lot costs unchanged; bonus lot cost zero, not tax basis",
            "explicit synthetic session guards and zero trading costs",
            "no complete portfolio valuation or strategy performance",
        ],
    }
    write_json(output / "inputs.json", inputs)
    state = create_state(inputs["account_id"], account, calendar, asof=at(3), data_kind="synthetic")
    initial_account = asdict(account)
    traces = []

    def trace(label):
        traces.append({"sequence": len(traces), "event": label, "state": export_state(state)})
        write_json(output / "trace.json", traces)

    trace("initial")
    state = capture_record_close(state, inputs["snapshot_id"], symbol, dates[1], at=at(4, 15))
    trace("record_close")

    def apply(stage, when):
        return apply_actions(state, [ActionStep(action, inputs["snapshot_id"], stage, when)])

    state = apply("register", at(4, 15))
    trace("register")
    state = apply("ex_accrual", at(5))
    trace("ex_accrual")
    claims = cash_claims(state)
    require(
        claims["gross_cash_receivable"] == Decimal("250.00")
        and claims["net_cash_receivable"] == Decimal("225.00")
        and claims["uncredited_shares"] == {symbol: 100}
        and state.account.cash == 0,
        "ex accrual did not isolate claims from cash/shares",
    )
    state = execute_session(state, **buy)
    trace("cash_receivable_cannot_fund_buy")
    buy_result = json.loads(state.journal[-1].notes[0])["orders"][0]
    require(
        buy_result["filled_shares"] == 0 and state.account.cash == 0, "pending receivable was spent"
    )
    state = apply("share_credit", at(5, 16))
    trace("credited_but_locked_bonus")
    require(
        state.account.total_shares(symbol) == 300
        and state.account.available_shares(symbol, dates[3]) == 200,
        "bonus became sellable before listing/explicit release",
    )
    require(
        sum(lot.shares * lot.unit_cost for lot in state.account.lots[symbol]) == Decimal("2000"),
        "synthetic acquisition cost was not conserved",
    )
    state = execute_session(state, **sell_old)
    trace("sell_old_only")
    old_result = json.loads(state.journal[-1].notes[0])["orders"][0]
    require(
        old_result["filled_shares"] == 200 and state.account.total_shares(symbol) == 100,
        "locked bonus was sold or old shares were lost",
    )
    state = apply("cash_payment", at(9, 16))
    trace("delayed_cash_payment")
    require(
        state.account.cash == Decimal("2225.00") and state.journal[-1].applied_at == at(9, 16),
        "delayed payment amount/time differs",
    )
    checkpoint = export_state(state)
    expected_integrity = (
        state.integrity
    )  # Retained before serialization; never taken from loaded payload.
    write_json(output / "checkpoint.json", checkpoint)
    (output / "checkpoint.sha256").write_text(expected_integrity + "\n", encoding="utf-8")
    state = restore_state(
        json.loads((output / "checkpoint.json").read_text(encoding="utf-8")),
        expected_integrity=expected_integrity,
    )
    trace("restored_checkpoint")
    state = apply("cash_payment", at(10))
    state = apply("ex_accrual", at(5))
    state = execute_session(state, **sell_old)
    require(
        export_state(state) == checkpoint, "replay changed amounts, journal or later state clock"
    )
    trace("action_and_execution_replays")
    state = execute_session(state, **sell_bonus)
    trace("sell_released_bonus")
    require(
        state.account.cash == Decimal("3125.00") and not state.account.lots,
        "final cash/share balance differs",
    )
    require(
        sum(j.cash_delta for j in state.journal) == state.account.cash,
        "cash journal does not reconcile",
    )
    require(
        sum(j.tax_remitted for j in state.journal) == Decimal("25.00"), "withholding not reconciled"
    )
    require(
        sum(j.gross_receivable_delta for j in state.journal) == 0,
        "cash receivable remains unsettled",
    )
    require(asdict(account) == initial_account, "input account was mutated")
    final = export_state(state)
    require(
        export_state(restore_state(final, expected_integrity=state.integrity)) == final,
        "final state fails checkpoint invariants",
    )
    write_json(output / "journal.json", final["state"]["journal"])
    write_json(output / "final_state.json", final)
    return {
        "status": "synthetic_smoke_passed",
        "data_kind": "synthetic",
        "research_eligible": False,
        "complete_portfolio_valuation_available": False,
        "cash": state.account.cash,
        "tax_remitted": Decimal("25.00"),
        "holdings": {},
        "cash_claims": cash_claims(state),
        "journal_entries": len(state.journal),
        "trace_entries": len(traces),
        "state_integrity": state.integrity,
        "verified": [
            "record_close_rights",
            "ex_cash_share_isolation",
            "cash_receivable_not_spendable",
            "integer_share_credit",
            "original_cost_conservation",
            "share_listing_release",
            "sell_after_record_keeps_dividend",
            "delayed_payment_clock",
            "cash_tax_conservation",
            "checkpoint_restore",
            "action_and_execution_replay_noop",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    output_root = (args.output_root or root / "artifacts/experiments/synthetic").resolve()
    started = datetime.now(timezone.utc)
    output = output_root / (
        started.strftime("%Y%m%dT%H%M%S.%fZ") + "-corporate-actions-" + uuid.uuid4().hex[:8]
    )
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": "synthetic-corporate-action-smoke-v1",
        "status": "running",
        "started_at_utc": started.isoformat(),
        "data_kind": "synthetic",
        "research_eligible": False,
        "purpose": "synthetic corporate action accounting only; no strategy performance",
        "complete_portfolio_valuation_available": False,
        "project_root": str(root),
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
        manifest["source"] = source_identity(root)
        write_json(output / "manifest.json", manifest)
        result = run_fixture(output)
        write_json(output / "result.json", result)
        require(source_identity(root) == manifest["source"], "source identity changed during smoke")
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
