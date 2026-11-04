"""
Tests for scoring.daily_vol_ic.

Run with: python test_vol_scoring.py   (no market data required)
"""

import sys

import numpy as np

from scoring import daily_vol_ic

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _market(n=400, seed=0):
    """Daily closes with persistent, regime-switching volatility."""
    rng = np.random.default_rng(seed)
    vol = np.exp(np.cumsum(rng.normal(0, 0.08, n)) * 0.5) * 0.01
    r = rng.normal(0, 1, n) * vol
    return 70 * np.exp(np.cumsum(r)), vol


def test_true_next_volatility_scores_well_and_beyond_trailing():
    close, vol = _market()
    bar_day = np.arange(len(close))
    pred = np.append(vol[1:], vol[-1])                 # tomorrow's true vol
    r = daily_vol_ic(pred, bar_day, bar_day, close)
    assert r["ic_abs1"] > 0.2 and r["partial_abs1"] > 0.05, r


def test_trailing_vol_itself_has_no_information_beyond_trailing_vol():
    close, _ = _market()
    lr = np.diff(np.log(close))
    bar_day = np.arange(len(close))
    pred = np.array([lr[max(0, d - 20):d].std() if d >= 2 else 0.0 for d in bar_day])
    r = daily_vol_ic(pred, bar_day, bar_day, close)
    assert r["ic_rv5"] > 0.1, r
    assert abs(r["partial_rv5"]) < 0.05, r


def test_noise_forecast_scores_about_zero():
    close, _ = _market()
    bar_day = np.arange(len(close))
    pred = np.random.default_rng(7).normal(size=len(close))
    r = daily_vol_ic(pred, bar_day, bar_day, close)
    assert abs(r["ic_abs1"]) < 0.15 and abs(r["partial_abs1"]) < 0.15, r


def test_outcomes_are_strictly_after_the_decision():
    close, _ = _market()
    lr = np.diff(np.log(close))
    bar_day = np.arange(len(close))
    today_abs = np.append(0.0, np.abs(lr))             # |return into day d|, known at d
    r = daily_vol_ic(today_abs, bar_day, bar_day, close)
    nxt = np.append(np.abs(lr), 0.0)                   # |return d → d+1|, the outcome
    r2 = daily_vol_ic(nxt, bar_day, bar_day, close)
    assert r2["ic_abs1"] > 0.99 > r["ic_abs1"], (r["ic_abs1"], r2["ic_abs1"])


def test_last_bar_of_each_day_is_the_forecast():
    close, vol = _market()
    bar_day = np.repeat(np.arange(len(close)), 3)
    pred = np.zeros(len(bar_day))
    pred[2::3] = np.append(vol[1:], vol[-1])           # only the last bar knows
    r = daily_vol_ic(pred + 1e-12 * np.arange(len(pred)), np.arange(len(bar_day)),
                     bar_day, close)
    assert r["ic_abs1"] > 0.2, r


if __name__ == "__main__":
    print("vol scoring tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all tests passed")
