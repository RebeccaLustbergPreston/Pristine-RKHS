"""
Does order flow predict the price at short horizons? Seconds to half an hour.

    .venv/bin/python horizon_check.py                    # CL, NG, HG, ZC
    .venv/bin/python horizon_check.py --symbols ZC

Reads the per-second book summaries written by depth_seconds.py. At each
horizon h, origins are spaced h seconds apart (non-overlapping), and each
origin t is the end of second t. Features use only seconds (t−h, t]:
    ofi       order-flow imbalance at the best quotes (Cont, Kukanov & Stoikov 2014)
    tflow     signed traded size, + buyer-initiated
    book      (bid size − ask size) / (bid size + ask size) at t
    depth5    the same over the top five levels
    pastret   the mid's own move over (t−h, t] — reversal or momentum
Outcome: the mid's move over (t, t+h], in ticks. The mid, not the last trade:
trade prices bounce between bid and ask, which manufactures predictability.
A window never spans a gap of more than 60 s or a contract switch.

Score: Spearman rank IC per session, averaged; t = mean / sd × √sessions,
so sessions are the independent units. "same window" scores ofi against the
move over (t−h, t] — order flow should explain moves as they happen, which
checks the plumbing, and is not a forecast.

Tradeable? For the top and bottom 10% of ofi (and of book) each session, the mean next-h
move in the signal's direction, against the spread at t: crossing the
spread to enter and exit costs one full spread round trip.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(_ROOT, "data", "depth_seconds")
TICK = {"CL": 0.01, "NG": 0.001, "HG": 0.0005, "ZC": 0.25}
HORIZONS = (1, 5, 10, 30, 60, 300, 1800)
MAX_GAP = 60
FEATURES = ("ofi", "tflow", "book", "depth5", "pastret")


def load(symbol):
    """All extracted months for a contract, concatenated in time order."""
    files = sorted(glob.glob(os.path.join(SRC, f"{symbol}_*.npz")))
    if not files:
        raise FileNotFoundError(f"no depth_seconds files for {symbol}")
    keys = ("sec", "bid", "ask", "bsz", "asz", "bdep", "adep", "ofi", "tflow", "ntr", "inst")
    parts = [np.load(f) for f in files]
    return {k: np.concatenate([p[k] for p in parts]) for k in keys}


def to_grid(d):
    """
    Regular 1-second grid, one per segment (no gap over MAX_GAP seconds, one
    instrument). Quotes carry forward; flows are zero in empty seconds.
    Returns a DataFrame indexed by second with a 'seg' column.
    """
    sec, inst = d["sec"].astype(np.int64), d["inst"]
    brk = np.r_[True, (np.diff(sec) > MAX_GAP) | (inst[1:] != inst[:-1])]
    seg = np.cumsum(brk) - 1
    frames = []
    for s in np.unique(seg):
        m = seg == s
        lo, hi = sec[m][0], sec[m][-1]
        if hi - lo < 600:
            continue
        df = pd.DataFrame({k: d[k][m] for k in d if k != "sec"}, index=sec[m])
        df = df.reindex(np.arange(lo, hi + 1))
        for k in ("ofi", "tflow", "ntr"):
            df[k] = df[k].fillna(0.0)
        df = df.ffill()
        df["seg"] = s
        frames.append(df)
    g = pd.concat(frames)
    g["mid"] = (g["bid"] + g["ask"]) / 2
    return g


def windows(g, h, tick):
    """Non-overlapping origins at horizon h with features and outcome."""
    rows = []
    for _, s in g.groupby("seg", sort=False):
        mid = s["mid"].to_numpy()
        n = len(s)
        cofi = np.r_[0.0, np.cumsum(s["ofi"].to_numpy())]
        ctfl = np.r_[0.0, np.cumsum(s["tflow"].to_numpy())]
        t = np.arange(h, n - h, h)                 # need (t−h, t] and (t, t+h]
        if not len(t):
            continue
        bsz, asz = s["bsz"].to_numpy()[t], s["asz"].to_numpy()[t]
        bd, ad = s["bdep"].to_numpy()[t], s["adep"].to_numpy()[t]
        rows.append(pd.DataFrame({
            "sec": s.index.to_numpy()[t],
            "ofi": cofi[t + 1] - cofi[t + 1 - h],
            "tflow": ctfl[t + 1] - ctfl[t + 1 - h],
            "book": (bsz - asz) / np.maximum(bsz + asz, 1),
            "depth5": (bd - ad) / np.maximum(bd + ad, 1),
            "pastret": (mid[t] - mid[t - h]) / tick,
            "fwd": (mid[t + h] - mid[t]) / tick,
            "spread": (s["ask"].to_numpy()[t] - s["bid"].to_numpy()[t]) / tick,
        }))
    w = pd.concat(rows, ignore_index=True)
    # Session: CME sessions start late afternoon Chicago; shifting by 2h puts
    # the daily halt, not midnight UTC, at the boundary.
    w["session"] = (w["sec"] + 2 * 3600) // 86400
    return w.dropna()


def session_ic(w, x, y="fwd", min_obs=10):
    """Mean of per-session Spearman ICs, its t-stat, and sessions used."""
    ics = []
    for _, s in w.groupby("session"):
        if len(s) < min_obs or s[x].nunique() < 2 or s[y].nunique() < 2:
            continue
        ics.append(s[x].rank().corr(s[y].rank()))
    ics = np.array([v for v in ics if np.isfinite(v)])
    if len(ics) < 3:
        return float("nan"), float("nan"), len(ics)
    return float(ics.mean()), float(ics.mean() / ics.std(ddof=1) * np.sqrt(len(ics))), len(ics)


def edge(w, x="ofi", q=0.1):
    """Mean next-h move (ticks) in the direction of the top/bottom decile signal; median spread."""
    out = []
    for _, s in w.groupby("session"):
        if len(s) < 50:
            continue
        lo, hi = s[x].quantile(q), s[x].quantile(1 - q)
        sel = s[(s[x] <= lo) | (s[x] >= hi)]
        sel = sel[sel[x] != 0]
        out.append(np.sign(sel[x]) * sel["fwd"])
    e = pd.concat(out) if out else pd.Series(dtype=float)
    return float(e.mean()) if len(e) else float("nan"), float(w["spread"].median())


def report(symbol):
    g = to_grid(load(symbol))
    tick = TICK[symbol]
    print(f"\n{symbol}: {len(g):,} seconds on the grid, "
          f"{g['seg'].nunique()} segments, median spread "
          f"{np.nanmedian((g['ask'] - g['bid']) / tick):.1f} tick(s)")
    head = "".join(f"{f:>13}" for f in FEATURES)
    print(f"  {'h':>6}{'n':>10}{'same wnd':>10}{head}{'ofi edge':>10}{'book edge':>11}{'spread':>8}")
    rows = []
    for h in HORIZONS:
        w = windows(g, h, tick)
        same = session_ic(w.assign(cur=w["pastret"]), "ofi", "cur")[0]
        cells, rec = "", {"symbol": symbol, "h": h, "n": len(w), "same": same}
        for f in FEATURES:
            ic, t, ns = session_ic(w, f)
            cells += f"{ic:>+7.3f} ({t:>+4.0f})" if np.isfinite(ic) else f"{'—':>13}"
            rec[f] = ic; rec[f + "_t"] = t
        e, sp = edge(w)
        eb, _ = edge(w, "book")
        rec.update(edge=e, book_edge=eb, spread=sp, sessions=ns)
        rows.append(rec)
        print(f"  {_fmt_h(h):>6}{len(w):>10,}{same:>+10.3f}{cells}{e:>+10.3f}{eb:>+11.3f}{sp:>8.1f}")
    return rows


def _fmt_h(h):
    return f"{h}s" if h < 60 else f"{h // 60}m"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    print("Rank IC with the next-h mid move: mean over sessions (t-stat across sessions).")
    print("'same wnd': ofi vs the move over the same h seconds (plumbing check, not a forecast).")
    print("'edge': mean next-h move in ticks, in the direction of the top/bottom 10% of ofi or book.")
    allrows = []
    for s in a.symbols.split(","):
        allrows += report(s.strip().upper())
    out = os.path.join(_ROOT, "data", "horizon_check.csv")
    pd.DataFrame(allrows).to_csv(out, index=False)
    print(f"\nTable: {out}")
    print("A forecast needs |t| ≥ 2 across sessions; a trade needs its edge above the spread.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
