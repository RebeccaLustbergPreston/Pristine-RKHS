"""
Tests for kernel_retest.py: the harness must let a kernel win when the
relationship is curved, and must not hand it a win when it is linear.

Run with: python test_kernel_retest.py   (no market data required)
"""

import sys

import numpy as np
import pandas as pd

import kernel_retest as kr
from minute_check import FEATURES

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _df(kind, n_sess=60, per=300, seed=0):
    rng = np.random.default_rng(seed)
    n = n_sess * per
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURES))), columns=list(FEATURES))
    df["minute"] = np.arange(n) + 28_000_000
    df["session"] = np.repeat(np.arange(n_sess), per)
    x0, x1 = df[FEATURES[0]].to_numpy(), df[FEATURES[1]].to_numpy()
    signal = np.sin(2 * x0) * x1 if kind == "curved" else x0 + 0.5 * x1
    df["fvol1"] = signal + rng.normal(scale=0.5, size=n)
    return df


def _beyond(df, model):
    preds, _ = kr.walk_forward(df, "fvol1", step=10)
    test = np.isfinite(preds["ridge"])
    return kr.session_scores(df, np.where(test, preds[model], np.nan), "fvol1",
                             base=np.where(test, preds["ridge"], np.nan))


def t_kernel_wins_on_curved():
    b, t = _beyond(_df("curved"), "tuned")
    assert b > 0.2 and t > 5, (b, t)
    b, t = _beyond(_df("curved"), "hybrid")
    assert b > 0.2 and t > 5, (b, t)


def t_no_win_on_linear():
    b, t = _beyond(_df("linear"), "tuned")
    assert t < 2, (b, t)


def t_tuning_uses_training_only():
    # Corrupt every test-period target: tuned forecasts must not change.
    df = _df("curved", n_sess=40)
    a, _ = kr.walk_forward(df, "fvol1", step=10)
    df2 = df.copy()
    first_test = np.unique(df["session"])[int(40 * 0.3)]
    later = df2["session"] >= first_test + 10        # beyond the first test block
    df2.loc[later, "fvol1"] = 1e6
    b, _ = kr.walk_forward(df2, "fvol1", step=10)
    block = (df["session"] >= first_test) & (df["session"] < first_test + 10)
    assert np.allclose(a["tuned"][block], b["tuned"][block])


for _n, _f in list(globals().items()):
    if _n.startswith("t_"):
        check(_n[2:], _f)
print(f"\n{'ALL PASS' if not _failures else f'{len(_failures)} FAILED'}")
sys.exit(1 if _failures else 0)
