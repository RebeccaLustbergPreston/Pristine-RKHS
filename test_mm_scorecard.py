"""
Tests for mm_scorecard.py (no market data required).

Run with: python test_mm_scorecard.py
"""

import sys

import numpy as np

import mm_scorecard as sc

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def test_drawdown_from_running_peak():
    m = sc.scorecard([10, -30, 5, 20, -50, 40])
    # cumulative 10, -20, -15, 5, -45, -5; peak 10 then 10 ... -> worst fall 55
    assert m["max_drawdown"] == -55


def test_drawdown_counts_an_opening_loss():
    assert sc.scorecard([-10, -10, 5])["max_drawdown"] == -20


def test_sharpe_and_sortino_signs():
    rng = np.random.default_rng(0)
    x = rng.normal(-1, 2, 500)
    m = sc.scorecard(x)
    assert m["sharpe_ann"] < 0 and m["sortino_ann"] < 0
    assert np.isclose(m["sharpe_ann"], x.mean() / x.std(ddof=1) * np.sqrt(252))


def test_sortino_downside_uses_all_sessions():
    m = sc.scorecard([2.0, -2.0, 2.0, -2.0])
    assert np.isclose(m["sortino_ann"], 0.0)
    m = sc.scorecard([3.0, -1.0])
    assert np.isclose(m["sortino_ann"], 1.0 / np.sqrt(0.5) * np.sqrt(252))


def test_shares_and_ratio():
    m = sc.scorecard([4, -2, 6, -2])
    assert m["profitable_share"] == 0.5 and m["win_loss_ratio"] == 2.5
    assert (m["best"], m["worst"]) == (6, -2)


def test_session_dates():
    # session 19697 runs from 22:00 UTC on 5 Dec 2023 to 22:00 UTC on 6 Dec:
    # the exchange's trade date 6 Dec
    assert list(sc.session_dates([19697])) == ["2023-12-06"]


if __name__ == "__main__":
    print("mm_scorecard tests:")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            check(name, fn)
    print("\nALL PASS" if not _failures else f"\n{len(_failures)} FAILED")
    sys.exit(1 if _failures else 0)
