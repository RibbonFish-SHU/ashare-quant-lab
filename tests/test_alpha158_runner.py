"""Portfolio selection and evaluation boundaries around the frozen model runner."""

import importlib
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("run_alpha158_diagnostic")


def test_top_selection_obeys_membership_missing_price_and_stable_score_ties(runner):
    symbols = ["000001.SZ", "000002.SZ", "600000.SH", "600001.SH", "600002.SH"]
    scores = np.array([[3.0, 3.0, 2.0, 20.0, 10.0]])
    closing = np.array([[10.0, 10.0, 10.0, 10.0, np.nan]])
    membership = np.array([[True, True, True, False, True]])
    selection = runner.select_top_k(scores, closing, membership, symbols, 1)
    assert selection.tolist() == [[True, False, False, False, False]]
    with pytest.raises(ValueError, match="insufficient current-day"):
        runner.select_top_k(scores, closing, membership, symbols, 4)


def test_future_scores_do_not_change_current_selection(runner):
    scores = np.array([[2.0, 1.0, 3.0], [2.0, 1.0, 3.0]])
    close = np.full(scores.shape, 10.0)
    members = np.ones(scores.shape, dtype=bool)
    symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
    before = runner.select_top_k(scores, close, members, symbols, 2)
    scores[1] = [1000, -1000, -1000]
    after = runner.select_top_k(scores, close, members, symbols, 2)
    np.testing.assert_array_equal(before[0], after[0])


def test_ic_uses_next_open_to_sixth_open_and_excludes_unmatured_tail(runner):
    dates = [f"2023-01-{day:02d}" for day in range(1, 11)]
    opening = np.full((10, 4), 10.0)
    opening[6:] = [9.0, 10.0, 12.0, 100.0]
    predictions = np.tile([-1.0, 0.0, 1.0, -100.0], (10, 1))
    members = np.tile([True, True, True, False], (10, 1))
    windows = [{"name": "sample", "evaluation_start": dates[0], "evaluation_end": dates[-1]}]
    rows, stats = runner.information_coefficients(
        dates, ["a", "b", "c", "d"], predictions, opening, members, windows
    )
    assert len(rows) == 4
    assert rows[0]["entry_date"] == dates[1]
    assert rows[0]["exit_date"] == dates[6]
    assert all(row["rows"] == 3 and row["rank_ic"] == pytest.approx(1) for row in rows)
    assert stats["all"]["days"] == 4
