"""
How long does the minute-level volatility forecaster take to train and to
use? The "could it run live?" numbers, measured on this laptop.

    .venv/bin/python timing_check.py                     # CL
    .venv/bin/python timing_check.py --symbol ZC

Uses kernel_fair.py's model exactly as tested (decision log 29): ridge on
the order-flow features plus hour of day, and an RBF kernel (4,000 random
Fourier features) fitted to ridge's errors, with its width and penalty tuned
on the most recent 60,000 rows. Training window: the last REFIT_SESSIONS
sessions (about a year, where the learning curve levels off).

Measures, each the median of REPEATS runs:
  features_per_minute   time to compute one minute's 15 features, averaged
                        over a batch of the whole history
  tune                  choosing the kernel's width and penalty
  fit_ridge, fit_kernel one refit of each on the training window
  predict_one           one forecast, ridge plus kernel, from one minute's
                        features (what a live system does each minute)

Writes data/timing_check.csv.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
import time

import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV

import kernel_fair as kf
from horizon_check import TICK, load, to_grid
from kernel_retest import drop_expiry
from minute_check import features, minute_bars

_ROOT = os.path.dirname(os.path.abspath(__file__))
REFIT_SESSIONS = 240
REPEATS = 3


def timed(fn, repeats=REPEATS):
    out, best = None, []
    for _ in range(repeats):
        t0 = time.perf_counter()
        out = fn()
        best.append(time.perf_counter() - t0)
    return out, float(np.median(best))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbol", default="CL")
    a = ap.parse_args(argv)
    sym = a.symbol.upper()
    tick = TICK[sym]
    g = to_grid(load(sym))
    bars = minute_bars(g, tick)
    df, t_feat = timed(lambda: features(bars, tick), repeats=1)
    df = drop_expiry(df, g)
    y = df["fvol1"].to_numpy(float)
    sess = df["session"].to_numpy()
    us = np.unique(sess)
    ok = np.all(np.isfinite(df[list(kf.FEATURES)].to_numpy(float)), axis=1) & np.isfinite(y)
    tr = np.flatnonzero(ok & (sess >= us[-REFIT_SESSIONS]))
    lin, ker = kf.inputs(df, tr)
    tt = tr[-kf.TUNE_ROWS:]
    r0 = RidgeCV(alphas=kf.ALPHAS).fit(lin[tt], y[tt])
    (gs, alpha), t_tune = timed(lambda: kf.tune(ker[tt], y[tt] - r0.predict(lin[tt])), repeats=1)
    ridge, t_ridge = timed(lambda: RidgeCV(alphas=kf.ALPHAS).fit(lin[tr], y[tr]))
    resid = y[tr] - ridge.predict(lin[tr])
    (rff, beta, b0), t_kernel = timed(
        lambda: kf.fit_kernel(ker[tr], resid, gs / ker.shape[1], alpha, kf.D_FIT), repeats=1)
    xl, xk = lin[tr[-1]:tr[-1] + 1], ker[tr[-1]:tr[-1] + 1]
    n_pred = 2000

    def predict_many():
        for _ in range(n_pred):
            ridge.predict(xl) + kf.predict(rff, xk, beta, b0)
    _, t_pred = timed(predict_many)
    rows = [
        {"step": "features_per_minute", "seconds": t_feat / len(bars)},
        {"step": "tune", "seconds": t_tune},
        {"step": "fit_ridge", "seconds": t_ridge},
        {"step": "fit_kernel", "seconds": t_kernel},
        {"step": "predict_one", "seconds": t_pred / n_pred},
    ]
    out = pd.DataFrame(rows)
    out.insert(0, "symbol", sym)
    out["training_rows"] = len(tr)
    out["machine"] = f"{platform.machine()} {platform.processor() or ''}".strip()
    out.to_csv(os.path.join(_ROOT, "data", "timing_check.csv"), index=False)
    print(f"{sym}: {len(tr):,} training rows ({REFIT_SESSIONS} sessions), {len(bars):,} minutes in all")
    for r in rows:
        s = r["seconds"]
        print(f"  {r['step']:<20} {s * 1e6:10.1f} microseconds" if s < 0.1 else
              f"  {r['step']:<20} {s:10.2f} seconds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
