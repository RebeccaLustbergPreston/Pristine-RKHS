"""
Does volatility travel between markets? Minute-level spillover, eight contracts.

    DATA_SOURCE=cleaned .venv/bin/python spillover.py

For each contract B, forecast its realised volatility over the next h minutes
(h = 1, 5) two ways, walk-forward by session:
  own     ridge on B's own recent volatility (last 1, 5, 30 minutes: the HAR
          benchmark) and the half-hour of the day
  + cross the same plus the other seven contracts' volatility over their last
          1 and 5 minutes
and score only what the cross terms add: the rank correlation of the +cross
forecast with the outcome after removing the own forecast from both, per
session, t across sessions. Then one other market at a time (A -> B), to see
who leads whom.

Data: cleaned 1-minute bars (switch gaps removed), 2 Jul 2023 - 31 Dec 2024,
on 8:30-13:15 Chicago, the hours all eight trade, on dates all eight trade.
A minute's variance is its Parkinson range estimator, (ln high/low)^2 / 4 ln 2,
which uses the whole bar and is less disturbed by bid-ask bounce than
close-to-close returns; a minute with no trade counts as no movement. Each
contract's last session before its series switches contract is dropped (thin
expiring books; entry 24). Writes data/spillover_{all,pairs}.csv.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV

from scoring import _rank_resid

_ROOT = os.path.dirname(os.path.abspath(__file__))
SYMBOLS = ("CL", "NG", "HG", "GC", "ZC", "ZT", "ES", "BTC")
HORIZONS = (1, 5)
OPEN, CLOSE = 8 * 60 + 30, 13 * 60 + 15
ALPHAS = np.logspace(-2, 4, 13)
CACHE = os.path.join(_ROOT, "data", "spillover_minutes.parquet")


def minute_variance(symbol):
    """Parkinson variance per minute on 8:30-13:15 Chicago, expiry sessions dropped."""
    from data_loader import DataLoader
    o = DataLoader(os.path.join(_ROOT, "data"), symbol, source="cleaned",
                   switch_gaps="remove").ohlcv()
    t = o.index.tz_convert("America/Chicago")
    tod = t.hour * 60 + t.minute
    keep = (tod >= OPEN) & (tod < CLOSE) & (t.dayofweek < 5)
    o, t = o[keep], t[keep]
    v = (np.log(o["high"].to_numpy() / o["low"].to_numpy())) ** 2 / (4 * np.log(2))
    df = pd.DataFrame({"date": t.date, "tod": t.hour * 60 + t.minute, "v": v,
                       "inst": o["instrument_id"].to_numpy()})
    last = df.groupby("inst")["date"].max()
    final = df["inst"].iloc[-1]
    expiry = (df["inst"] != final) & (df["date"] == df["inst"].map(last))
    return df[~expiry]


def panel():
    """Minute variance, dates x minutes x contracts, on dates all eight trade."""
    if os.path.exists(CACHE):
        return pd.read_parquet(CACHE)
    per = {s: minute_variance(s) for s in SYMBOLS}
    dates = sorted(set.intersection(*(set(d["date"]) for d in per.values())))
    idx = pd.MultiIndex.from_product([dates, range(OPEN, CLOSE)], names=["date", "tod"])
    out = pd.DataFrame(index=idx)
    for s, d in per.items():
        out[s] = d.set_index(["date", "tod"])["v"].reindex(idx).fillna(0.0)
    out = out.reset_index()
    out["date"] = pd.to_datetime(out["date"])
    out.to_parquet(CACHE)
    return out


def features(p):
    """Log variance over the last 1, 5, 30 minutes per contract, within each date."""
    f = pd.DataFrame({"date": p["date"], "tod": p["tod"]})
    g = p.groupby("date")
    # The floor added before logs comes from the first five dates only, which
    # are always warm-up (never scored): a full-sample statistic would let
    # future data shape past features (POSTMORTEM bug 5).
    warm = p["date"].isin(np.sort(p["date"].unique())[:5])
    for s in SYMBOLS:
        w = p.loc[warm, s]
        eps = w[w > 0].median() * 0.05
        f[f"{s}_1"] = np.log(p[s] + eps)
        f[f"{s}_5"] = np.log(g[s].transform(lambda x: x.rolling(5, min_periods=1).mean()) + eps)
        f[f"{s}_30"] = np.log(g[s].transform(lambda x: x.rolling(30, min_periods=1).mean()) + eps)
        for h in HORIZONS:
            fwd = g[s].transform(lambda x: x[::-1].rolling(h, min_periods=h).mean()[::-1].shift(-1))
            f[f"{s}_fwd{h}"] = np.log(fwd + eps)
    f["slot"] = (f["tod"] - OPEN) // 30
    return f


def walk_forward(f, cols, y, start_frac=0.3, step=10):
    X = np.hstack([f[cols].to_numpy(float), np.eye(int(f["slot"].max()) + 1)[f["slot"].to_numpy()]])
    yv = f[y].to_numpy(float)
    dates = f["date"].to_numpy()
    ud = np.unique(dates)
    pred = np.full(len(f), np.nan)
    ok = np.all(np.isfinite(X), axis=1) & np.isfinite(yv)
    for k in range(int(len(ud) * start_frac), len(ud), step):
        te_d = ud[k:k + step]
        tr = ok & (dates < te_d[0])
        te = np.isin(dates, te_d) & np.all(np.isfinite(X), axis=1)
        if tr.sum() < 1000 or not te.any():
            continue
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd[sd == 0] = 1.0
        m = RidgeCV(alphas=ALPHAS).fit((X[tr] - mu) / sd, yv[tr])
        pred[te] = m.predict((X[te] - mu) / sd)
    return pred


def beyond(f, pred, base, y, h):
    """Mean per-date rank partial correlation of pred with y beyond base; t across dates."""
    d = pd.DataFrame({"date": f["date"], "p": pred, "b": base, "y": f[y], "tod": f["tod"]})
    d = d[np.isfinite(d["p"]) & np.isfinite(d["b"]) & np.isfinite(d["y"]) & ((d["tod"] - OPEN) % h == 0)]
    vals = []
    for _, s in d.groupby("date"):
        if len(s) < 20:
            continue
        ra, rb = _rank_resid(s["p"].to_numpy(), s["b"].to_numpy()), _rank_resid(s["y"].to_numpy(), s["b"].to_numpy())
        if ra.std() > 1e-9 and rb.std() > 0:
            vals.append(np.corrcoef(ra, rb)[0, 1])
    v = np.array(vals)
    return float(v.mean()), float(v.mean() / v.std(ddof=1) * np.sqrt(len(v))), len(v)


def main():
    p = panel()
    f = features(p)
    print(f"Spillover: {f['date'].nunique()} dates all eight trade, "
          f"{len(f):,} minutes, 8:30-13:15 Chicago\n")
    rows_all, rows_pair = [], []
    for h in HORIZONS:
        print(f"next {h} min: information other markets add beyond B's own HAR + time of day")
        print(f"  {'B':<4}{'all 7 others':>16}{'t':>7}   strongest single leader")
        for b in SYMBOLS:
            own_cols = [f"{b}_1", f"{b}_5", f"{b}_30"]
            y = f"{b}_fwd{h}"
            own = walk_forward(f, own_cols, y)
            others = [s for s in SYMBOLS if s != b]
            cross = walk_forward(f, own_cols + [f"{s}_{k}" for s in others for k in (1, 5)], y)
            m, t, n = beyond(f, cross, own, y, h)
            rows_all.append({"h": h, "target": b, "beyond_own": m, "t": t, "dates": n})
            best = None
            for a in others:
                pa = walk_forward(f, own_cols + [f"{a}_1", f"{a}_5"], y)
                ma, ta, _ = beyond(f, pa, own, y, h)
                rows_pair.append({"h": h, "leader": a, "target": b, "beyond_own": ma, "t": ta})
                if best is None or ma > best[1]:
                    best = (a, ma, ta)
            print(f"  {b:<4}{m:>+16.3f}{t:>+7.1f}   {best[0]} ({best[1]:+.3f}, t {best[2]:+.1f})")
        print()
    pd.DataFrame(rows_all).to_csv(os.path.join(_ROOT, "data", "spillover_all.csv"), index=False)
    pd.DataFrame(rows_pair).to_csv(os.path.join(_ROOT, "data", "spillover_pairs.csv"), index=False)
    print("A market adds information only if 'beyond' is positive with t >= 2 (rule fixed in advance).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
