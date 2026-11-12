"""
Does a kernel beat a linear model at predicting direction over the next
few seconds, where the order-book signal is strongest?

    .venv/bin/python seconds_check.py                    # CL, NG, HG, ZC
    .venv/bin/python seconds_check.py --symbols ZC

Every earlier kernel test was at one minute or longer (entries 23, 28, 29),
where the kernel helped volatility a little and direction not at all. At
seconds, book imbalance predicts the next mid move strongly (entry 19), and
the published evidence for nonlinear models (queue imbalance, deep learning
on order books) is strongest. This tests that directly before anyone buys
finer data.

Data: the per-second book for 18 months (`depth_seconds`, via
horizon_check.py). At each horizon h (1, 5, 10 s), origins every SPACING
seconds; features from the state at the end of second t and the h seconds
before it; outcome the mid move over (t, t+h] in ticks. No window crosses a
segment break (a gap over a minute or a contract switch); each contract's
final session before a switch is dropped (entry 24); seconds with a crossed
or one-sided book are dropped.

Features: book imbalance; top-five depth imbalance; spread in ticks; log size
at the best bid and ask; order-flow imbalance, signed trade flow and trade
count over the last h seconds; the last h seconds' mid move; time of day as
sine and cosine. Clipped at the training window's 0.5/99.5 percentiles and
standardised on it (as kernel_fair.py).

Models, walk-forward by session (expanding window, first 30% of sessions to
start, refit every STEP sessions, training thinned to MAX_TRAIN rows evenly
over all past sessions):
  ridge     RidgeCV on the features
  placebo   a second ridge fitted on the same thinned rows the kernel uses;
            it holds no new information, so its gain over ridge shows what
            the paired score hands out for nothing
  hybrid    ridge plus an RBF kernel (4,000 random Fourier features) fitted
            to ridge's errors, width and penalty tuned on the most recent
            TUNE_ROWS training rows (kernel_fair.py's model, entry 29)

Score: per session, the rank IC of each model with the outcome; the paired
gain IC(hybrid) - IC(ridge) and IC(placebo) - IC(ridge), mean and t across
sessions (entry 28's rule for reporting improvements).

RULE, FIXED BEFORE RUNNING: at a given horizon the kernel earns a place for
seconds-level direction if hybrid - ridge is positive with t >= 2 on at
least 3 of the 4 contracts, and the placebo is not (t < 2 or negative).

Writes data/seconds_check.csv.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV

import kernel_fair as kf
from horizon_check import TICK, load, to_grid

_ROOT = os.path.dirname(os.path.abspath(__file__))
HORIZONS = (1, 5, 10)
SPACING = 10
STEP = 40
MAX_TRAIN = 300_000
TUNE_ROWS = 60_000
ALPHAS = np.logspace(-2, 4, 13)
FEATURES = ("book", "depth5", "spread", "lbsz", "lasz", "ofi_h", "tflow_h", "ntr_h", "ret_h",
            "tod_sin", "tod_cos")


def drop_expiry_seconds(inst, session):
    """False for each contract's final session before a switch."""
    df = pd.DataFrame({"inst": inst, "session": session})
    last = df.groupby("inst")["session"].transform("max")
    final_inst = inst[-1]
    return ~((inst != final_inst) & (session == last.to_numpy()))


def window_sum(x, h, seg):
    """Sum of x over (t-h, t] within a segment; NaN where the window crosses one."""
    c = np.r_[0.0, np.cumsum(np.nan_to_num(x))]
    n = len(x)
    out = np.full(n, np.nan)
    idx = np.arange(h, n)
    ok = seg[idx] == seg[idx - h]
    out[idx[ok]] = c[idx[ok] + 1] - c[idx[ok] - h + 1]
    return out


def build(g, tick, h, spacing=SPACING):
    """Feature rows at origins every `spacing` seconds, with the h-second outcome."""
    sec = g.index.to_numpy()
    seg = g["seg"].to_numpy()
    bid, ask = g["bid"].to_numpy(float), g["ask"].to_numpy(float)
    bsz, asz = g["bsz"].to_numpy(float), g["asz"].to_numpy(float)
    bdep, adep = g["bdep"].to_numpy(float), g["adep"].to_numpy(float)
    mid = (bid + ask) / 2.0
    n = len(sec)
    valid = np.isfinite(bid) & np.isfinite(ask) & (ask > bid) & (bsz > 0) & (asz > 0)
    session = (sec + 2 * 3600) // 86400
    keep = valid & drop_expiry_seconds(g["inst"].to_numpy(), session)
    t = np.arange(h, n - h)
    t = t[(sec[t] % spacing) == 0]
    ok = (seg[t + h] == seg[t]) & (seg[t - h] == seg[t]) & keep[t] & keep[t + h] & keep[t - h]
    t = t[ok]
    f = {
        "book": ((bsz - asz) / (bsz + asz))[t],
        "depth5": ((bdep - adep) / np.maximum(bdep + adep, 1))[t],
        "spread": ((ask - bid) / tick)[t],
        "lbsz": np.log1p(bsz)[t],
        "lasz": np.log1p(asz)[t],
        "ofi_h": window_sum(g["ofi"].to_numpy(float), h, seg)[t],
        "tflow_h": window_sum(g["tflow"].to_numpy(float), h, seg)[t],
        "ntr_h": window_sum(g["ntr"].to_numpy(float), h, seg)[t],
        "ret_h": ((mid[t] - mid[t - h]) / tick),
    }
    ang = 2 * np.pi * (sec[t] % 86400) / 86400
    f["tod_sin"], f["tod_cos"] = np.sin(ang), np.cos(ang)
    df = pd.DataFrame(f)
    df["y"] = (mid[t + h] - mid[t]) / tick
    df["session"] = session[t]
    df["sec"] = sec[t]
    return df.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)


def design(df, tr):
    """Features clipped and standardised on training rows tr."""
    F = df[list(FEATURES)].to_numpy(float)
    lo, hi = np.percentile(F[tr], 0.5, axis=0), np.percentile(F[tr], 99.5, axis=0)
    F = np.clip(F, lo, hi)
    mu, sd = F[tr].mean(0), F[tr].std(0)
    sd[sd == 0] = 1.0
    return (F - mu) / sd


def thin(idx, n):
    return idx if len(idx) <= n else idx[np.linspace(0, len(idx) - 1, n).astype(int)]


def walk_forward(df, start_frac=0.3, step=STEP, max_train=MAX_TRAIN, n_fit=kf.D_FIT, seed=0):
    y = df["y"].to_numpy(float)
    sess = df["session"].to_numpy()
    us = np.unique(sess)
    out = {m: np.full(len(df), np.nan) for m in ("ridge", "placebo", "hybrid")}
    chosen = []
    for k in range(int(len(us) * start_frac), len(us), step):
        te_s = us[k:k + step]
        tr_all = np.flatnonzero(sess < te_s[0])
        te = np.flatnonzero(np.isin(sess, te_s))
        if len(tr_all) < 2000 or not len(te):
            continue
        X = design(df, tr_all)
        ridge = RidgeCV(alphas=ALPHAS).fit(X[tr_all], y[tr_all])
        out["ridge"][te] = ridge.predict(X[te])
        tr = thin(tr_all, max_train)
        out["placebo"][te] = RidgeCV(alphas=ALPHAS).fit(X[tr], y[tr]).predict(X[te])
        resid = y[tr] - ridge.predict(X[tr])
        tt = tr[-TUNE_ROWS:]
        gs, a = kf.tune(X[tt], y[tt] - ridge.predict(X[tt]), seed)
        chosen.append(gs)
        rff, beta, b0 = kf.fit_kernel(X[tr], resid, gs / X.shape[1], a, n_fit, seed)
        out["hybrid"][te] = out["ridge"][te] + kf.predict(rff, X[te], beta, b0)
    return out, chosen


def session_ic(df, pred, min_obs=30):
    d = pd.DataFrame({"p": pred, "y": df["y"].to_numpy(), "s": df["session"].to_numpy()})
    d = d[np.isfinite(d["p"])]
    d["rp"] = d.groupby("s")["p"].rank()
    d["ry"] = d.groupby("s")["y"].rank()
    g = d.groupby("s")
    n = g.size()
    ic = g.apply(lambda x: x["rp"].corr(x["ry"]) if x["p"].nunique() > 1 else np.nan,
                 include_groups=False)
    return ic[n >= min_obs].dropna()


def paired(a, b):
    d = (a - b).dropna()
    if len(d) < 3 or d.std(ddof=1) == 0:
        return float("nan"), float("nan")
    return float(d.mean()), float(d.mean() / d.std(ddof=1) * np.sqrt(len(d)))


def run(symbol, horizons=HORIZONS):
    tick = TICK[symbol]
    g = to_grid(load(symbol))
    rows = []
    for h in horizons:
        df = build(g, tick, h)
        out, chosen = walk_forward(df)
        ic = {m: session_ic(df, out[m]) for m in out}
        book = session_ic(df, df["book"].to_numpy())
        common = ic["ridge"].index
        hy, ht = paired(ic["hybrid"].reindex(common), ic["ridge"])
        pl, pt = paired(ic["placebo"].reindex(common), ic["ridge"])
        r = {"symbol": symbol, "horizon_s": h, "rows": len(df), "sessions": len(common),
             "book_ic": float(book.reindex(common).mean()), "ridge_ic": float(ic["ridge"].mean()),
             "hybrid_minus_ridge": hy, "hybrid_t": ht, "placebo_minus_ridge": pl, "placebo_t": pt,
             "gamma_choices": str(pd.Series(chosen).value_counts().to_dict())}
        rows.append(r)
        print(f"  {symbol} h={h:>2}s  rows {len(df):,}  book IC {r['book_ic']:+.3f}  ridge IC "
              f"{r['ridge_ic']:+.3f}  kernel gain {hy:+.4f} (t {ht:+.1f})  placebo {pl:+.4f} "
              f"(t {pt:+.1f})  gamma {r['gamma_choices']}", flush=True)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    print("RULE (fixed before running): the kernel earns a place at a horizon if hybrid - ridge")
    print("is positive with t >= 2 on 3 of 4 contracts and the placebo is not.", flush=True)
    rows = []
    for s in a.symbols.split(","):
        rows += run(s.strip().upper())
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(_ROOT, "data", "seconds_check.csv"), index=False)
    print("\nVerdict by horizon:")
    for h, d in df.groupby("horizon_s"):
        n = int(((d.hybrid_minus_ridge > 0) & (d.hybrid_t >= 2)).sum())
        bad = int(((d.placebo_minus_ridge > 0) & (d.placebo_t >= 2)).sum())
        print(f"  {h:>2}s: kernel passes on {n}/{len(d)}, placebo on {bad}/{len(d)}"
              + ("  EARNS A PLACE" if n >= 3 and bad == 0 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
