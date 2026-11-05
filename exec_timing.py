"""
Does waiting for a favourable order book make a daily trade cheaper?

    .venv/bin/python exec_timing.py                      # CL, NG, HG, ZC

Decision log entry 19 found that book imbalance at the best quotes predicts
the mid over the next seconds to a minute, but by far less than the spread.
That cannot pay for a trade on its own; it might still make a trade we were
going to do anyway cheaper. This measures it on the per-second book from
depth_seconds.py.

Decision moments every 5 minutes, at seconds with a quote. At each one we
price a buy and a sell of one contract, so any drift in the price cancels
between them. Cost, in ticks, is the price paid (buy) or given up (sell)
against the mid at the decision moment:
    now      cross the spread at once
    book     wait while the book leans against us — for a buy, while
             book imbalance (bid size − ask size)/(bid size + ask size) is at
             or below −θ, i.e. sellers stacked at the best ask, the mid more
             likely to fall — then cross; cross anyway at W seconds
    random   cross at a uniformly random second within W, ignoring the
             book: the control for what waiting by itself does
Saving = now − policy. Means over decisions, t-stat across sessions.
Decisions only where a quote exists and the spread is at most MAX_SPREAD
ticks: a daily strategy trades in normal markets, not pre-open. A window
never runs past a gap or a contract switch. Taker orders only: a resting
limit order's fill depends on queue position, which this data cannot show.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

from horizon_check import TICK, load, to_grid

_ROOT = os.path.dirname(os.path.abspath(__file__))
WAITS = (10, 30, 60, 300)
THETAS = (0.3, 0.6)
EVERY = 300
MAX_SPREAD = 2          # ticks at the decision moment: a daily strategy trades in normal markets


def fills(bid, ask, book, t0, W, theta, rng):
    """
    Cost in ticks·tick (price units) of a buy and a sell decided at t0, for
    each policy, against mid[t0]. Arrays are one segment's 1-second grid.
    Returns dict policy → (buy_cost, sell_cost) arrays over t0.
    """
    mid0 = (bid[t0] + ask[t0]) / 2
    win = t0[:, None] + np.arange(W + 1)[None, :]          # seconds t0 .. t0+W
    bk = book[win]
    # Buy: proceed once book > −θ; sell: once book < +θ. Else wait to W.
    go_b = bk > -theta
    go_s = bk < theta
    go_b[:, -1] = True
    go_s[:, -1] = True
    jb = t0 + go_b.argmax(axis=1)
    js = t0 + go_s.argmax(axis=1)
    jr = t0 + rng.integers(0, W + 1, size=len(t0))
    return {
        "now": (ask[t0] - mid0, mid0 - bid[t0]),
        "book": (ask[jb] - mid0, mid0 - bid[js]),
        "random": (ask[jr] - mid0, mid0 - bid[jr]),
        "waited_b": jb - t0, "waited_s": js - t0,
    }


def run(symbol, seed=0):
    g = to_grid(load(symbol))
    tick = TICK[symbol]
    rng = np.random.default_rng(seed)
    rows = []
    for _, s in g.groupby("seg", sort=False):
        bid, ask = s["bid"].to_numpy(), s["ask"].to_numpy()
        bsz, asz = s["bsz"].to_numpy(), s["asz"].to_numpy()
        book = (bsz - asz) / np.maximum(bsz + asz, 1)
        sess = (s.index.to_numpy() + 2 * 3600) // 86400
        for W in WAITS:
            t0 = np.arange(0, len(s) - W - 1, EVERY)
            sp = (ask[t0] - bid[t0]) / tick
            t0 = t0[np.isfinite(sp) & (sp <= MAX_SPREAD + 1e-9)]
            if not len(t0):
                continue
            for th in THETAS:
                f = fills(bid, ask, book, t0, W, th, rng)
                now = (f["now"][0] + f["now"][1]) / 2 / tick
                for pol in ("book", "random"):
                    cost = (f[pol][0] + f[pol][1]) / 2 / tick
                    ok = np.isfinite(cost) & np.isfinite(now)
                    wait = (f["waited_b"] + f["waited_s"]) / 2 if pol == "book" \
                        else np.full(len(t0), W / 2)
                    rows.append(pd.DataFrame({
                        "W": W, "theta": th, "policy": pol, "session": sess[t0][ok],
                        "now": now[ok], "cost": cost[ok], "saving": (now - cost)[ok],
                        "wait": wait[ok],
                    }))
    return pd.concat(rows, ignore_index=True)


def summarise(df):
    out = []
    for (W, th, pol), d in df.groupby(["W", "theta", "policy"]):
        per = d.groupby("session")["saving"].mean()
        t = per.mean() / per.std(ddof=1) * np.sqrt(len(per)) if len(per) > 2 else np.nan
        out.append({"W": W, "theta": th, "policy": pol, "now": d["now"].mean(),
                    "saving": d["saving"].mean(), "t": t, "wait": d["wait"].mean(),
                    "decisions": len(d)})
    return pd.DataFrame(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    print("Saving in ticks per contract traded vs crossing at once (mean of a buy and a sell);")
    print("'now' = half-spread paid by crossing at once; t-stat across sessions.\n")
    allrows = []
    for sym in a.symbols.split(","):
        sym = sym.strip().upper()
        tab = summarise(run(sym))
        tab.insert(0, "symbol", sym)
        allrows.append(tab)
        print(f"{sym}  (cost of crossing now: {tab['now'].mean():.2f} ticks)")
        print(f"  {'W':>5}{'θ':>6}{'policy':>8}{'saving':>9}{'t':>7}{'mean wait':>11}{'decisions':>11}")
        for r in tab.itertuples():
            print(f"  {r.W:>4}s{r.theta:>6.1f}{r.policy:>8}{r.saving:>+9.3f}{r.t:>+7.1f}"
                  f"{r.wait:>10.0f}s{r.decisions:>11,}")
        print()
    out = os.path.join(_ROOT, "data", "exec_timing.csv")
    pd.concat(allrows).to_csv(out, index=False)
    print(f"Table: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
