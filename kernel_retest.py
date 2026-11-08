"""
A fair last test of the kernels, where the signal and the data both exist.

    .venv/bin/python kernel_retest.py                    # CL, NG, HG, ZC
    .venv/bin/python kernel_retest.py --symbols ZC

The kernels lost wherever data was scarce: ~300 daily rows (entries 11-18),
then three months of minute bars (entry 23), where an untuned RBF kernel
lost to ridge on every row. Flexible models need data; 18 months of 1-minute
bars is six times more. Same features and walk-forward as minute_check.py
(1-minute bars from the per-second book, expiry sessions dropped as in entry
24), four models, each with the hour of day:

  ridge    RidgeCV on the features (the linear benchmark)
  kernel   random Fourier features of an RBF kernel, 400 components,
           gamma = 1 / features: the untuned kernel of entry 23
  tuned    RBF random features, 1,000 components; gamma and the ridge
           penalty chosen on the last 20% of each training window (by time),
           then refit on the whole window. No test data touches the choice.
  hybrid   ridge, plus a tuned kernel fitted to ridge's residuals: the kernel
           only has to find what a linear model misses

Kernel models train on at most MAX_KERNEL_ROWS rows of each window (evenly
spaced in time) to fit in memory; ridge uses every row.

Targets (as entry 23): next 1 and 5 minutes' log realised variance, and next
1 and 5 minutes' mid move. Scores per session, t across sessions:
  ic            rank IC with the outcome
  beyond_ridge  rank partial correlation with the outcome after removing the
                ridge forecast from both: what the kernel adds over linear

RULE, FIXED BEFORE RUNNING: a kernel model earns a place for a target only
if beyond_ridge is positive with t >= 2 on at least 3 of the 4 contracts.
Writes data/kernel_retest.csv.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
from sklearn.kernel_approximation import RBFSampler
from sklearn.linear_model import Ridge, RidgeCV

from horizon_check import TICK, load, to_grid
from minute_check import FEATURES, features, minute_bars
from scoring import _rank_resid

_ROOT = os.path.dirname(os.path.abspath(__file__))
ALPHAS = np.logspace(-2, 4, 13)
GAMMA_SCALE = (0.1, 0.3, 1.0, 3.0)        # x 1 / number of features
TUNE_ALPHAS = (0.1, 1.0, 10.0, 100.0)
MAX_KERNEL_ROWS = 120_000
TARGETS = ("fvol1", "fvol5", "fwd1", "fwd5")
MODELS = ("ridge", "kernel", "tuned", "hybrid")


def drop_expiry(b, g):
    """Each contract's final session before a switch (entry 24)."""
    b = b.copy()
    b["inst"] = b["minute"].map(g.groupby(g.index // 60)["inst"].last())
    last = b.groupby("inst")["session"].max()
    final = b["inst"].iloc[-1]
    expiry = (b["inst"] != final) & (b["session"] == b["inst"].map(last))
    return b[~expiry].reset_index(drop=True)


def design(df):
    X = df[list(FEATURES)].to_numpy(float)
    h = ((df["minute"].to_numpy() // 60) % 24).astype(int)
    return np.hstack([X, np.eye(24)[h]])


def _thin(idx, n):
    return idx if len(idx) <= n else idx[np.linspace(0, len(idx) - 1, n).astype(int)]


def fit_tuned(Xtr, ytr, d, seed, resid_of=None):
    """Pick gamma and penalty on the last 20% of the window, refit on all of it."""
    cut = int(len(ytr) * 0.8)
    best = None
    for gs in GAMMA_SCALE:
        rff = RBFSampler(gamma=gs / d, n_components=1000, random_state=seed)
        Z = rff.fit_transform(Xtr)
        t = ytr if resid_of is None else ytr - resid_of
        for a in TUNE_ALPHAS:
            m = Ridge(alpha=a).fit(Z[:cut], t[:cut])
            err = np.mean((m.predict(Z[cut:]) - t[cut:]) ** 2)
            if best is None or err < best[0]:
                best = (err, gs, a)
    _, gs, a = best
    rff = RBFSampler(gamma=gs / d, n_components=1000, random_state=seed)
    t = ytr if resid_of is None else ytr - resid_of
    return rff, Ridge(alpha=a).fit(rff.fit_transform(Xtr), t), gs, a


def walk_forward(df, ycol, start_frac=0.3, step=20, seed=0):
    """Out-of-sample forecasts of every model; NaN where none."""
    X = design(df)
    y = df[ycol].to_numpy(float)
    sess = df["session"].to_numpy()
    us = np.unique(sess)
    d = len(FEATURES)
    out = {m: np.full(len(df), np.nan) for m in MODELS}
    okX = np.all(np.isfinite(X), axis=1)
    ok = okX & np.isfinite(y)
    chosen = []
    for k in range(int(len(us) * start_frac), len(us), step):
        te_s = us[k:k + step]
        tr = np.flatnonzero(ok & (sess < te_s[0]))
        te = np.flatnonzero(np.isin(sess, te_s) & okX)
        if len(tr) < 2000 or not len(te):
            continue
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd[sd == 0] = 1.0
        Xs = (X - mu) / sd
        lin = RidgeCV(alphas=ALPHAS).fit(Xs[tr], y[tr])
        out["ridge"][te] = lin.predict(Xs[te])
        kt = _thin(tr, MAX_KERNEL_ROWS)
        rff = RBFSampler(gamma=1.0 / d, n_components=400, random_state=seed)
        m = RidgeCV(alphas=ALPHAS).fit(rff.fit_transform(Xs[kt]), y[kt])
        out["kernel"][te] = m.predict(rff.transform(Xs[te]))
        r, m, gs, a = fit_tuned(Xs[kt], y[kt], d, seed)
        out["tuned"][te] = m.predict(r.transform(Xs[te]))
        r2, m2, gs2, a2 = fit_tuned(Xs[kt], y[kt], d, seed, resid_of=lin.predict(Xs[kt]))
        out["hybrid"][te] = lin.predict(Xs[te]) + m2.predict(r2.transform(Xs[te]))
        chosen.append((gs, a, gs2, a2))
    return out, chosen


def session_scores(df, pred, ycol, base=None, h=1, min_obs=10):
    d = df.assign(p=pred, b=np.nan if base is None else base)
    d = d[np.isfinite(d["p"]) & np.isfinite(d[ycol]) & ((d["minute"] % h) == 0)]
    if base is not None:
        d = d[np.isfinite(d["b"])]
    v = []
    for _, s in d.groupby("session"):
        if len(s) < min_obs or s["p"].nunique() < 2:
            continue
        if base is None:
            v.append(s["p"].rank().corr(s[ycol].rank()))
        else:
            ra = _rank_resid(s["p"].to_numpy(), s["b"].to_numpy())
            rb = _rank_resid(s[ycol].to_numpy(), s["b"].to_numpy())
            if ra.std() > 1e-9 and rb.std() > 0:
                v.append(np.corrcoef(ra, rb)[0, 1])
    v = np.array([x for x in v if np.isfinite(x)])
    if len(v) < 3:
        return float("nan"), float("nan")
    return float(v.mean()), float(v.mean() / v.std(ddof=1) * np.sqrt(len(v)))


def run(symbol):
    tick = TICK[symbol]
    g = to_grid(load(symbol))
    df = drop_expiry(features(minute_bars(g, tick), tick), g)
    print(f"\n{symbol}: {len(df):,} minute bars, {df['session'].nunique()} sessions", flush=True)
    rows = []
    for ycol in TARGETS:
        h = int(ycol[-1])
        preds, chosen = walk_forward(df, ycol)
        test = np.isfinite(preds["ridge"])
        cells = []
        for mname in MODELS:
            p = np.where(test, preds[mname], np.nan)
            ic, t = session_scores(df, p, ycol, h=h)
            row = {"symbol": symbol, "target": ycol, "model": mname, "ic": ic, "ic_t": t}
            if mname != "ridge":
                b, bt = session_scores(df, p, ycol, base=np.where(test, preds["ridge"], np.nan), h=h)
                row.update(beyond_ridge=b, beyond_ridge_t=bt)
                cells.append(f"{mname} {ic:+.3f} (beyond ridge {b:+.3f}, t {bt:+.1f})")
            else:
                cells.append(f"ridge {ic:+.3f}")
            rows.append(row)
        gs = pd.Series([c[0] for c in chosen]).mode()
        print(f"  {ycol:<6} " + " | ".join(cells)
              + (f"   [tuned gamma x{gs.iloc[0]:g} most often]" if len(gs) else ""), flush=True)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    print("RULE (fixed before running): a kernel model earns a place for a target only if it")
    print("adds beyond ridge with t >= 2 on at least 3 of the 4 contracts.")
    rows = []
    for s in a.symbols.split(","):
        rows += run(s.strip().upper())
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(_ROOT, "data", "kernel_retest.csv"), index=False)
    print("\nVerdict by target (contracts where the model adds beyond ridge with t >= 2):")
    for ycol in TARGETS:
        parts = []
        for mname in MODELS[1:]:
            s = df[(df.target == ycol) & (df.model == mname)]
            n = int(((s["beyond_ridge"] > 0) & (s["beyond_ridge_t"] >= 2)).sum())
            parts.append(f"{mname} {n}/{len(s)}{' EARNS A PLACE' if n >= 3 else ''}")
        print(f"  {ycol:<6} " + "   ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
