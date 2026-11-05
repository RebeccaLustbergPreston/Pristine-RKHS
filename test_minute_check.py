"""
Tests for minute_check.py.

Run with: python test_minute_check.py   (no market data required)
"""

import sys

import numpy as np
import pandas as pd

from horizon_check import to_grid
from minute_check import FEATURES, features, minute_bars, walk_forward

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _seconds(n_days=12, secs=6 * 3600, seed=0, lead=0.0):
    """Synthetic per-second book over several sessions, one per day."""
    rng = np.random.default_rng(seed)
    parts = []
    for d in range(n_days):
        t0 = 1_700_000_000 + d * 86400
        flow = rng.normal(size=secs)
        # A persistent book (half-life ~1 minute), as real books are.
        z = pd.Series(rng.normal(size=secs)).ewm(halflife=60).mean().to_numpy()
        book = np.tanh(z / z.std())
        drift = lead * np.r_[0.0, book[:-1]]           # book leads the next second's move
        mid = 100 + 0.01 * np.cumsum(np.round(rng.normal(size=secs) * 0.3 + drift))
        parts.append({"sec": t0 + np.arange(secs), "bid": mid - 0.005, "ask": mid + 0.005,
                      "bsz": 50 * (1 + book), "asz": 50 * (1 - book) + 1, "bdep": np.full(secs, 300.0),
                      "adep": np.full(secs, 300.0), "ofi": flow, "tflow": flow,
                      "ntr": rng.poisson(2, secs), "inst": np.ones(secs)})
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


def _df(**kw):
    return features(minute_bars(to_grid(_seconds(**kw)), 0.01), 0.01)


def t_targets_align():
    df = _df(n_days=2)
    s = df[df["seg"] == df["seg"].iloc[0]].reset_index(drop=True)
    m = 100
    assert np.isclose(s["fwd5"][m], (s["mid"][m + 5] - s["mid"][m]) / 0.01)
    want = np.log(s["rv"][m + 1:m + 6].mean() + 1e-3)
    assert np.isclose(s["fvol5"][m], want)


def t_features_causal():
    d = _seconds(n_days=1)
    a = features(minute_bars(to_grid(d), 0.01), 0.01)
    d2 = {k: v.copy() for k, v in d.items()}
    cut = len(d2["sec"]) // 2
    d2["bid"][cut:] += 1.0; d2["ask"][cut:] += 1.0; d2["tflow"][cut:] *= 5; d2["ofi"][cut:] *= 5
    b = features(minute_bars(to_grid(d2), 0.01), 0.01)
    last_safe = (d["sec"][cut] // 60) - 1
    ra, rb = a[a["minute"] < last_safe], b[b["minute"] < last_safe]
    pd.testing.assert_frame_equal(ra[list(FEATURES)], rb[list(FEATURES)])


def t_walk_forward_out_of_sample():
    df = _df(n_days=12)
    p = walk_forward(df, "fwd1", "ridge")
    first = np.sort(df["session"].unique())[int(df["session"].nunique() * 0.3)]
    assert np.isnan(p[df["session"].to_numpy() < first]).all()
    assert np.isfinite(p[df["session"].to_numpy() >= first]).mean() > 0.5


def t_finds_planted_signal():
    df = _df(n_days=12, lead=0.8)
    p = walk_forward(df, "fwd1", "ridge")
    ok = np.isfinite(p) & np.isfinite(df["fwd1"])
    assert pd.Series(p[ok]).rank().corr(pd.Series(df["fwd1"].to_numpy()[ok]).rank()) > 0.1


def t_no_signal_in_noise():
    df = _df(n_days=12, lead=0.0)
    p = walk_forward(df, "fwd1", "kernel")
    ok = np.isfinite(p) & np.isfinite(df["fwd1"])
    assert abs(pd.Series(p[ok]).rank().corr(pd.Series(df["fwd1"].to_numpy()[ok]).rank())) < 0.05


for _n, _f in list(globals().items()):
    if _n.startswith("t_"):
        check(_n[2:], _f)
print(f"\n{'ALL PASS' if not _failures else f'{len(_failures)} FAILED'}")
sys.exit(1 if _failures else 0)
