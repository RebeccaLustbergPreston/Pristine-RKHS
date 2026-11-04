"""
Tests that MomentumGate.gate_values_walk_forward never sees the future.

Run with: python test_momentum_gate.py   (no market data required)
"""

import sys

import numpy as np

from kernels.momentum_gate import MomentumGate

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _prices(n=260, seed=0):
    return 70 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, 0.015, n)))


def _gate():
    return MomentumGate(short_window=20, long_window=60, vol_window=20, n_rff=64,
                        random_state=1)


def test_changing_the_future_does_not_change_earlier_gates():
    p = _prices()
    cut = 200                                   # change every price after day 200
    q = p.copy()
    q[cut + 1:] *= np.exp(np.random.default_rng(9).normal(0, 0.05, len(q) - cut - 1))
    a = _gate().gate_values_walk_forward(p, min_train=40)
    b = _gate().gate_values_walk_forward(q, min_train=40)
    last_row_known = cut - 60                   # row m describes day m + lookback
    assert np.allclose(a[:last_row_known], b[:last_row_known], equal_nan=True)
    assert not np.allclose(a[last_row_known + 2:], b[last_row_known + 2:], equal_nan=True)


def test_full_sample_fit_does_see_the_future():
    # The original fit + gate_values: changing late returns moves early gates.
    p = _prices()
    q = p.copy()
    q[230:] *= 1.3
    lb = 60

    def full(x):
        g = _gate()
        y = np.diff(np.log(x))[lb:]
        g.fit(x, y)
        return g.gate_values(x)
    assert not np.allclose(full(p)[:100], full(q)[:100])


def test_rows_without_enough_history_are_nan():
    g = _gate().gate_values_walk_forward(_prices(), min_train=40)
    assert np.isnan(g[:40]).all() and np.isfinite(g[40:]).all()


if __name__ == "__main__":
    print("momentum gate tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all tests passed")
