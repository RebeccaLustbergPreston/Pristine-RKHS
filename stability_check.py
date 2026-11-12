"""
Do the short-horizon results hold across the 18 months, or only in part of
them? A by-quarter breakdown of three results, from data already on disk:

    .venv/bin/python stability_check.py                  # CL, NG, HG, ZC

  book_ic       per-session rank IC of book imbalance at the end of a minute
                with the next minute's mid move (decision log 19, 27); no
                model, every session of the 18 months
  vol_ic        per-session rank IC of the walk-forward ridge forecast of the
                next minute's volatility with the outcome (kernel_paired.py's
                saved forecasts, entries 23, 27); test period only
  mm_vs_always  per-session net profit of the market-making forecast gate
  mm_vs_random  minus quoting all the time / standing aside at random,
                pessimistic fill rule (mm_backtest.py, entry 30)

Each is averaged over the sessions in each calendar quarter, with a t across
sessions. A result that holds should keep its sign, with t >= 2, in every
quarter. Writes data/stability_check.csv.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
SYMBOLS = ("CL", "NG", "HG", "ZC")


def session_dates(sessions):
    """Session number (days since 1970) to the exchange trade date."""
    return pd.to_datetime(np.asarray(sessions, np.int64), unit="D")


def quarter_of(sessions):
    return session_dates(sessions).to_period("Q").astype(str)


def per_session_ic(df, x, y, min_obs=10):
    """Spearman correlation of columns x and y within each session."""
    d = df[[x, y, "session"]].replace([np.inf, -np.inf], np.nan).dropna()
    out = {}
    for s, g in d.groupby("session"):
        if len(g) >= min_obs and g[x].nunique() > 1 and g[y].nunique() > 1:
            out[s] = g[x].rank().corr(g[y].rank())
    return pd.Series(out, dtype=float)


def by_quarter(per_session):
    """Mean, t and session count of a per-session series, by quarter."""
    s = per_session.dropna()
    q = pd.Series(quarter_of(s.index), index=s.index)
    rows = []
    for name, v in s.groupby(q):
        sd = v.std(ddof=1)
        t = v.mean() / sd * np.sqrt(len(v)) if len(v) > 2 and sd > 0 else np.nan
        rows.append({"quarter": name, "mean": float(v.mean()), "t": float(t), "sessions": len(v)})
    return pd.DataFrame(rows)


def model_rows(symbol):
    from horizon_check import TICK, load, to_grid
    from kernel_retest import drop_expiry
    from minute_check import features, minute_bars
    tick = TICK[symbol]
    g = to_grid(load(symbol))
    df = drop_expiry(features(minute_bars(g, tick), tick), g)
    out = {"book_ic": per_session_ic(df, "book", "fwd1")}
    z = np.load(os.path.join(_ROOT, "data", f"kernel_paired_{symbol}.npz"))
    assert (z["minute"] == df["minute"].to_numpy()).all()
    out["vol_ic"] = per_session_ic(df.assign(pred=z["fvol1_ridge"]), "pred", "fvol1")
    return out


def mm_rows(symbol):
    s = pd.read_csv(os.path.join(_ROOT, "data", f"mm_sessions_{symbol}.csv"), index_col=0)
    return {"mm_vs_always": s["through_vol"] - s["through_always"],
            "mm_vs_random": s["through_vol"] - s["through_random"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    a = ap.parse_args(argv)
    frames = []
    for sym in a.symbols.split(","):
        series = {**model_rows(sym), **mm_rows(sym)}
        for metric, ps in series.items():
            q = by_quarter(ps)
            q.insert(0, "metric", metric)
            q.insert(0, "symbol", sym)
            frames.append(q)
            cells = "  ".join(f"{r.quarter} {r['mean']:+.3f} (t {r.t:+.1f}, n {r.sessions})"
                              for _, r in q.iterrows())
            print(f"{sym} {metric:<13} {cells}", flush=True)
    pd.concat(frames).to_csv(os.path.join(_ROOT, "data", "stability_check.csv"), index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
