"""
Tests for spillover.py.

Run with: python test_spillover.py   (no market data required)
"""

import sys

import numpy as np
import pandas as pd

import spillover as sp

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _panel(n_days=60, lead=0.0, seed=0):
    """Synthetic minute variance; with lead > 0, CL's variance raises NG's a minute later."""
    rng = np.random.default_rng(seed)
    mins = sp.CLOSE - sp.OPEN
    rows = []
    for d in range(n_days):
        base = {s: np.exp(rng.normal(0, 1, mins)) for s in sp.SYMBOLS}
        base["NG"] = base["NG"] + lead * np.r_[0.0, base["CL"][:-1]]
        rows.append(pd.DataFrame({"date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=d),
                                  "tod": np.arange(sp.OPEN, sp.CLOSE), **base}))
    return pd.concat(rows, ignore_index=True)


def t_target_is_next_h():
    p = _panel(2)
    f = sp.features(p)
    s = p[p["date"] == p["date"].iloc[0]].reset_index(drop=True)
    eps = p["ES"][p["ES"] > 0].median() * 0.05            # 2 days: both are warm-up
    i = 40
    assert np.isclose(f["ES_fwd5"].iloc[i], np.log(s["ES"].iloc[i + 1:i + 6].mean() + eps))
    assert np.isclose(f["ES_fwd1"].iloc[i], np.log(s["ES"].iloc[i + 1] + eps))
    last = s.index[-1]
    assert np.isnan(f["ES_fwd1"].iloc[last])           # no next minute within the day


def t_features_causal():
    # Change everything from a point on day 8 (after the warm-up dates):
    # no earlier feature may move.
    p = _panel(10)
    a = sp.features(p)
    p2 = p.copy()
    cut = p.index[p["date"] == p["date"].unique()[7]][100]
    p2.loc[cut:, "CL"] *= 10
    b = sp.features(p2)
    cols = [c for c in a.columns if c.startswith("CL_") and "fwd" not in c]
    pd.testing.assert_frame_equal(a.loc[:cut - 1, cols], b.loc[:cut - 1, cols])


def _beyond(p, b, a):
    f = sp.features(p)
    own_cols = [f"{b}_1", f"{b}_5", f"{b}_30"]
    own = sp.walk_forward(f, own_cols, f"{b}_fwd1")
    cross = sp.walk_forward(f, own_cols + [f"{a}_1", f"{a}_5"], f"{b}_fwd1")
    return sp.beyond(f, cross, own, f"{b}_fwd1", 1)


def t_finds_planted_lead():
    m, t, _ = _beyond(_panel(lead=3.0), "NG", "CL")
    assert m > 0.1 and t > 5, (m, t)


def t_no_spillover_in_independent_markets():
    m, t, _ = _beyond(_panel(lead=0.0), "NG", "CL")
    assert abs(t) < 3, (m, t)


for _n, _f in list(globals().items()):
    if _n.startswith("t_"):
        check(_n[2:], _f)
print(f"\n{'ALL PASS' if not _failures else f'{len(_failures)} FAILED'}")
sys.exit(1 if _failures else 0)
