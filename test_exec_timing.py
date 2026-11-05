"""
Tests for exec_timing.py.

Run with: python test_exec_timing.py   (no market data required)
"""

import sys

import numpy as np

from exec_timing import fills

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _book(n=5000, seed=0, informative=True):
    """Spread one tick (0.01). An ask-heavy book (−) precedes a 1-tick fall."""
    rng = np.random.default_rng(seed)
    book = rng.uniform(-1, 1, n)
    step = np.where(book[:-1] < -0.6, -0.01, np.where(book[:-1] > 0.6, 0.01, 0.0))
    if not informative:
        step = rng.permutation(step)
    mid = 100 + np.r_[0.0, np.cumsum(step)]
    return mid - 0.005, mid + 0.005, book


def t_cross_now_costs_half_spread():
    bid, ask, book = _book()
    f = fills(bid, ask, book, np.arange(0, 4000, 50), 30, 0.6, np.random.default_rng(0))
    assert np.allclose(f["now"][0], 0.005) and np.allclose(f["now"][1], 0.005)


def t_book_policy_saves_when_book_informs():
    bid, ask, book = _book()
    t0 = np.arange(0, 4800, 7)
    f = fills(bid, ask, book, t0, 30, 0.6, np.random.default_rng(0))
    now = np.mean(f["now"][0] + f["now"][1])
    wait = np.mean(f["book"][0] + f["book"][1])
    assert now - wait > 0.002, (now, wait)


def t_no_saving_when_book_uninformative():
    bid, ask, book = _book(informative=False)
    t0 = np.arange(0, 4800, 7)
    f = fills(bid, ask, book, t0, 30, 0.6, np.random.default_rng(0))
    now = np.mean(f["now"][0] + f["now"][1])
    wait = np.mean(f["book"][0] + f["book"][1])
    assert abs(now - wait) < 0.002, (now, wait)


def t_waits_within_window():
    bid, ask, book = _book()
    t0 = np.arange(0, 4000, 13)
    f = fills(bid, ask, book, t0, 60, 0.3, np.random.default_rng(0))
    for k in ("waited_b", "waited_s"):
        assert f[k].min() >= 0 and f[k].max() <= 60
    # A buy never waits on a book that is not leaning against it.
    assert np.all(f["waited_b"][book[t0] > -0.3] == 0)


for _n, _f in list(globals().items()):
    if _n.startswith("t_"):
        check(_n[2:], _f)
print(f"\n{'ALL PASS' if not _failures else f'{len(_failures)} FAILED'}")
sys.exit(1 if _failures else 0)
