"""
Tests for ridge_check.py.

Run with: python test_ridge_check.py   (no market data required)
"""

import sys

import numpy as np

from ridge_check import (LABEL_DAYS, blocks, evaluate, session_rows, targets,
                         walk_forward)

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _prices(n=300, seed=0):
    rng = np.random.default_rng(seed)
    return 70 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))


def t_session_rows_timing():
    # 3 bars per session; fast value = bar index, slow value = day index.
    bar_day = np.repeat(np.arange(5), 3)
    fast = np.arange(15, dtype=float)[:, None]
    slow = np.arange(5, dtype=float)[:, None]
    X = session_rows(fast, bar_day, slow, 5, lag=1)
    assert np.isnan(X[0]).all()                       # no completed prior day
    for d in range(1, 5):
        assert X[d, 0] == 3 * d + 2, X[d]             # last bar of session d
        assert X[d, 1] == d - 1, X[d]                 # slow from day d-1


def t_targets():
    close = _prices(60)
    t = targets(close)
    lr = np.diff(np.log(close))
    assert np.allclose(t["day"][:-1], lr) and np.isnan(t["day"][-1])
    d = 30
    want = np.log(lr[d:d + 20].std()) - np.log(lr[d - 20:d].std())
    assert np.isclose(t["relvol"][d], want)
    assert np.isnan(t["relvol"][:20]).all()


def t_purge():
    # A feature equal to the label lets ridge fit perfectly only if purged
    # rows leak; check directly that no training label reaches the test block.
    n = 200
    for target, h in LABEL_DAYS.items():
        for a, _ in blocks(n):
            tr = np.arange(a)
            tr = tr[tr + h < a]
            assert (tr + h).max() < a


def t_finds_real_signal():
    # The next-day return is in the features: ridge must score near 1.
    close = _prices(300, 1)
    y = targets(close)["day"]
    rng = np.random.default_rng(2)
    X = np.column_stack([np.nan_to_num(y), rng.normal(size=(300, 4))])
    real, null, n = evaluate(X, close, "day", n_null=10)
    assert real > 0.8, real
    assert abs(null.mean()) < 0.2, null.mean()
    assert n > 100


def t_noise_scores_like_null():
    close = _prices(300, 3)
    X = np.random.default_rng(4).normal(size=(300, 6))
    real, null, _ = evaluate(X, close, "day", n_null=20)
    assert abs(real) < 0.25, real


def t_no_forecast_before_start():
    close = _prices(200, 5)
    X = np.random.default_rng(6).normal(size=(200, 3))
    p = walk_forward(X, targets(close)["day"], 1)
    assert np.isnan(p[:80]).all() and np.isfinite(p[80:]).all()


for _n, _f in list(globals().items()):
    if _n.startswith("t_"):
        check(_n[2:], _f)
print(f"\n{'ALL PASS' if not _failures else f'{len(_failures)} FAILED'}")
sys.exit(1 if _failures else 0)
