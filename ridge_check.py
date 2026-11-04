"""
Is the kernel model too flexible, or are the features empty?

    DATA_SOURCE=cleaned .venv/bin/python ridge_check.py              # all eight
    DATA_SOURCE=cleaned .venv/bin/python ridge_check.py --symbols CL

The kernel pipeline fits a flexible model on ~300 training sessions and
shows no skill (decision log entries 11, 16, 17). This asks the same
questions with the least flexible model there is: ridge regression, one row
per session, on the features every contract shares — OrderFlow at the
session's last bar, VRP and Macro from the last completed day (the same lag
the pipeline uses). If ridge finds skill the kernels missed, the kernel
model is overfitting; if ridge finds none either, the features are empty for
these targets.

Two targets, scored as before:
    day     next session's log return; daily rank IC (scoring.daily_ic)
    relvol  log(forward 20-day vol / trailing 20-day vol); rank IC with the
            next 5 days' realised vol after trailing 20-day vol is removed
            from both (scoring.daily_vol_ic "beyond trailing")

Walk-forward: expanding window, first test session at 40% of the sample,
refit every 20 sessions; a training row is dropped when its label is not
known by the first session of the block it predicts (purge, entry 15).
Features are standardised on the training rows only. The null retrains on
the target shuffled across sessions, many times, and is scored against the
real outcomes, so the real score is compared with a distribution, not one
draw. Per-contract feature sets are cached in data/ridge_check_cache/.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
from sklearn.linear_model import RidgeCV

from scoring import daily_ic, daily_vol_ic

_ROOT = os.path.dirname(os.path.abspath(__file__))
ALL_SYMBOLS = ("CL", "NG", "HG", "GC", "ZC", "ZT", "ES", "BTC")
CACHE = os.path.join(_ROOT, "data", "ridge_check_cache")
ALPHAS = np.logspace(-2, 4, 13)
VOL_WINDOW = 20


# ── Dataset: one row per session ─────────────────────────────────────────────

def session_rows(fast, bar_day, slow, n_days, lag=1):
    """
    X[d] = fast features at the last bar of session d, then slow features of
    day d - lag. Sessions with no bar, or before the lag, are NaN rows.
    """
    fast, slow = np.asarray(fast, float), np.asarray(slow, float)
    X = np.full((n_days, fast.shape[1] + slow.shape[1]), np.nan)
    last = {}
    for i, d in enumerate(np.asarray(bar_day)):
        last[int(d)] = i                          # bar_day is in time order
    for d, i in last.items():
        if lag <= d < n_days and d - lag < len(slow):
            X[d] = np.concatenate([fast[i], slow[d - lag]])
    return X


def targets(close):
    """Next-day log return and relative forward vol, per session; NaN where undefined."""
    lr = np.diff(np.log(np.asarray(close, float)))     # lr[k]: close k → k+1
    n = len(close)
    day = np.full(n, np.nan)
    day[:-1] = lr
    rel = np.full(n, np.nan)
    for d in range(VOL_WINDOW, n - 5):
        fwd = lr[d:min(d + VOL_WINDOW, len(lr))]
        rel[d] = np.log(fwd.std() + 1e-12) - np.log(lr[d - VOL_WINDOW:d].std() + 1e-12)
    return {"day": day, "relvol": rel}


LABEL_DAYS = {"day": 1, "relvol": VOL_WINDOW}


# ── Walk-forward ridge ───────────────────────────────────────────────────────

def blocks(n, start_frac=0.4, step=20):
    """Test blocks [a, b) covering sessions from start_frac·n to the end."""
    a = int(n * start_frac)
    return [(s, min(s + step, n)) for s in range(a, n, step)]


def walk_forward(X, y, label_days, start_frac=0.4, step=20):
    """Out-of-sample ridge forecasts; NaN where no forecast was made."""
    n = len(y)
    pred = np.full(n, np.nan)
    ok = np.all(np.isfinite(X), axis=1)
    for a, b in blocks(n, start_frac, step):
        tr = np.arange(a)
        tr = tr[ok[tr] & np.isfinite(y[tr]) & (tr + label_days < a)]   # purge
        te = np.arange(a, b)
        te = te[ok[te]]
        if len(tr) < 30 or not len(te):
            continue
        mu, sd = X[tr].mean(0), X[tr].std(0)
        sd[sd == 0] = 1.0
        m = RidgeCV(alphas=ALPHAS).fit((X[tr] - mu) / sd, y[tr])
        pred[te] = m.predict((X[te] - mu) / sd)
    return pred


def score(pred, close, target):
    """Daily IC for 'day', 'beyond trailing' next-5-day vol for 'relvol'."""
    idx = np.flatnonzero(np.isfinite(pred))
    if len(idx) < 3:
        return float("nan"), 0
    if target == "day":
        r = daily_ic(pred[idx], idx, np.arange(len(close)), close)
        return r["ic"], r["n_days"]
    r = daily_vol_ic(pred[idx], idx, np.arange(len(close)), close,
                     trailing=VOL_WINDOW, ahead=5)
    return r["partial_rv5"], r["n_days"]


def evaluate(X, close, target, n_null=50, seed=0):
    """Real score, the shuffled-target null scores, and days scored."""
    y = targets(close)[target]
    real, n = score(walk_forward(X, y, LABEL_DAYS[target]), close, target)
    rng = np.random.default_rng(seed)
    fin = np.flatnonzero(np.isfinite(y))
    null = []
    for _ in range(n_null):
        ys = y.copy()
        ys[fin] = y[fin][rng.permutation(len(fin))]
        null.append(score(walk_forward(X, ys, LABEL_DAYS[target]), close, target)[0])
    return real, np.array(null), n


# ── Features from the pipeline ───────────────────────────────────────────────

def load_symbol(symbol):
    """(X, close) for one contract, from the pipeline's feature builder; cached."""
    path = os.path.join(CACHE, f"{symbol}_{os.environ.get('DATA_SOURCE', 'legacy')}.npz")
    if os.path.exists(path):
        d = np.load(path)
        return d["X"], d["close"]
    import pandas as pd
    import run_backtest as rb
    from backtesting.walk_forward import MultiResolutionAligner
    from feature_alignment import to_epoch_seconds

    F = rb.build_features_for_symbol(symbol)
    n_slow = F.n_slow_axis
    days = pd.DatetimeIndex(F.daily.index[:n_slow])
    if rb.DATA_SOURCE == "cleaned":                  # session starts (entry 9)
        prev = days.shift(-1, freq="D")[:1].append(days[:-1])
        day_ts = to_epoch_seconds((prev + pd.Timedelta(hours=17)).tz_localize("America/Chicago"))
    else:
        day_ts = to_epoch_seconds(days)
    al = MultiResolutionAligner(F.master_ts, day_ts, feature_lag=rb.SLOW_FEATURE_LAG)
    bar_day = np.clip(al.get_daily_indices(np.arange(F.n_master)), 0, n_slow - 1)
    slow = np.hstack([F.vrp_feats, F.macro_feats])
    X = session_rows(F.orderflow_feats, bar_day, slow, n_slow, lag=rb.SLOW_FEATURE_LAG)
    close = F.daily["close"].to_numpy(float)[:n_slow]
    os.makedirs(CACHE, exist_ok=True)
    np.savez(path, X=X, close=close)
    return X, close


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="all")
    ap.add_argument("--nulls", type=int, default=50)
    a = ap.parse_args(argv)
    symbols = ALL_SYMBOLS if a.symbols == "all" else tuple(
        s.strip().upper() for s in a.symbols.split(","))
    # The pipeline's feature builder reads its configuration from the
    # environment at import: use the validate_pipeline.py configuration.
    from validate_pipeline import BASE_ENV
    for k, v in BASE_ENV.items():
        os.environ.setdefault(k, v)

    data = {s: load_symbol(s) for s in symbols}
    print(f"\nRidge check — data={os.environ.get('DATA_SOURCE', 'legacy')}, "
          f"{a.nulls} shuffled-target nulls per contract\n")
    for target, label in (("day", "next-day return: daily rank IC"),
                          ("relvol", "relative vol: beyond-trailing IC, next-5-day vol")):
        print("=" * 72)
        print(f"  {label}")
        print("=" * 72)
        print(f"  {'':<5}{'ridge':>9}{'null mean':>11}{'null sd':>9}{'pct':>7}{'days':>6}")
        reals, nulls, ns = [], [], []
        for s in symbols:
            X, close = data[s]
            r, nu, n = evaluate(X, close, target, a.nulls)
            nu = nu[np.isfinite(nu)]
            pct = float((nu < r).mean()) if len(nu) and np.isfinite(r) else float("nan")
            print(f"  {s:<5}{r:>+9.3f}{nu.mean():>+11.3f}{nu.std():>9.3f}{pct:>7.0%}{n:>6}")
            if np.isfinite(r) and len(nu) == a.nulls:
                reals.append(r); nulls.append(nu); ns.append(n)
        w = np.array(ns, float)
        pr = float(np.average(reals, weights=w))
        pn = np.average(np.vstack(nulls), axis=0, weights=w)       # pooled, per null draw
        z = (pr - pn.mean()) / pn.std()
        print(f"  {'pool':<5}{pr:>+9.3f}{pn.mean():>+11.3f}{pn.std():>9.3f}"
              f"{(pn < pr).mean():>7.0%}{int(w.sum()):>6}   z = {z:+.2f}\n")
    print("  'pct': share of shuffled-target nulls the real ridge beats. Skill needs")
    print("  the pooled z at or above +2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
