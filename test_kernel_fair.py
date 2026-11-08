"""
Tests for kernel_fair.py (no market data required).

Run with: python test_kernel_fair.py
"""

import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

import kernel_fair as kf
from kernel_paired import paired
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
    signal = x0 + np.sin(2 * x0) * x1 if kind == "curved" else x0 + 0.5 * x1
    df["fvol1"] = signal + rng.normal(scale=0.5, size=n)
    return df


def test_rff_approximates_rbf():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(50, 5))
    g = 0.2
    Z = kf.RFF(5, 20000, g, seed=3)(X).astype(float)
    K = np.exp(-g * ((X[:, None] - X[None]) ** 2).sum(-1))
    assert np.abs(Z @ Z.T - K).max() < 0.05


def test_chunked_ridge_matches_sklearn():
    rng = np.random.default_rng(2)
    X = rng.normal(size=(25_000, 4))
    y = np.sin(X[:, 0]) + rng.normal(scale=0.1, size=len(X))
    rff, beta, b0 = kf.fit_kernel(X, y, 0.3, 1.0, 300, seed=4)
    Z = rff(X).astype(float)
    m = Ridge(alpha=1.0).fit(Z, y)
    assert np.allclose(kf.predict(rff, X[:100], beta, b0), m.predict(Z[:100]), atol=1e-3)


def test_hour_not_one_hot_in_kernel():
    df = _df("linear", n_sess=3)
    lin, ker = kf.inputs(df, np.arange(len(df)))
    assert lin.shape[1] == len(FEATURES) + 24
    assert ker.shape[1] == len(FEATURES) + 2
    assert np.abs(ker).max() < 6


def test_clip_uses_training_rows_only():
    df = _df("linear", n_sess=4)
    df.loc[len(df) - 1, FEATURES[0]] = 1e6
    tr = np.arange(len(df) - 300)
    lin_a, _ = kf.inputs(df, tr)
    df.loc[len(df) - 1, FEATURES[0]] = 1e9
    lin_b, _ = kf.inputs(df, tr)
    assert np.allclose(lin_a[tr], lin_b[tr])


def _gain(kind):
    df = _df(kind)
    out, _ = kf.walk_forward(df, "fvol1", step=10, windows=(None,), n_fit=800)
    r, hy = out[(None, "ridge")], out[(None, "hybrid")]
    return paired(df, hy, r, "fvol1")


def test_curved_gain():
    d, t = _gain("curved")
    assert d > 0.01 and t > 5, (d, t)


def test_linear_no_gain():
    d, t = _gain("linear")
    assert abs(d) < 0.01 and t < 2, (d, t)


if __name__ == "__main__":
    kf.TUNE_ROWS, kf.D_TUNE = 8000, 400
    print("kernel_fair tests:")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            check(name, fn)
    print("\nALL PASS" if not _failures else f"\n{len(_failures)} FAILED")
    sys.exit(1 if _failures else 0)
