"""
Tests for label-horizon purging in PurgedWalkForward.split_indices.

Run with: python test_purge.py   (no market data required)
"""

import sys

import numpy as np

from backtesting.walk_forward import PurgedWalkForward

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _engine():
    return PurgedWalkForward(n_splits=4, embargo_bars=10, min_train_bars=200)


def test_no_training_label_reaches_into_the_test_fold():
    n, h = 1000, 50                                   # labels look 50 bars ahead
    label_end = np.minimum(np.arange(n) + h, n - 1)
    for tr, te in _engine().split_indices(n, label_end=label_end):
        assert label_end[tr].max() <= te[0], (label_end[tr].max(), te[0])


def test_without_label_end_the_embargo_alone_lets_them_through():
    n, h = 1000, 50
    label_end = np.minimum(np.arange(n) + h, n - 1)
    leaks = [label_end[tr].max() > te[0] for tr, te in _engine().split_indices(n)]
    assert any(leaks)


def test_one_bar_labels_change_nothing():
    n = 1000
    a = _engine().split_indices(n)
    b = _engine().split_indices(n, label_end=np.minimum(np.arange(n) + 1, n - 1))
    assert all(np.array_equal(x[0], y[0]) and np.array_equal(x[1], y[1])
               for x, y in zip(a, b))


if __name__ == "__main__":
    print("purge tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all tests passed")
