"""
Tests for mm_backtest.py (no market data required).

Run with: python test_mm_backtest.py
"""

import sys

import numpy as np
import pandas as pd

import mm_backtest as mm

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _book(bids, asks, bsz=None, asz=None, seg=None):
    n = len(bids)
    return (np.array(bids), np.array(asks),
            np.array(bsz if bsz is not None else [10.0] * n),
            np.array(asz if asz is not None else [10.0] * n),
            np.array(seg if seg is not None else [0] * n))


def _sim(book, mode="through", bid_ok=True, ask_ok=True, max_hold=1000):
    b, a, bs, as_, sg = book
    n = len(b)
    return mm.simulate(b, a, bs, as_, sg, np.full(n, bid_ok), np.full(n, ask_ok), mode, max_hold)


def test_trade_through_fills_at_our_price():
    # we join the bid at 100; the bid drops to 99: we bought at 100
    f = _sim(_book([100, 100, 99, 99], [101, 101, 100, 100]), ask_ok=False)
    i, side, px, forced = f
    assert (side[0], px[0], forced[0]) == (1, 100, 0) and i[0] == 2


def test_no_fill_without_trade_through():
    f = _sim(_book([100] * 5, [101] * 5), "through")
    assert len(f[0]) == 0


def test_queue_fill_needs_size_ahead_consumed():
    # 10 ahead of us at 100, 5 more join behind (15), then 12 leave: more
    # than the 10 ahead, so we count as filled
    b = _book([100] * 4, [101] * 4, bsz=[10, 15, 3, 3])
    f = _sim(b, "queue", ask_ok=False)
    passive = f[0][f[3] == 0]
    assert len(passive) == 1 and passive[0] == 2, f
    # exactly the 10 ahead leave: we are at the front, not filled
    f = _sim(_book([100] * 4, [101] * 4, bsz=[10, 15, 5, 5]), "queue", ask_ok=False)
    assert len(f[0]) == 0


def test_round_trip_pnl_and_fees():
    # buy at 100 (through), then sell at 102 when the ask trades through 102
    b = _book([100, 99, 101, 103, 103], [101, 100, 102, 104, 104])
    f = _sim(b)
    sess = np.zeros(5, int)
    pnl = mm.session_pnl(f, sess, fee_ticks=0.1)
    buys = f[2][f[1] == 1]
    sells = f[2][f[1] == -1]
    assert np.isclose(pnl.iloc[0], sells.sum() - buys.sum() - 0.1 * len(f[0]))
    assert len(buys) == len(sells)            # ends flat


def test_forced_exit_crosses_spread():
    b = _book([100, 99, 99, 99, 99, 99], [101, 100, 100, 100, 100, 100])
    f = _sim(b, ask_ok=True, max_hold=2)
    forced = f[3] == 1
    assert forced.any() and f[1][forced][0] == -1 and f[2][forced][0] == 99


def test_segments_end_flat():
    b = _book([100, 99, 99, 50, 50], [101, 100, 100, 51, 51], seg=[0, 0, 0, 1, 1])
    f = _sim(b, ask_ok=False)
    pos = np.cumsum(f[1])
    assert pos[-1] == 0
    i_close = f[0][f[3] == 1]
    assert (i_close == 2).all()               # closed in segment 0, not at 50


def test_gate_is_causal():
    rng = np.random.default_rng(0)
    fc = rng.normal(size=6000)
    m = np.arange(6000)
    g1 = mm.gate(m, fc, window=500)
    fc2 = fc.copy()
    fc2[4000:] += 100                         # change only the future
    g2 = mm.gate(m, fc2, window=500)
    assert (g1[:4000] == g2[:4000]).all()
    assert not g1[:500].any()                 # no threshold before warm-up


def test_forecast_applies_to_next_minute():
    sec = np.arange(0, 180)
    flags = mm.minute_flags(sec, np.array([0, 1, 2]), np.array([True, False, True]))
    assert not flags[:60].any()               # minute 0 has no earlier forecast
    assert flags[60:120].all() and not flags[120:180].any()


def test_closing_side_always_quoted():
    # long after a fill, with all opening disallowed after it, the offer stays
    b = _book([100, 99, 99, 101, 101], [101, 100, 100, 102, 102])
    n = 5
    allow = np.array([True, False, False, False, False])
    f = mm.simulate(*b[:4], b[4], allow, np.zeros(n, bool), "through", 1000)
    assert list(f[1]) == [1, -1] and f[3].tolist() == [0, 0]


def test_dropped_second_splits_segment():
    seg = np.array([0, 0, 0, 0, 1, 1])
    valid = np.array([True, True, False, True, True, True])
    out = mm.valid_segments(seg, valid)
    assert out.tolist() == [1, 1, 2, 3, 3]


def test_no_position_crosses_a_session():
    # long from the first session; the boundary must close it there, so each
    # session's cash flows net to a price difference, not a price level
    bid = np.array([100, 99, 99, 99, 99, 99])
    ask = bid + 1
    session = np.array([1, 1, 1, 2, 2, 2])
    seg = mm.split_sessions(np.zeros(6, int), session)
    n = 6
    f = mm.simulate(bid, ask, np.full(n, 10.0), np.full(n, 10.0), seg,
                    np.ones(n, bool), np.zeros(n, bool), "through", 1000)
    pnl = mm.session_pnl(f, session, 0.0)
    assert (abs(pnl) < 10).all(), pnl
    assert (np.cumsum(f[1])[np.isin(f[0], [2])] == 0).all()


def _delayed(bids, asks, delay, bid_ok=True, ask_ok=False):
    b, a, bs, as_, sg = _book(bids, asks)
    n = len(b)
    return mm.simulate(b, a, bs, as_, sg, np.full(n, bid_ok), np.full(n, ask_ok), "through",
                       1000, delay=delay)


def test_stale_bid_is_picked_off():
    # touch 104/106 until second 3, then 103/105. Seeing 3 s late, at second 6
    # we bid 104: above the real bid, below the real ask. It rests there as the
    # best bid and is filled at 104 the next second, although the market has
    # not moved since second 4
    bids = np.array([104, 104, 104, 104, 103, 103, 103, 103, 103])
    asks = bids + 2
    n = len(bids)
    allow = np.r_[np.zeros(3, bool), np.ones(n - 3, bool)]

    def run(d):
        return mm.simulate(bids, asks, np.full(n, 10.0), np.full(n, 10.0), np.zeros(n, int),
                           allow, np.zeros(n, bool), "through", 1000, delay=d)
    f = run(3)
    assert (f[0][0], f[1][0], f[2][0], f[3][0]) == (7, 1, 104, 0), f


def test_stale_bid_crossing_the_ask_buys_at_the_ask():
    # touch 104/105 until second 3, when we are first allowed to bid; the
    # market falls to 100/101 at second 4. Seeing 3 s late, at second 6 we
    # bid 104, which crosses the real ask: we buy there, at 101
    bids = np.array([104, 104, 104, 104, 100, 100, 100, 100])
    asks = bids + 1
    n = len(bids)
    allow = np.array([False, False, False, True, True, True, True, True])
    f = mm.simulate(bids, asks, np.full(n, 10.0), np.full(n, 10.0), np.zeros(n, int),
                    allow, np.zeros(n, bool), "through", 1000, delay=3)
    assert (f[0][0], f[1][0], f[2][0]) == (6, 1, 101), f


def test_delay_needs_through_rule():
    b = _book([100, 100], [101, 101])
    try:
        mm.simulate(*b, np.ones(2, bool), np.ones(2, bool), "queue", 10, delay=1)
    except ValueError:
        return
    raise AssertionError("queue rule accepted a delay")


if __name__ == "__main__":
    print("mm_backtest tests:")
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            check(name, fn)
    print("\nALL PASS" if not _failures else f"\n{len(_failures)} FAILED")
    sys.exit(1 if _failures else 0)
