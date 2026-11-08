"""
Tests for mm_latency.py (no market data required).

Run with: python test_mm_latency.py
"""

import os
import sys
import tempfile

import numpy as np
import pandas as pd

import mm_backtest as mm
import mm_latency as ml

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def test_breakeven_fee():
    # 1,000 fills earning $500 before fees break even at $0.50 a side;
    # a policy losing before fees needs a rebate (negative fee)
    rows = pd.DataFrame([
        {"symbol": "CL", "fill": "through", "policy": "vol", "fills_per_session": 1000.0,
         "gross_usd_per_session": 500.0},
        {"symbol": "CL", "fill": "through", "policy": "always", "fills_per_session": 2000.0,
         "gross_usd_per_session": -1000.0},
        {"symbol": "CL", "fill": "through", "policy": "vol - always", "fills_per_session": np.nan,
         "gross_usd_per_session": np.nan},
    ])
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "mm.csv")
        rows.to_csv(p, index=False)
        be = ml.breakeven(p).set_index("policy")
    assert set(be.index) == {"vol", "always"}
    assert np.isclose(be.loc["vol", "breakeven_fee_usd"], 0.5)
    assert np.isclose(be.loc["always", "breakeven_fee_usd"], -0.5)


def test_delay_never_helps_on_a_trending_book():
    # a steadily falling market: a slower quoter is picked off at least as
    # often and at least as badly as a faster one
    n = 400
    bids = 1000 - np.arange(n) // 3
    asks = bids + 1
    sz = np.full(n, 10.0)
    seg = np.zeros(n, int)
    ok = np.ones(n, bool)

    def loss(d):
        f = mm.simulate(bids, asks, sz, sz, seg, ok, ok, "through", 120, delay=d)
        return mm.session_pnl(f, np.zeros(n, int), 0.0).sum()
    assert loss(5) <= loss(0)


if __name__ == "__main__":
    print("mm_latency tests:")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            check(name, fn)
    print("\nALL PASS" if not _failures else f"\n{len(_failures)} FAILED")
    sys.exit(1 if _failures else 0)
