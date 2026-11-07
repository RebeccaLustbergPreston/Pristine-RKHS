"""
Does our order-flow information add to what crude's options market already
prices? Next-day and next-week realised volatility, July 2023 - December 2024.

    DATA_SOURCE=cleaned .venv/bin/python vol_vs_implied.py

Target: log realised variance of crude over the next session (h = 1) and the
mean over the next five (h = 5), from the cleaned 1-minute bars (Parkinson
range per minute, summed over the session; expiry sessions dropped as in
entry 24). Everything below is known at session d's close:
  HAR        log realised variance of day d, of d-4..d, of d-21..d (Corsi 2009)
  implied    log of ATM implied variance per day from LO option settlements at
             d (implied_vol.py; settlement is at 13:30, before the close)
  order flow end-of-session summaries from the per-second book
             (depth_seconds.py): mean |book imbalance|, |order-flow| and
             |trade flow| and trade count over the last hour, VPIN-style and
             Kyle-style measures, realised variance of the last 30 minutes
Models, walk-forward (expanding from 40% of days, refit every 20, label-
horizon purge): HAR; HAR + implied; HAR + implied + order flow. Scores: rank
IC with the outcome, and the rank partial correlation of each model beyond
the one before it. One series, so the no-skill s.e. is about 1/sqrt(days).
Writes data/vol_vs_implied.csv.
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV

from scoring import _rank_resid

_ROOT = os.path.dirname(os.path.abspath(__file__))
ALPHAS = np.logspace(-2, 4, 13)
HORIZONS = (1, 5)


def daily_rv():
    """Session realised variance of crude from cleaned 1-minute bars."""
    from data_loader import DataLoader
    o = DataLoader(os.path.join(_ROOT, "data"), "CL", source="cleaned", switch_gaps="remove").ohlcv()
    v = (np.log(o["high"].to_numpy() / o["low"].to_numpy())) ** 2 / (4 * np.log(2))
    df = pd.DataFrame({"session": pd.to_datetime(o["session_date"].to_numpy()), "v": v,
                       "inst": o["instrument_id"].to_numpy()})
    last = df.groupby("inst")["session"].max()
    final = df["inst"].iloc[-1]
    expiry = set(last[last.index != final])
    rv = df.groupby("session")["v"].sum()
    return rv[~rv.index.isin(expiry)]


def orderflow_daily():
    """End-of-session order-flow summaries for CL from the per-second book."""
    files = sorted(glob.glob(os.path.join(_ROOT, "data", "depth_seconds", "CL_*.npz")))
    if len(files) < 18:
        return None
    from horizon_check import load, to_grid
    from minute_check import features, minute_bars
    b = features(minute_bars(to_grid(load("CL")), 0.01), 0.01)
    t = pd.to_datetime(b["minute"] * 60, unit="s", utc=True).dt.tz_convert("America/Chicago")
    # Session = the trade date: after 17:00 Chicago belongs to the next day.
    b["session"] = (t + pd.Timedelta(hours=7)).dt.tz_localize(None).dt.normalize()
    b["abs_book"] = b["book"].abs()
    b["abs_ofi"] = b["ofi1"].abs()
    b["abs_tflow"] = b["tflow1"].abs()
    last_hour = b.groupby("session").tail(60)
    g = last_hour.groupby("session")
    out = pd.DataFrame({"of_book": g["abs_book"].mean(), "of_ofi": g["abs_ofi"].mean(),
                        "of_tflow": g["abs_tflow"].mean(), "of_ntr": np.log1p(g["ntr"].mean()),
                        "of_vpin": g["vpin30"].last(), "of_kyle": g["kyle30"].last(),
                        "of_rv30": g["rv30"].last(), "of_intensity": g["intensity"].last()})
    return out


def walk_forward(X, y, label_days, start_frac=0.4, step=20):
    n = len(y)
    pred = np.full(n, np.nan)
    ok = np.all(np.isfinite(X), axis=1) & np.isfinite(y)
    for a in range(int(n * start_frac), n, step):
        tr = np.arange(a)
        tr = tr[ok[tr] & (tr + label_days < a)]
        te = np.arange(a, min(a + step, n))
        te = te[np.all(np.isfinite(X[te]), axis=1)]
        if len(tr) < 60 or not len(te):
            continue
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd[sd == 0] = 1.0
        m = RidgeCV(alphas=ALPHAS).fit((X[tr] - mu) / sd, y[tr])
        pred[te] = m.predict((X[te] - mu) / sd)
    return pred


def ic(p, y):
    ok = np.isfinite(p) & np.isfinite(y)
    return float(pd.Series(p[ok]).rank().corr(pd.Series(y[ok]).rank())), int(ok.sum())


def partial(p, y, base):
    ok = np.isfinite(p) & np.isfinite(y) & np.isfinite(base)
    ra, rb = _rank_resid(p[ok], base[ok]), _rank_resid(y[ok], base[ok])
    return float(np.corrcoef(ra, rb)[0, 1]), int(ok.sum())


def main():
    rv = daily_rv()
    iv = pd.read_csv(os.path.join(_ROOT, "data", "crude_iv_daily.csv"), parse_dates=["date"]).set_index("date")
    d = pd.DataFrame({"rv": rv})
    d["lrv"] = np.log(d["rv"])
    d["har_d"] = d["lrv"]
    d["har_w"] = np.log(d["rv"].rolling(5).mean())
    d["har_m"] = np.log(d["rv"].rolling(22).mean())
    d["iv"] = np.log(iv["iv"].reindex(d.index) ** 2 / 252)
    of = orderflow_daily()
    of_cols = []
    if of is not None:
        d = d.join(of, how="left")
        of_cols = list(of.columns)
    rows = []
    print(f"Crude volatility vs implied: {d['rv'].notna().sum()} sessions"
          f"{'' if of_cols else ' (order-flow step waits for the full depth extraction)'}\n")
    for h in HORIZONS:
        fut = d["rv"][::-1].rolling(h).mean()[::-1].shift(-1)
        y = np.log(fut).to_numpy()
        sets = {"HAR": ["har_d", "har_w", "har_m"], "HAR + implied": ["har_d", "har_w", "har_m", "iv"]}
        if of_cols:
            sets["HAR + implied + order flow"] = sets["HAR + implied"] + of_cols
        sets["implied alone"] = ["iv"]
        preds = {k: walk_forward(d[c].to_numpy(float), y, h) for k, c in sets.items()}
        test = np.isfinite(preds["HAR"])
        print(f"next {h} session{'s' if h > 1 else ''}:")
        prev = None
        for k, p in preds.items():
            p = np.where(test, p, np.nan)
            r, n = ic(p, y)
            row = {"h": h, "model": k, "ic": r, "days": n}
            line = f"  {k:<28} IC {r:+.3f}"
            if prev is not None and k != "implied alone":
                b, _ = partial(p, y, np.where(test, preds[prev], np.nan))
                row["beyond_previous"] = b
                line += f"   beyond {prev}: {b:+.3f} (s.e. ~{1 / np.sqrt(n):.3f})"
            rows.append(row)
            print(line)
            if k != "implied alone":
                prev = k
        print()
    pd.DataFrame(rows).to_csv(os.path.join(_ROOT, "data", "vol_vs_implied.csv"), index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
