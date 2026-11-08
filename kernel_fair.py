"""
A fair kernel, and a learning curve: does the kernel pull ahead of ridge as it
gets more history?

    .venv/bin/python kernel_fair.py                      # CL, NG, HG, ZC
    .venv/bin/python kernel_fair.py --symbols ZC

kernel_retest.py / kernel_paired.py found a tuned RBF kernel layered on ridge
improves next 1 and 5 minutes' volatility forecasts by a real but tiny amount
(paired IC gain ~0.002-0.014). Tuning chose the smoothest kernel on offer
almost every time. Three things in that setup could have forced the kernel
toward linear, all fixed here:

  1. Hour of day. It went into the kernel as 24 standardised one-hot columns,
     about +-4.8 each, which outweighed all 15 order-flow features in the
     kernel's distances. Here the hour stays in the linear part (one-hot, in
     ridge) and the kernel sees it only as sin/cos of the time of day.
  2. Too few random features (1,000), which make sharp kernels look noisy.
     Here: 4,000 for the final fit (2,000 while tuning), computed in chunks so
     every row in the window fits in memory.
  3. Heavy tails. Each feature is clipped at its training window's 0.5 and
     99.5 percentiles before standardising (for ridge too, so ridge gets the
     same inputs).

The gamma grid now reaches ten times smoother than before, so if tuning still
picks the bottom we know the data, not the grid, wants linear.

Learning curve: at each refit (every 40 sessions), ridge and the hybrid (ridge
plus the kernel fitted to ridge's residuals) are trained on the last 60, 120
and 240 sessions and on all history. Hyperparameters are tuned once per refit
on the most recent 60,000 training rows (first 80% fit, last 20% score, by
time) and shared by every window. Score: per session, IC(hybrid) - IC(ridge
on the same window); mean and t across sessions. If the gain grows with the
window, more history helps the kernel; if it is flat, a decade will not change
the answer. Writes data/kernel_fair.csv.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV

from horizon_check import TICK, load, to_grid
from kernel_paired import paired
from kernel_retest import drop_expiry
from minute_check import FEATURES, features, minute_bars

_ROOT = os.path.dirname(os.path.abspath(__file__))
ALPHAS = np.logspace(-2, 4, 13)
GAMMA_SCALE = (0.01, 0.03, 0.1, 0.3, 1.0, 3.0)   # x 1 / kernel inputs
TUNE_ALPHAS = np.logspace(-2, 3, 6)
D_TUNE, D_FIT = 2000, 4000
TUNE_ROWS = 60_000
WINDOWS = (60, 120, 240, None)                   # sessions of history; None = all
TARGETS = ("fvol1", "fvol5")
CHUNK = 10_000


def hours(df):
    return ((df["minute"].to_numpy() // 60) % 24).astype(int)


def inputs(df, tr):
    """(linear design, kernel design), clipped and standardised on rows tr."""
    F = df[list(FEATURES)].to_numpy(float)
    lo, hi = np.nanpercentile(F[tr], 0.5, axis=0), np.nanpercentile(F[tr], 99.5, axis=0)
    F = np.clip(F, lo, hi)
    mu, sd = np.nanmean(F[tr], 0), np.nanstd(F[tr], 0)
    sd[sd == 0] = 1.0
    F = (F - mu) / sd
    h = hours(df)
    lin = np.hstack([F, np.eye(24)[h]])
    ang = 2 * np.pi * (df["minute"].to_numpy() % 1440) / 1440
    ker = np.hstack([F, np.sqrt(2) * np.c_[np.sin(ang), np.cos(ang)]])
    return lin, ker


class RFF:
    """Random Fourier features of exp(-gamma |x - y|^2)."""

    def __init__(self, d, n, gamma, seed=0):
        r = np.random.default_rng(seed)
        self.W = r.normal(scale=np.sqrt(2 * gamma), size=(d, n)).astype(np.float32)
        self.b = r.uniform(0, 2 * np.pi, n).astype(np.float32)
        self.s = np.float32(np.sqrt(2.0 / n))

    def __call__(self, X):
        return self.s * np.cos(X.astype(np.float32) @ self.W + self.b)


def gram(rff, X, y):
    """Z'Z, Z'y, column means of Z and mean of y, accumulated in chunks."""
    n, D = len(X), rff.W.shape[1]
    ZZ, Zy, zs = np.zeros((D, D)), np.zeros(D), np.zeros(D)
    for i in range(0, n, CHUNK):
        Z = rff(X[i:i + CHUNK])
        ZZ += (Z.T @ Z).astype(float)
        Zy += Z.T.astype(float) @ y[i:i + CHUNK]
        zs += Z.sum(0)
    zm, ym = zs / n, y.mean()
    return ZZ - n * np.outer(zm, zm), Zy - n * zm * ym, zm, ym


def predict(rff, X, beta, b0):
    out = np.empty(len(X))
    for i in range(0, len(X), CHUNK):
        out[i:i + CHUNK] = rff(X[i:i + CHUNK]).astype(float) @ beta + b0
    return out


def fit_kernel(X, y, gamma, alpha, n, seed=0):
    rff = RFF(X.shape[1], n, gamma, seed)
    ZZ, Zy, zm, ym = gram(rff, X, y)
    beta = np.linalg.solve(ZZ + alpha * np.eye(len(Zy)), Zy)
    return rff, beta, ym - zm @ beta


def tune(X, y, seed=0):
    """Gamma scale and penalty by time-ordered 80/20 holdout on X, y."""
    cut = int(len(y) * 0.8)
    d = X.shape[1]
    best = None
    for gs in GAMMA_SCALE:
        rff = RFF(d, D_TUNE, gs / d, seed)
        ZZ, Zy, zm, ym = gram(rff, X[:cut], y[:cut])
        e, V = np.linalg.eigh(ZZ)
        Vy = V.T @ Zy
        Zv = rff(X[cut:]).astype(float)
        for a in TUNE_ALPHAS:
            beta = V @ (Vy / (np.maximum(e, 0) + a))
            err = np.mean((Zv @ beta + ym - zm @ beta - y[cut:]) ** 2)
            if best is None or err < best[0]:
                best = (err, gs, a)
    return best[1], best[2]


def walk_forward(df, ycol, start_frac=0.3, step=40, windows=WINDOWS, seed=0, n_fit=D_FIT):
    """Out-of-sample ridge and hybrid forecasts per history window."""
    y = df[ycol].to_numpy(float)
    sess = df["session"].to_numpy()
    us = np.unique(sess)
    out = {(w, m): np.full(len(df), np.nan) for w in windows for m in ("ridge", "hybrid")}
    chosen = []
    F = df[list(FEATURES)].to_numpy(float)
    okF = np.all(np.isfinite(F), axis=1)
    ok = okF & np.isfinite(y)
    for k in range(int(len(us) * start_frac), len(us), step):
        te_s = us[k:k + step]
        tr_all = np.flatnonzero(ok & (sess < te_s[0]))
        te = np.flatnonzero(np.isin(sess, te_s) & okF)
        if len(tr_all) < 2000 or not len(te):
            continue
        lin, ker = inputs(df, tr_all)
        # Tune on the most recent rows, on ridge's residuals there.
        tt = tr_all[-TUNE_ROWS:]
        r0 = RidgeCV(alphas=ALPHAS).fit(lin[tt], y[tt])
        gs, a = tune(ker[tt], y[tt] - r0.predict(lin[tt]), seed)
        chosen.append(gs)
        for w in windows:
            tr = tr_all if w is None else tr_all[sess[tr_all] >= us[max(0, k - w)]]
            if len(tr) < 2000:
                continue
            r = RidgeCV(alphas=ALPHAS).fit(lin[tr], y[tr])
            base_tr, base_te = r.predict(lin[tr]), r.predict(lin[te])
            rff, beta, b0 = fit_kernel(ker[tr], y[tr] - base_tr, gs / ker.shape[1], a, n_fit, seed)
            out[(w, "ridge")][te] = base_te
            out[(w, "hybrid")][te] = base_te + predict(rff, ker[te], beta, b0)
    return out, chosen


def run(symbol):
    tick = TICK[symbol]
    g = to_grid(load(symbol))
    df = drop_expiry(features(minute_bars(g, tick), tick), g)
    print(f"\n{symbol}: {len(df):,} minute bars, {df['session'].nunique()} sessions", flush=True)
    rows = []
    for ycol in TARGETS:
        h = int(ycol[-1])
        out, chosen = walk_forward(df, ycol)
        # Score every window on the same rows: those every window forecast.
        test = np.all([np.isfinite(out[(w, "ridge")]) for w in WINDOWS], axis=0)
        cells = []
        for w in WINDOWS:
            r = np.where(test, out[(w, "ridge")], np.nan)
            hy = np.where(test, out[(w, "hybrid")], np.nan)
            dif, t = paired(df, hy, r, ycol, h=h)
            label = "all" if w is None else f"{w}"
            rows.append({"symbol": symbol, "target": ycol, "window_sessions": label,
                         "hybrid_minus_ridge": dif, "t": t})
            cells.append(f"{label:>3}: {dif:+.4f} (t {t:+.1f})")
        gsm = pd.Series(chosen).value_counts().to_dict()
        print(f"  {ycol:<6} hybrid - ridge by history  " + "   ".join(cells)
              + f"   [gamma scale chosen: {gsm}]", flush=True)
        rows[-1]["gamma_choices"] = str(gsm)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    print("Paired per-session IC gain of the fixed hybrid over ridge trained on the same")
    print("history (last 60 / 120 / 240 sessions / all). Grows with history -> more data helps.",
          flush=True)
    rows = []
    for s in a.symbols.split(","):
        rows += run(s.strip().upper())
    pd.DataFrame(rows).to_csv(os.path.join(_ROOT, "data", "kernel_fair.csv"), index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
