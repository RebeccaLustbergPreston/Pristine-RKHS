"""
Kernel-style features at 1 and 5 minutes: direction and volatility.

    .venv/bin/python minute_check.py                     # CL, NG, HG, ZC

Entry 19 scored single book and flow features at fixed horizons. This puts
the pipeline's kind of features — order flow, a VPIN-style imbalance, a
Kyle's-lambda-style price impact, a Hawkes-style trade-intensity burst,
realised volatility — on 1-minute bars built from the per-second book
(depth_seconds.py), and asks two things at h = 1 and 5 minutes:

  direction   the mid's move over the next h minutes, in ticks
  volatility  log realised variance of the mid over the next h minutes,
              scored beyond trailing 30-minute realised variance (rank
              correlation after removing it from forecast and outcome, as
              in entry 16)

Forecasts, each walk-forward by session (expanding window from the first
30% of sessions, refit every 5 sessions, features standardised on training
rows only):
  book     book imbalance alone, no fit (the entry-19 benchmark)
  trailing trailing 30-minute realised variance alone
  har      the HAR benchmark: ridge on 1/5/30-minute realised variance and
           the hour of day — the standard simple volatility model
  ridge    RidgeCV on all features
  kernel   random Fourier features of an RBF kernel (as the pipeline's
           kernels use) + RidgeCV — the flexible model
Scores: per-session Spearman IC over non-overlapping origins, mean, t across
sessions. For direction, the edge: mean next-h move in the forecast's
direction over each session's top and bottom 10%, against the spread.

Features at minute m use only data up to the end of minute m; no rolling
window crosses a gap or a contract switch (the per-second segments). The
VPIN-style measure uses |net signed flow| over gross per-second flow, a
proxy: the per-second files keep net signed size, not gross volume.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd
from sklearn.kernel_approximation import RBFSampler
from sklearn.linear_model import RidgeCV

from horizon_check import TICK, load, to_grid
from scoring import _rank_resid

_ROOT = os.path.dirname(os.path.abspath(__file__))
HORIZONS = (1, 5)
ALPHAS = np.logspace(-2, 4, 13)
FEATURES = ("book", "depth5", "ofi1", "ofi5", "tflow1", "tflow5", "ret1", "ret5",
            "vpin30", "kyle30", "intensity", "rv1", "rv5", "rv30", "spread")
# The standard realised-volatility benchmark (HAR: Corsi 2009) — the last
# minute, 5 and 30 minutes of realised variance — plus the hour of day,
# since intraday volatility follows the clock. Both volatility models also
# get the hour, so "beyond HAR" measures the order-flow features only.
HAR = ("rv1", "rv5", "rv30")


def minute_bars(g: pd.DataFrame, tick: float) -> pd.DataFrame:
    """Per-second grid → 1-minute bars, within segments."""
    g = g.copy()
    g["dmid"] = g.groupby("seg")["mid"].diff().fillna(0.0) / tick
    g["tabs"] = g["tflow"].abs()
    g["minute"] = g.index // 60
    agg = g.groupby(["seg", "minute"], sort=True).agg(
        mid=("mid", "last"), bid=("bid", "last"), ask=("ask", "last"),
        bsz=("bsz", "last"), asz=("asz", "last"), bdep=("bdep", "last"), adep=("adep", "last"),
        ofi=("ofi", "sum"), tflow=("tflow", "sum"), tabs=("tabs", "sum"), ntr=("ntr", "sum"),
        rv=("dmid", lambda x: float((x ** 2).sum())), n=("mid", "size"))
    agg = agg[agg["n"] >= 30].reset_index()             # mostly-complete minutes
    agg["session"] = (agg["minute"] * 60 + 2 * 3600) // 86400
    return agg


def features(b: pd.DataFrame, tick: float) -> pd.DataFrame:
    """Causal features per minute bar, computed within each segment."""
    out = []
    for _, s in b.groupby("seg", sort=False):
        s = s.copy()
        dm = s["mid"].diff().fillna(0.0) / tick
        scale_o = s["ofi"].abs().rolling(60, min_periods=10).mean() + 1e-9
        scale_t = s["tabs"].rolling(60, min_periods=10).mean() + 1e-9
        s["book"] = (s["bsz"] - s["asz"]) / np.maximum(s["bsz"] + s["asz"], 1)
        s["depth5"] = (s["bdep"] - s["adep"]) / np.maximum(s["bdep"] + s["adep"], 1)
        s["ofi1"] = s["ofi"] / scale_o
        s["ofi5"] = s["ofi"].rolling(5).sum() / scale_o
        s["tflow1"] = s["tflow"] / scale_t
        s["tflow5"] = s["tflow"].rolling(5).sum() / scale_t
        s["ret1"] = dm
        s["ret5"] = dm.rolling(5).sum()
        s["vpin30"] = s["tflow"].rolling(30).sum().abs() / (s["tabs"].rolling(30).sum() + 1e-9)
        cov = (dm * s["tflow"]).rolling(30).mean() - dm.rolling(30).mean() * s["tflow"].rolling(30).mean()
        var = s["tflow"].rolling(30).var(ddof=0)
        s["kyle30"] = cov / (var + 1e-9) * scale_t
        s["intensity"] = np.log((s["ntr"].ewm(span=5).mean() + 1) / (s["ntr"].ewm(span=60).mean() + 1))
        s["rv1"] = np.log(s["rv"] + 1e-3)
        s["rv5"] = np.log(s["rv"].rolling(5).mean() + 1e-3)
        s["rv30"] = np.log(s["rv"].rolling(30).mean() + 1e-3)
        s["spread"] = (s["ask"] - s["bid"]) / tick
        mid = s["mid"].to_numpy()
        mins = s["minute"].to_numpy()
        rv = s["rv"].to_numpy()
        crv = np.r_[0.0, np.cumsum(rv)]
        n = len(s)
        for h in HORIZONS:
            fwd = np.full(n, np.nan)
            fvol = np.full(n, np.nan)
            fwd[:n - h] = (mid[h:] - mid[:n - h]) / tick
            fvol[:n - h] = np.log((crv[np.arange(h, n) + 1] - crv[np.arange(1, n - h + 1)]) / h + 1e-3)
            gap = np.ones(n, bool)                     # a dropped minute inside the window
            gap[:n - h] = mins[h:] - mins[:n - h] != h
            fwd[gap] = np.nan
            fvol[gap] = np.nan
            s[f"fwd{h}"] = fwd
            s[f"fvol{h}"] = fvol
        out.append(s)
    return pd.concat(out, ignore_index=True)


def _hours(df):
    h = ((df["minute"].to_numpy() // 60) % 24).astype(int)
    return np.eye(24)[h]


def walk_forward(df, ycol, kind, start_frac=0.3, step=5, seed=0, cols=FEATURES, hours=False):
    """Out-of-sample forecasts per row, by session blocks; NaN where none."""
    X = df[list(cols)].to_numpy(float)
    if hours:
        X = np.hstack([X, _hours(df)])
    y = df[ycol].to_numpy(float)
    sess = df["session"].to_numpy()
    us = np.unique(sess)
    pred = np.full(len(df), np.nan)
    ok = np.all(np.isfinite(X), axis=1) & np.isfinite(y)
    first = int(len(us) * start_frac)
    for k in range(first, len(us), step):
        test_s = us[k:k + step]
        tr = ok & (sess < test_s[0])
        te = np.isin(sess, test_s) & np.all(np.isfinite(X), axis=1)
        if tr.sum() < 500 or not te.any():
            continue
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd[sd == 0] = 1.0
        Xtr, Xte = (X[tr] - mu) / sd, (X[te] - mu) / sd
        if kind == "kernel":
            rff = RBFSampler(gamma=1.0 / len(cols), n_components=400, random_state=seed)
            Xtr, Xte = rff.fit_transform(Xtr), rff.transform(Xte)
        m = RidgeCV(alphas=ALPHAS).fit(Xtr, y[tr])
        pred[te] = m.predict(Xte)
    return pred


def session_score(df, pred, ycol, h, base=None, min_obs=10):
    """Mean per-session IC (or partial IC beyond ``base``) on origins h minutes apart."""
    d = df.assign(p=pred)
    d = d[np.isfinite(d["p"]) & np.isfinite(d[ycol])]
    d = d[(d["minute"] % h) == 0]
    ics = []
    for _, s in d.groupby("session"):
        if len(s) < min_obs or s["p"].nunique() < 2:
            continue
        if base is None:
            ics.append(s["p"].rank().corr(s[ycol].rank()))
        else:
            b = s[base].to_numpy()
            ra, rb = _rank_resid(s["p"].to_numpy(), b), _rank_resid(s[ycol].to_numpy(), b)
            if ra.std() > 1e-9 and rb.std() > 0:
                ics.append(np.corrcoef(ra, rb)[0, 1])
    ics = np.array([v for v in ics if np.isfinite(v)])
    if len(ics) < 3:
        return float("nan"), float("nan")
    return float(ics.mean()), float(ics.mean() / ics.std(ddof=1) * np.sqrt(len(ics)))


def edge(df, pred, h, q=0.1):
    d = df.assign(p=pred)
    d = d[np.isfinite(d["p"]) & np.isfinite(d[f"fwd{h}"]) & ((d["minute"] % h) == 0)]
    parts = []
    for _, s in d.groupby("session"):
        if len(s) < 50:
            continue
        lo, hi = s["p"].quantile(q), s["p"].quantile(1 - q)
        sel = s[(s["p"] <= lo) | (s["p"] >= hi)]
        parts.append(np.sign(sel["p"] - s["p"].median()) * sel[f"fwd{h}"])
    e = pd.concat(parts) if parts else pd.Series(dtype=float)
    return float(e.mean()) if len(e) else float("nan")


def report(symbol):
    tick = TICK[symbol]
    df = features(minute_bars(to_grid(load(symbol)), tick), tick)
    n_sess = df["session"].nunique()
    print(f"\n{symbol}: {len(df):,} minute bars, {n_sess} sessions, "
          f"median spread {df['spread'].median():.1f} tick(s)")
    rows = []
    for h in HORIZONS:
        f, v = f"fwd{h}", f"fvol{h}"
        # Direction
        res = {"book": df["book"].to_numpy(float)}
        for kind in ("ridge", "kernel"):
            res[kind] = walk_forward(df, f, kind)
        test = np.isfinite(res["ridge"])                 # score every forecast on the same rows
        cells = []
        for k, p in res.items():
            p = np.where(test, p, np.nan)
            ic, t = session_score(df, p, f, h)
            e = edge(df, p, h)
            cells.append(f"{k} IC {ic:+.3f} (t {t:+.1f}) edge {e:+.2f}")
            rows.append({"symbol": symbol, "h": h, "target": "direction", "forecast": k,
                         "ic": ic, "t": t, "edge_ticks": e, "spread": df["spread"].median()})
        print(f"  {h} min direction   " + "   ".join(cells))
        # Volatility
        vres = {"trailing": df["rv30"].to_numpy(float),
                "har": walk_forward(df, v, "ridge", cols=HAR, hours=True)}
        for kind in ("ridge", "kernel"):
            vres[kind] = walk_forward(df, v, kind, hours=True)
        test = np.isfinite(vres["ridge"]) & np.isfinite(vres["har"])
        dfv = df.assign(har_pred=np.where(test, vres["har"], np.nan))
        cells = []
        for k, p in vres.items():
            p = np.where(test, p, np.nan)
            ic, t = session_score(dfv, p, v, h)
            bi, bt = session_score(dfv, p, v, h, base="rv30")
            hi, ht = session_score(dfv, p, v, h, base="har_pred") if k in ("ridge", "kernel") \
                else (float("nan"), float("nan"))
            cells.append(f"{k} IC {ic:+.3f} >30m {bi:+.3f}" +
                         (f" >HAR {hi:+.3f} (t {ht:+.1f})" if np.isfinite(hi) else ""))
            rows.append({"symbol": symbol, "h": h, "target": "volatility", "forecast": k,
                         "ic": ic, "t": t, "beyond_trailing": bi, "beyond_t": bt,
                         "beyond_har": hi, "beyond_har_t": ht})
        print(f"  {h} min volatility  " + "   ".join(cells))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    print("Per-session rank IC (t across sessions). Direction edge: ticks per round trip")
    print("in the forecast's direction, top/bottom 10%; crossing costs about one spread.")
    rows = []
    for s in a.symbols.split(","):
        rows += report(s.strip().upper())
    out = os.path.join(_ROOT, "data", "minute_check.csv")
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\nTable: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
