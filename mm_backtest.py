"""
Market-making backtest: does the 1-minute volatility forecast tell a passive
quoter when to stand aside?

    .venv/bin/python mm_backtest.py                      # CL, NG, HG, ZC
    .venv/bin/python mm_backtest.py --symbols CL

A market maker rests a bid at the best bid and an offer at the best ask and
earns the spread when both fill. Its cost is adverse selection: its quote
fills just before the price runs through it. Our out-of-sample forecasts of
next minute's volatility (kernel_paired.py, ridge and ridge + kernel) sort
minutes by that risk. This asks whether pulling quotes in the minutes the
forecast rates loud makes a quoter better off.

Simulation, on the per-second book (one snapshot per second, the state at
the end of the second):

  quotes     one lot, joined at the best bid and/or best ask at the end of a
             second and live from the next second. Position limit one lot
             each way: flat quotes both sides, long quotes only the offer,
             short only the bid. A quote that is no longer at the touch is
             cancelled and rejoined at the back of the new touch's queue.
  fills      we cannot see our own place in the queue, so two rules bracket
             the truth:
               through  filled only when the price trades through our level
                        (best bid falls below our bid, or the ask reaches
                        it). Our order would be the best bid until it filled,
                        so this is certain. Pessimistic: every fill comes
                        with the price already moving against us.
               queue    also filled when the displayed size at our price has
                        shrunk, since we joined, by more than the size that
                        was ahead of us. Optimistic: it counts every
                        cancellation as if it were ahead of us.
  exits      a position still open after MAX_HOLD seconds is closed by
             crossing the spread, as is any position at the end of a
             segment (a gap of over a minute, a contract switch, or the
             session boundary at 22:00 UTC). Sessions with less than an hour
             of test data (one-second stubs at a boundary) are not scored.
  costs      FEE_USD per contract per side (exchange and clearing), on every
             fill, passive or not. Results are also reported before fees.

Policies (the closing side of an open position is always quoted):
  always     quote whenever there is a forecast (control)
  random     pull quotes in randomly chosen minutes, as often as "vol" does
             (placebo)
  vol        quote only in minutes the ridge forecast rates calmer than the
             median of its previous GATE_WINDOW forecasts (causal)
  vol_kernel the same with the ridge + kernel forecast
  book       quote always, but skip the side the book leans against: no new
             offer when book imbalance > BOOK_K, no new bid when < -BOOK_K
  vol_book   vol and book together

The forecast made at the end of minute m governs the seconds of minute m+1.

RULE, FIXED BEFORE RUNNING: the volatility gate earns a place if "vol"
beats both "always" and "random" in per-session net profit with paired
t >= 2 on at least 3 of the 4 contracts, under both fill rules. A policy is
worth taking to the suite's risk layers only if its own net profit per
session is positive with t >= 2 under the pessimistic "through" rule.

Writes data/mm_backtest.csv (one row per contract, policy and fill rule) and
data/mm_sessions_<SYM>.csv (net profit per session, in dollars per lot).
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

from horizon_check import TICK, load, to_grid

_ROOT = os.path.dirname(os.path.abspath(__file__))
TICK_USD = {"CL": 10.0, "NG": 10.0, "HG": 12.5, "ZC": 12.5}
FEE_USD = 1.0
MAX_HOLD = 120
MIN_SESSION_SECONDS = 3600
GATE_Q = 0.5
GATE_WINDOW = 20_000
BOOK_K = 0.5
POLICIES = ("always", "random", "vol", "vol_kernel", "book", "vol_book")
FILLS = ("through", "queue")


def gate(minutes, forecast, q=GATE_Q, window=GATE_WINDOW):
    """True where a minute's forecast is below the q-quantile of the previous
    `window` forecasts. Uses no forecast later than the minute itself."""
    f = pd.Series(forecast, dtype=float)
    ok = np.isfinite(f.to_numpy())
    past = f[ok].rolling(window, min_periods=min(window, 2000)).quantile(q).shift(1)
    thr = pd.Series(np.nan, index=f.index)
    thr[ok] = past.to_numpy()
    out = (f < thr).to_numpy()
    return out & np.isfinite(thr.to_numpy())


def minute_flags(sec, minutes, flag):
    """Per-second flag: the value set for minute m applies to minute m + 1."""
    s = pd.Series(np.asarray(flag, bool), index=np.asarray(minutes) + 1)
    s = s[~s.index.duplicated()]
    return s.reindex(np.asarray(sec) // 60).fillna(False).to_numpy(bool)


def valid_segments(seg, valid):
    """Segment ids for the valid seconds only: a dropped second (one side of
    the book empty) ends the segment, so no position spans it."""
    seg, valid = np.asarray(seg), np.asarray(valid, bool)
    prev_ok = np.r_[False, valid[:-1]]
    new = ~prev_ok | np.r_[True, seg[1:] != seg[:-1]]
    return np.cumsum(new)[valid]


def split_sessions(seg, session):
    """Segment ids that also break at every session boundary, so that no
    position is carried from one session into the next and each session's
    profit is complete in itself."""
    seg, session = np.asarray(seg), np.asarray(session)
    new = np.r_[True, (seg[1:] != seg[:-1]) | (session[1:] != session[:-1])]
    return np.cumsum(new)


def simulate(bid, ask, bsz, asz, seg, allow_bid, allow_ask, mode="through", max_hold=MAX_HOLD):
    """
    Prices in integer ticks. Returns fills as arrays (index, side, price,
    forced): side +1 bought, -1 sold. Every segment ends flat.
    """
    bid, ask = bid.tolist(), ask.tolist()
    bsz, asz = bsz.tolist(), asz.tolist()
    seg, allow_bid, allow_ask = seg.tolist(), allow_bid.tolist(), allow_ask.tolist()
    queue = mode == "queue"
    F_i, F_side, F_px, F_forced = [], [], [], []
    n = len(bid)
    pos, t_in = 0, 0
    qb = qa = None                 # resting prices
    ahead_b = ahead_a = 0.0        # size ahead of us
    last_b = last_a = 0.0          # displayed size at our price last second
    for i in range(n):
        b, a = bid[i], ask[i]
        if i and seg[i] != seg[i - 1]:
            qb = qa = None         # segment change: previous one already flat
        # 1. fills on quotes placed at the end of an earlier second
        if qb is not None:
            hit = b < qb or a <= qb
            if not hit and queue and b == qb:
                ahead_b -= max(last_b - bsz[i], 0.0)
                last_b = bsz[i]
                hit = ahead_b < 0
            if hit:
                F_i.append(i); F_side.append(1); F_px.append(qb); F_forced.append(0)
                if pos == 0:
                    t_in = i
                pos += 1
                qb = None
        if qa is not None:
            hit = a > qa or b >= qa
            if not hit and queue and a == qa:
                ahead_a -= max(last_a - asz[i], 0.0)
                last_a = asz[i]
                hit = ahead_a < 0
            if hit:
                F_i.append(i); F_side.append(-1); F_px.append(qa); F_forced.append(0)
                if pos == 0:
                    t_in = i
                pos -= 1
                qa = None
        # 2. forced exits: held too long, or the segment ends here
        end = i == n - 1 or seg[i + 1] != seg[i]
        if pos != 0 and (end or i - t_in >= max_hold):
            if pos > 0:
                F_i.append(i); F_side.append(-1); F_px.append(b); F_forced.append(1)
            else:
                F_i.append(i); F_side.append(1); F_px.append(a); F_forced.append(1)
            pos = 0
            qb = qa = None
        if end:
            qb = qa = None
            continue
        # 3. quotes for the next second
        want_b = pos < 1 and (pos < 0 or allow_bid[i])
        want_a = pos > -1 and (pos > 0 or allow_ask[i])
        if a <= b:                 # crossed or locked book: stand aside
            want_b = want_a = False
        if want_b:
            if qb != b:
                qb, ahead_b = b, bsz[i]
            last_b = bsz[i]
        else:
            qb = None
        if want_a:
            if qa != a:
                qa, ahead_a = a, asz[i]
            last_a = asz[i]
        else:
            qa = None
    return (np.array(F_i, dtype=np.int64), np.array(F_side, dtype=np.int64),
            np.array(F_px, dtype=np.int64), np.array(F_forced, dtype=np.int64))


def session_pnl(fills, session, fee_ticks):
    """Net profit per session in ticks per lot (every segment ends flat)."""
    i, side, px, _ = fills
    cash = -side * px - fee_ticks
    return pd.Series(cash, dtype=float).groupby(session[i]).sum()


def markout(fills, mid, seg, h=60):
    """Mean mid move h seconds after each passive fill, in our favour (ticks)."""
    i, side, px, forced = fills
    i, side, px = i[forced == 0], side[forced == 0], px[forced == 0]
    j = np.minimum(i + h, len(mid) - 1)
    same = seg[j] == seg[i]
    return float(np.mean(side[same] * (mid[j[same]] - px[same]))) if same.any() else float("nan")


def paired_t(a, b):
    d = (a - b).dropna()
    if len(d) < 3 or d.std(ddof=1) == 0:
        return float(d.mean()) if len(d) else float("nan"), float("nan")
    return float(d.mean()), float(d.mean() / d.std(ddof=1) * np.sqrt(len(d)))


def policy_flags(name, sec, minutes, fc_ridge, fc_kernel, book, eligible, rng):
    if name in ("vol", "vol_book"):
        g = minute_flags(sec, minutes, gate(minutes, fc_ridge))
    elif name == "vol_kernel":
        g = minute_flags(sec, minutes, gate(minutes, fc_kernel))
    elif name == "random":
        frac = minute_flags(sec, minutes, gate(minutes, fc_ridge))[eligible].mean()
        g = minute_flags(sec, minutes, rng.random(len(minutes)) < frac)
    else:
        g = np.ones(len(sec), bool)
    g = g & eligible
    ab, aa = g.copy(), g.copy()
    if name in ("book", "vol_book"):
        ab &= book >= -BOOK_K
        aa &= book <= BOOK_K
    return ab, aa


def run(symbol, seed=0):
    tick = TICK[symbol]
    g = to_grid(load(symbol))
    z = np.load(os.path.join(_ROOT, "data", f"kernel_paired_{symbol}.npz"))
    minutes, fr, fk = z["minute"], z["fvol1_ridge"], z["fvol1_hybrid"]
    valid = np.isfinite(g[["bid", "ask", "bsz", "asz"]].to_numpy()).all(axis=1)
    seg = valid_segments(g["seg"].to_numpy(), valid)
    g = g[valid]
    sec = g.index.to_numpy()
    bid = np.rint(g["bid"].to_numpy() / tick).astype(np.int64)
    ask = np.rint(g["ask"].to_numpy() / tick).astype(np.int64)
    bsz, asz = g["bsz"].to_numpy(float), g["asz"].to_numpy(float)
    mid = (bid + ask) / 2.0
    book = (bsz - asz) / np.maximum(bsz + asz, 1)
    session = (sec + 2 * 3600) // 86400
    seg = split_sessions(seg, session)
    eligible = minute_flags(sec, minutes, np.isfinite(fr))
    # a session counts only with at least MIN_SESSION_SECONDS of test data;
    # this drops one-second stubs where a segment runs onto a boundary
    counts = pd.Series(session[eligible]).value_counts()
    test_sessions = np.sort(counts.index[counts >= MIN_SESSION_SECONDS].to_numpy())
    eligible &= np.isin(session, test_sessions)
    fee = FEE_USD / TICK_USD[symbol]
    print(f"\n{symbol}: dropped {(~valid).sum():,} seconds with one side of the book empty", flush=True)
    print(f"{symbol}: {eligible.sum():,} test seconds in {len(np.unique(session[eligible]))} sessions",
          flush=True)
    rows, per = [], {}
    for mode in FILLS:
        for name in POLICIES:
            rng = np.random.default_rng(seed)
            ab, aa = policy_flags(name, sec, minutes, fr, fk, book, eligible, rng)
            f = simulate(bid, ask, bsz, asz, seg, ab, aa, mode)
            assert np.isin(session[f[0]], test_sessions).all(), "fill outside the scored sessions"
            net = session_pnl(f, session, fee).reindex(test_sessions, fill_value=0.0)
            gross = session_pnl(f, session, 0.0).reindex(test_sessions, fill_value=0.0)
            per[(mode, name)] = net * TICK_USD[symbol]
            m, t = paired_t(net, net * 0)
            rows.append({
                "symbol": symbol, "fill": mode, "policy": name,
                "quoting_share": float(((ab | aa) & eligible).sum() / eligible.sum()),
                "fills_per_session": len(f[0]) / len(test_sessions),
                "forced_share": float(f[3].mean()) if len(f[3]) else float("nan"),
                "markout60_ticks": markout(f, mid, seg),
                "gross_ticks_per_fill": float(gross.sum() / max(len(f[0]), 1)),
                "net_usd_per_session": m * TICK_USD[symbol], "net_t": t,
                "gross_usd_per_session": float(gross.mean()) * TICK_USD[symbol],
                "sharpe_ann": float(net.mean() / net.std(ddof=1) * np.sqrt(252)) if net.std() > 0 else float("nan"),
            })
            r = rows[-1]
            print(f"  {mode:<7} {name:<10} quoting {r['quoting_share']:4.0%}  fills/sess {r['fills_per_session']:7.1f}"
                  f"  markout60 {r['markout60_ticks']:+.2f}t  gross/fill {r['gross_ticks_per_fill']:+.3f}t"
                  f"  net ${r['net_usd_per_session']:+8.1f}/sess (t {t:+.1f})", flush=True)
        for a, b in (("vol", "always"), ("vol", "random"), ("vol_kernel", "vol"), ("vol_book", "book")):
            d, t = paired_t(per[(mode, a)], per[(mode, b)])
            rows.append({"symbol": symbol, "fill": mode, "policy": f"{a} - {b}",
                         "net_usd_per_session": d, "net_t": t})
            print(f"  {mode:<7} {a} - {b}: ${d:+.1f}/sess (t {t:+.1f})", flush=True)
    pd.DataFrame({f"{m}_{p}": s for (m, p), s in per.items()}).to_csv(
        os.path.join(_ROOT, "data", f"mm_sessions_{symbol}.csv"))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    print("RULE (fixed before running): the volatility gate earns a place if 'vol' beats both")
    print("'always' and 'random' with paired t >= 2 on 3 of 4 contracts under both fill rules.")
    print(f"Fees ${FEE_USD:.2f}/side; max hold {MAX_HOLD}s; gate below the trailing median of "
          f"{GATE_WINDOW:,} forecasts.", flush=True)
    rows = []
    for s in a.symbols.split(","):
        rows += run(s.strip().upper())
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(_ROOT, "data", "mm_backtest.csv"), index=False)
    print("\nVerdict (contracts where vol beats always AND random with t >= 2):")
    for mode in FILLS:
        n = 0
        for s in df.symbol.unique():
            ok = all(((df.symbol == s) & (df.fill == mode) & (df.policy == p)
                      & (df.net_usd_per_session > 0) & (df.net_t >= 2)).any()
                     for p in ("vol - always", "vol - random"))
            n += ok
        print(f"  {mode:<7} {n}/{df.symbol.nunique()}{'  EARNS A PLACE' if n >= 3 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
