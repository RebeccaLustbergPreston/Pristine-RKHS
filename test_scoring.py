"""
Tests for scoring.py and for how bars are mapped to days and daily features.

Run with: python test_scoring.py   (no market data required)
"""

import sys

import numpy as np
import pandas as pd

from backtesting.walk_forward import MultiResolutionAligner
from scoring import daily_ic, fold_of

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _setup(n_days=30, bars_per_day=4, seed=0):
    rng = np.random.default_rng(seed)
    close = 70 * np.exp(np.cumsum(rng.normal(0, 0.01, n_days)))
    bar_day = np.repeat(np.arange(n_days), bars_per_day)
    return close, bar_day


def test_perfect_end_of_day_forecast_scores_one():
    close, bar_day = _setup()
    nxt = np.append(np.diff(np.log(close)), 0.0)            # tomorrow's return
    pred = nxt[bar_day]                                       # every bar knows it
    r = daily_ic(pred, np.arange(len(bar_day)), bar_day, close)
    assert abs(r["ic"] - 1.0) < 1e-12 and r["n_days"] == len(close) - 1


def test_only_the_last_bar_of_each_day_is_scored():
    close, bar_day = _setup()
    nxt = np.append(np.diff(np.log(close)), 0.0)
    pred = np.zeros(len(bar_day))
    last = np.flatnonzero(np.diff(bar_day, append=bar_day[-1] + 1))
    pred[last] = nxt[bar_day[last]]                           # only the decision bar is right
    r = daily_ic(pred + 1e-12 * np.arange(len(pred)), np.arange(len(bar_day)), bar_day, close)
    assert abs(r["ic"] - 1.0) < 1e-9


def test_the_same_day_return_does_not_count():
    close, bar_day = _setup()
    today = np.insert(np.diff(np.log(close)), 0, 0.0)        # return *into* each day
    r = daily_ic(today[bar_day], np.arange(len(bar_day)), bar_day, close)
    assert abs(r["ic"]) < 0.5


def test_per_fold_scores_and_no_skill_error():
    close, bar_day = _setup(n_days=40)
    pred = np.random.default_rng(1).normal(size=len(bar_day))
    folds = (np.arange(len(bar_day)) * 4) // len(bar_day)
    r = daily_ic(pred, np.arange(len(bar_day)), bar_day, close, folds)
    assert len(r["ic_folds"]) == 4 and abs(r["se_no_skill"] - 1 / np.sqrt(38)) < 1e-12


def test_fold_of_maps_bars_to_their_fold():
    assert list(fold_of([3, 4, 9], [[1, 2, 3], [4, 5], [9]])) == [0, 1, 2]


def test_evening_bar_belongs_to_the_next_session():
    # Sessions 5 and 6 July 2023; session 6 July starts 5 July 17:00 Chicago.
    starts = pd.DatetimeIndex(["2023-07-04 17:00", "2023-07-05 17:00"]).tz_localize(
        "America/Chicago").tz_convert("UTC")
    bars = pd.DatetimeIndex(["2023-07-05 20:00", "2023-07-05 22:30", "2023-07-06 01:00"],
                            tz="UTC")
    a = MultiResolutionAligner(bars.asi8 / 1e9, starts.asi8 / 1e9)
    # 20:00 UTC = 15:00 CDT (session 5 July); 22:30 UTC = 17:30 CDT (session 6 July)
    assert list(a.get_daily_indices(np.arange(3))) == [0, 1, 1]


def test_daily_features_come_from_the_last_completed_day():
    a = MultiResolutionAligner(np.array([0.5, 1.5, 2.5]), np.array([0.0, 1.0, 2.0]),
                               feature_lag=1)
    assert list(a.get_daily_indices(np.arange(3))) == [0, 1, 2]
    assert list(a.get_feature_day_indices(np.arange(3))) == [0, 0, 1]
    slow = np.array([[10.0], [11.0], [12.0]])
    assert list(a.align_features(None, slow, np.arange(3))[:, 0]) == [10.0, 10.0, 11.0]


def test_lag_zero_reproduces_the_old_mapping():
    a = MultiResolutionAligner(np.array([0.5, 1.5]), np.array([0.0, 1.0]))
    assert list(a.get_feature_day_indices(np.arange(2))) == [0, 1]


if __name__ == "__main__":
    print("scoring tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all tests passed")
