"""
Tests for depth_seconds.py (book → per-second summaries) and horizon_check.py.

Run with: python test_horizon_check.py   (no market data, no databento needed)
"""

import sys

import numpy as np
import pandas as pd

from depth_seconds import event_ofi, merge, summarise
from horizon_check import session_ic, to_grid, windows

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


_DT = [("ts_recv", "u8"), ("instrument_id", "u4"), ("action", "S1"), ("side", "S1"),
       ("size", "u4")] + [(f"{s}_{k}_0{i}", "i8" if k == "px" else "u4")
                          for i in range(5) for s in ("bid", "ask") for k in ("px", "sz")]


def _records(rows):
    """rows: (second, inst, action, side, size, bid, bsz, ask, asz)."""
    a = np.zeros(len(rows), dtype=_DT)
    for j, (t, inst, act, side, size, b, bs, k, ks) in enumerate(rows):
        a[j]["ts_recv"] = int(t * 1e9) + j
        a[j]["instrument_id"], a[j]["action"], a[j]["side"], a[j]["size"] = inst, act, side, size
        a[j]["bid_px_00"], a[j]["bid_sz_00"] = int(round(b * 1e9)), bs
        a[j]["ask_px_00"], a[j]["ask_sz_00"] = int(round(k * 1e9)), ks
    return a


def _carry():
    return {"bid": np.nan, "ask": np.nan, "bsz": np.nan, "asz": np.nan, "inst": -1}


def t_event_ofi_cases():
    # Bid up a tick: the whole new bid size is buying pressure.
    assert event_ofi(10.01, 5, 10.02, 7, 10.00, 9, 10.02, 7) == 5
    # Same bid, size grows 9 → 12: +3.
    assert event_ofi(10.00, 12, 10.02, 7, 10.00, 9, 10.02, 7) == 3
    # Ask down a tick: selling pressure of the new ask size.
    assert event_ofi(10.00, 9, 10.01, 4, 10.00, 9, 10.02, 7) == -4


def t_summarise_per_second():
    a = _records([
        (100, 1, b"A", b"B", 1, 10.00, 9, 10.02, 7),
        (100, 1, b"A", b"B", 1, 10.01, 5, 10.02, 7),     # +5
        (100, 1, b"T", b"B", 3, 10.01, 5, 10.02, 4),     # buy 3; ask size 7→4: +3
        (101, 1, b"T", b"A", 2, 10.01, 3, 10.02, 4),     # sell 2; bid size 5→3: −2
    ])
    s, carry = summarise(a, _carry())
    out = merge([s])
    assert list(out["sec"]) == [100, 101]
    assert list(out["ofi"]) == [8, -2], out["ofi"]
    assert list(out["tflow"]) == [3, -2]
    assert list(out["ntr"]) == [1, 1]
    assert out["bid"][0] == 10.01 and out["asz"][0] == 4


def t_crossed_book_and_chunks():
    rows = [
        (100, 1, b"A", b"N", 0, 10.05, 1, 10.00, 1),     # crossed: ignored
        (101, 1, b"A", b"B", 1, 10.00, 9, 10.02, 7),
        (101, 1, b"A", b"B", 1, 10.01, 5, 10.02, 7),
        (102, 1, b"A", b"A", 1, 10.01, 5, 10.01 + 0.01, 2),
    ]
    whole = merge([summarise(_records(rows), _carry())[0]])
    p1, c = summarise(_records(rows[:2]), _carry())
    p2, _ = summarise(_records(rows[2:]), c)
    split = merge([p1, p2])
    assert np.isnan(whole["bid"][0])                     # no quote in the crossed second
    for k in ("ofi", "tflow", "bid", "ask"):
        assert np.allclose(whole[k], split[k], equal_nan=True), k


def t_instrument_change_resets_ofi():
    a = _records([
        (100, 1, b"A", b"B", 1, 10.00, 9, 10.02, 7),
        (101, 2, b"A", b"B", 1, 11.00, 50, 11.02, 7),     # new contract
    ])
    out = merge([summarise(a, _carry())[0]])
    assert out["ofi"][1] == 0


def _grid_data(n=4000, seed=0, lead=False):
    """Flow drives the mid; with lead=True flow at second t moves mid at t+5."""
    rng = np.random.default_rng(seed)
    flow = rng.normal(size=n)
    move = np.roll(flow, 5) if lead else flow
    mid = 100 + 0.01 * np.cumsum(np.round(move))
    return {"sec": np.arange(n) + 1_700_000_000, "bid": mid - 0.005, "ask": mid + 0.005,
            "bsz": np.full(n, 10.0), "asz": np.full(n, 10.0), "bdep": np.full(n, 50.0),
            "adep": np.full(n, 50.0), "ofi": flow, "tflow": flow, "ntr": np.ones(n),
            "inst": np.ones(n)}


def t_no_lookahead():
    # Flow moves the price in the same second only: same-window IC is high,
    # forecasting IC must be ~0 — any leak of (t, t+h] into features shows here.
    w = windows(to_grid(_grid_data()), 10, 0.01)
    same = w["ofi"].rank().corr(w["pastret"].rank())
    fwd = w["ofi"].rank().corr(w["fwd"].rank())
    assert same > 0.7, same
    assert abs(fwd) < 0.1, fwd


def t_finds_a_lead():
    # Flow leads the price by 5 s: a 10 s forecast must pick it up.
    w = windows(to_grid(_grid_data(lead=True)), 10, 0.01)
    assert w["ofi"].rank().corr(w["fwd"].rank()) > 0.2


def t_gap_splits_segments():
    d = _grid_data(2000)
    d["sec"][1000:] += 3600                              # an hour's halt
    g = to_grid(d)
    assert g["seg"].nunique() == 2 and len(g) == 2000


def t_session_ic_tstat():
    rng = np.random.default_rng(1)
    w = pd.DataFrame({"x": rng.normal(size=4000), "session": np.repeat(np.arange(40), 100)})
    w["fwd"] = 0.3 * w["x"] + rng.normal(size=4000)
    ic, t, n = session_ic(w, "x")
    assert n == 40 and ic > 0.2 and t > 5


for _n, _f in list(globals().items()):
    if _n.startswith("t_"):
        check(_n[2:], _f)
print(f"\n{'ALL PASS' if not _failures else f'{len(_failures)} FAILED'}")
sys.exit(1 if _failures else 0)
