"""
Tests for stability_check.py (no market data required).

Run with: python test_stability_check.py
"""

import sys

import numpy as np
import pandas as pd

import stability_check as sc

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def test_quarters_follow_trade_dates():
    # 19697 = 6 Dec 2023, 19725 = 3 Jan 2024
    assert list(sc.quarter_of([19697, 19725])) == ["2023Q4", "2024Q1"]


def test_per_session_ic_is_within_sessions():
    rng = np.random.default_rng(0)
    n = 40
    x = rng.normal(size=2 * n)
    df = pd.DataFrame({"session": np.repeat([1, 2], n), "x": x,
                       "y": np.r_[x[:n], -x[n:]]})
    ic = sc.per_session_ic(df, "x", "y")
    assert np.isclose(ic.loc[1], 1.0) and np.isclose(ic.loc[2], -1.0)


def test_by_quarter_mean_and_t():
    s = pd.Series([1.0, 2.0, 3.0, 10.0], index=[19697, 19698, 19699, 19725])
    q = sc.by_quarter(s).set_index("quarter")
    assert q.loc["2023Q4", "sessions"] == 3 and np.isclose(q.loc["2023Q4", "mean"], 2.0)
    assert np.isclose(q.loc["2023Q4", "t"], 2.0 / 1.0 * np.sqrt(3))
    assert np.isnan(q.loc["2024Q1", "t"])


if __name__ == "__main__":
    print("stability_check tests:")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            check(name, fn)
    print("\nALL PASS" if not _failures else f"\n{len(_failures)} FAILED")
    sys.exit(1 if _failures else 0)
