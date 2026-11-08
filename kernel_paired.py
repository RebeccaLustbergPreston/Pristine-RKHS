"""
Does the tuned kernel make the volatility forecast better, or does it only
score well on "beyond ridge"? A paired check of kernel_retest.py.

    .venv/bin/python kernel_paired.py                    # CL, NG, HG, ZC
    .venv/bin/python kernel_paired.py --symbols ZC

kernel_retest.py found the tuned kernel and the hybrid "earn a place" on next
1 and 5 minutes' volatility on all four contracts, with beyond-ridge partial
correlations of +0.10 to +0.18. But the hybrid (ridge plus a kernel on ridge's
residuals) beat ridge's own IC by only 0.000 to 0.014. If the kernel really
held that much information ridge lacks, adding it should lift the IC by far
more. So the beyond-ridge score may be too generous when two forecasts are
correlated at 0.8 or more. Two checks, same walk-forward and data as
kernel_retest.py:

  paired     per session, IC(model) - IC(ridge) on the same rows; mean and t
             across sessions. This asks the question that matters: is the
             forecast better?
  placebo    a second ridge, the same linear model fitted on the thinned rows
             the kernels train on. It holds no information the first ridge
             lacks, so its beyond-ridge score is what the metric hands out for
             nothing. A kernel's beyond-ridge only means something above it.

Writes data/kernel_paired.csv and the forecasts to
data/kernel_paired_<SYM>.npz.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV

import kernel_retest as kr
from horizon_check import TICK, load, to_grid
from minute_check import FEATURES, features, minute_bars

_ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS = ("fvol1", "fvol5")
MODELS = ("ridge", "placebo", "tuned", "hybrid")


def walk_forward(df, ycol, start_frac=0.3, step=20, seed=0):
    """Out-of-sample forecasts of ridge, the placebo ridge, tuned and hybrid."""
    X = kr.design(df)
    y = df[ycol].to_numpy(float)
    sess = df["session"].to_numpy()
    us = np.unique(sess)
    d = len(FEATURES)
    out = {m: np.full(len(df), np.nan) for m in MODELS}
    okX = np.all(np.isfinite(X), axis=1)
    ok = okX & np.isfinite(y)
    for k in range(int(len(us) * start_frac), len(us), step):
        te_s = us[k:k + step]
        tr = np.flatnonzero(ok & (sess < te_s[0]))
        te = np.flatnonzero(np.isin(sess, te_s) & okX)
        if len(tr) < 2000 or not len(te):
            continue
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd[sd == 0] = 1.0
        Xs = (X - mu) / sd
        lin = RidgeCV(alphas=kr.ALPHAS).fit(Xs[tr], y[tr])
        out["ridge"][te] = lin.predict(Xs[te])
        kt = kr._thin(tr, kr.MAX_KERNEL_ROWS)
        out["placebo"][te] = RidgeCV(alphas=kr.ALPHAS).fit(Xs[kt], y[kt]).predict(Xs[te])
        r, m, _, _ = kr.fit_tuned(Xs[kt], y[kt], d, seed)
        out["tuned"][te] = m.predict(r.transform(Xs[te]))
        r2, m2, _, _ = kr.fit_tuned(Xs[kt], y[kt], d, seed, resid_of=lin.predict(Xs[kt]))
        out["hybrid"][te] = lin.predict(Xs[te]) + m2.predict(r2.transform(Xs[te]))
    return out


def paired(df, a, b, ycol, h=1, min_obs=10):
    """Mean per-session IC(a) - IC(b) on origins h minutes apart; t across sessions."""
    d = df.assign(a=a, b=b)
    d = d[np.isfinite(d["a"]) & np.isfinite(d["b"]) & np.isfinite(d[ycol]) & ((d["minute"] % h) == 0)]
    v = []
    for _, s in d.groupby("session"):
        if len(s) < min_obs or s["a"].nunique() < 2 or s["b"].nunique() < 2:
            continue
        ry = s[ycol].rank()
        v.append(s["a"].rank().corr(ry) - s["b"].rank().corr(ry))
    v = np.array([x for x in v if np.isfinite(x)])
    if len(v) < 3:
        return float("nan"), float("nan")
    return float(v.mean()), float(v.mean() / v.std(ddof=1) * np.sqrt(len(v)))


def run(symbol):
    tick = TICK[symbol]
    g = to_grid(load(symbol))
    df = kr.drop_expiry(features(minute_bars(g, tick), tick), g)
    print(f"\n{symbol}: {len(df):,} minute bars, {df['session'].nunique()} sessions", flush=True)
    rows, keep = [], {}
    for ycol in TARGETS:
        h = int(ycol[-1])
        preds = walk_forward(df, ycol)
        test = np.isfinite(preds["ridge"])
        base = np.where(test, preds["ridge"], np.nan)
        cells = []
        for mname in MODELS[1:]:
            p = np.where(test, preds[mname], np.nan)
            ic, _ = kr.session_scores(df, p, ycol, h=h)
            br, brt = kr.session_scores(df, p, ycol, base=base, h=h)
            dif, dt = paired(df, p, base, ycol, h=h)
            rows.append({"symbol": symbol, "target": ycol, "model": mname, "ic": ic,
                         "beyond_ridge": br, "beyond_ridge_t": brt,
                         "ic_minus_ridge": dif, "ic_minus_ridge_t": dt})
            cells.append(f"{mname} beyond {br:+.3f} | IC-ridge {dif:+.4f} (t {dt:+.1f})")
            keep[f"{ycol}_{mname}"] = preds[mname]
        keep[f"{ycol}_ridge"] = preds["ridge"]
        print(f"  {ycol:<6} " + "   ".join(cells), flush=True)
    np.savez_compressed(os.path.join(_ROOT, "data", f"kernel_paired_{symbol}.npz"),
                        minute=df["minute"].to_numpy(), session=df["session"].to_numpy(), **keep)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    print("beyond = rank partial correlation beyond ridge (the kernel_retest score);")
    print("IC-ridge = paired per-session IC improvement over ridge. The placebo is a")
    print("second linear ridge with no new information.", flush=True)
    rows = []
    for s in a.symbols.split(","):
        rows += run(s.strip().upper())
    pd.DataFrame(rows).to_csv(os.path.join(_ROOT, "data", "kernel_paired.csv"), index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
