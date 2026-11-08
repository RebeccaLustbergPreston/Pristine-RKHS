"""
Tests for kernel_paired.py: the paired IC check must credit a kernel on a
curved relationship and not on a linear one.

Run with: python test_kernel_paired.py   (no market data required)
"""

import sys

import numpy as np
import pandas as pd

import kernel_paired as kp
from minute_check import FEATURES

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _df(kind, n_sess=50, per=300, seed=0):
    rng = np.random.default_rng(seed)
    n = n_sess * per
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=list(FEATURES))
    df["minute"] = np.arange(n) + 28_000_000
    df["session"] = np.repeat(np.arange(n_sess), per)
    x0, x1 = df[FEATURES[0]].to_numpy(), df[FEATURES[1]].to_numpy()
    signal = x0 + np.sin(2 * x0) * x1 if kind == "curved" else x0 + 0.5 * x1
    df["fvol1"] = signal + rng.normal(scale=0.5, size=n)
    return df


def _diff(kind, model):
    df = _df(kind)
    p = kp.walk_forward(df, "fvol1", step=10)
    test = np.isfinite(p["ridge"])
    return kp.paired(df, np.where(test, p[model], np.nan), np.where(test, p["ridge"], np.nan), "fvol1")


def test_curved_hybrid_improves():
    d, t = _diff("curved", "hybrid")
    assert d > 0.01 and t > 5, (d, t)


def test_linear_hybrid_no_gain():
    d, t = _diff("linear", "hybrid")
    assert abs(d) < 0.01 and t < 2, (d, t)


def test_placebo_no_gain():
    d, t = _diff("linear", "placebo")
    assert abs(d) < 0.01, (d, t)


def test_paired_identical_is_zero():
    df = _df("linear", n_sess=5)
    p = df[FEATURES[0]].to_numpy()
    d, _ = kp.paired(df, p, p, "fvol1")
    assert d == 0.0


if __name__ == "__main__":
    print("kernel_paired tests:")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            check(name, fn)
    print("\nALL PASS" if not _failures else f"\n{len(_failures)} FAILED")
    sys.exit(1 if _failures else 0)
