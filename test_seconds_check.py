"""
Tests for seconds_check.py (no market data required).

Run with: python test_seconds_check.py
"""

import sys

import numpy as np
import pandas as pd

import kernel_fair as kf
import seconds_check as sc

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _grid(n=4000, seed=0, seg_break=None):
    rng = np.random.default_rng(seed)
    mid = 1000 + np.cumsum(rng.choice([-1, 0, 0, 0, 1], n))
    sec = np.arange(n) + 1_700_000_000
    g = pd.DataFrame({"bid": mid.astype(float), "ask": mid + 1.0,
                      "bsz": rng.integers(1, 30, n).astype(float),
                      "asz": rng.integers(1, 30, n).astype(float),
                      "bdep": rng.integers(10, 90, n).astype(float),
                      "adep": rng.integers(10, 90, n).astype(float),
                      "ofi": rng.normal(size=n), "tflow": rng.normal(size=n),
                      "ntr": rng.integers(0, 5, n).astype(float), "inst": np.ones(n)}, index=sec)
    seg = np.zeros(n, int)
    if seg_break is not None:
        seg[seg_break:] = 1
    g["seg"] = seg
    return g


def test_window_sum_stays_in_segment():
    seg = np.array([0, 0, 0, 1, 1, 1])
    out = sc.window_sum(np.ones(6), 2, seg)
    assert np.isnan(out[:2]).all() and out[2] == 2 and np.isnan(out[3]) and np.isnan(out[4])
    assert out[5] == 2


def test_features_do_not_see_the_future():
    g = _grid()
    a = sc.build(g, 1.0, 5, spacing=10)
    g2 = g.copy()
    g2.iloc[2000:, g2.columns.get_loc("bid")] += 50
    g2.iloc[2000:, g2.columns.get_loc("ask")] += 50
    b = sc.build(g2, 1.0, 5, spacing=10)
    cut = g.index[2000 - 5 - 1]
    fa = a[a["sec"] < cut].set_index("sec")
    fb = b[b["sec"] < cut].set_index("sec")
    cols = list(sc.FEATURES) + ["y"]
    assert np.allclose(fa[cols].to_numpy(), fb.loc[fa.index, cols].to_numpy())


def test_no_window_crosses_a_segment():
    g = _grid(seg_break=2000)
    df = sc.build(g, 1.0, 10, spacing=1)
    edge = g.index[2000]
    assert not ((df["sec"] >= edge - 10) & (df["sec"] < edge + 10)).any()


def _df(kind, n_sess=60, per=400, seed=1):
    rng = np.random.default_rng(seed)
    n = n_sess * per
    df = pd.DataFrame(rng.normal(size=(n, len(sc.FEATURES))), columns=list(sc.FEATURES))
    x0, x1 = df["book"].to_numpy(), df["spread"].to_numpy()
    signal = x0 + np.sin(2 * x0) * x1 if kind == "curved" else x0 + 0.5 * x1
    df["y"] = signal + rng.normal(scale=0.5, size=n)
    df["session"] = np.repeat(np.arange(n_sess), per)
    df["sec"] = np.arange(n)
    return df


def _gain(kind):
    df = _df(kind)
    out, _ = sc.walk_forward(df, step=10, max_train=8000, n_fit=800)
    ic = {m: sc.session_ic(df, out[m], min_obs=10) for m in out}
    common = ic["ridge"].index
    return (sc.paired(ic["hybrid"].reindex(common), ic["ridge"]),
            sc.paired(ic["placebo"].reindex(common), ic["ridge"]))


def test_kernel_gains_on_a_curved_relation():
    (d, t), _ = _gain("curved")
    assert d > 0.01 and t > 5, (d, t)


def test_no_gain_on_a_linear_relation():
    (d, t), (pd_, pt) = _gain("linear")
    assert abs(d) < 0.01 and t < 2, (d, t)
    assert abs(pd_) < 0.01, (pd_, pt)


if __name__ == "__main__":
    sc.TUNE_ROWS, kf.D_TUNE = 4000, 400
    print("seconds_check tests:")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            check(name, fn)
    print("\nALL PASS" if not _failures else f"\n{len(_failures)} FAILED")
    sys.exit(1 if _failures else 0)
