"""
Scores that read predictions directly, not trading profit.

``daily_ic`` answers one question for any run, whatever its training target:
how well does the prediction made at the end of each session forecast the
next session's close-to-close return — the return the end-of-day position
actually earns. Because every run is scored on that same outcome, a model
trained on next-bar returns and one trained on next-day returns can be
compared directly (``compare_targets.py``).

The score is a rank (Spearman) correlation, so a handful of large days
cannot carry it, reported overall and per walk-forward fold. With n scored
days its standard error under no skill is about 1/sqrt(n).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def daily_ic(pred, bar_idx, bar_day, day_close, fold_of_bar=None) -> dict:
    """
    pred        : (n_oos,) out-of-sample predictions, one per scored bar
    bar_idx     : (n_oos,) the bar each prediction belongs to, in time order
    bar_day     : (n_bars,) session index of every bar
    day_close   : (n_days,) session close prices (switch-adjusted if used)
    fold_of_bar : (n_oos,) walk-forward fold of each prediction, optional

    For each session with a scored bar, the decision-time prediction is the
    one at its last scored bar; the outcome is log close[d+1] − log close[d].
    The final session has no outcome and is dropped.
    """
    pred = np.asarray(pred, dtype=float)
    bar_idx = np.asarray(bar_idx)
    if len(pred) != len(bar_idx):
        raise ValueError(f"{len(pred)} predictions for {len(bar_idx)} bars")
    if np.any(np.diff(bar_idx) < 0):
        raise ValueError("bar_idx must be in time order")
    days = np.asarray(bar_day)[bar_idx]
    close = np.asarray(day_close, dtype=float)
    df = pd.DataFrame({"day": days, "pred": pred,
                       "fold": -1 if fold_of_bar is None else np.asarray(fold_of_bar)})
    last = df.groupby("day", sort=True).tail(1)               # end-of-session prediction
    last = last[last["day"] + 1 < len(close)]
    out = np.log(close[last["day"].to_numpy() + 1]) - np.log(close[last["day"].to_numpy()])
    last = last.assign(outcome=out)

    def ic(x: pd.DataFrame) -> float:
        if len(x) < 3 or x["pred"].nunique() < 2 or x["outcome"].nunique() < 2:
            return float("nan")
        return float(x["pred"].rank().corr(x["outcome"].rank()))

    per_fold = ([ic(g) for _, g in last.groupby("fold", sort=True)]
                if fold_of_bar is not None else [])
    n = len(last)
    return {"ic": ic(last), "ic_folds": np.array(per_fold, dtype=float), "n_days": n,
            "se_no_skill": 1.0 / np.sqrt(max(n - 1, 1))}


def fold_of(oos_indices, fold_index_lists) -> np.ndarray:
    """Fold number of each out-of-sample bar, from the per-fold test indices."""
    lookup = {}
    for f, idx in enumerate(fold_index_lists):
        for i in np.asarray(idx).tolist():
            lookup[i] = f
    return np.array([lookup.get(i, -1) for i in np.asarray(oos_indices).tolist()])


# ── Volatility forecasts ─────────────────────────────────────────────────────

def _rank_resid(y: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Ranks of y with the linear effect of the ranks of x removed."""
    ry = pd.Series(y).rank().to_numpy()
    rx = pd.Series(x).rank().to_numpy()
    rx = rx - rx.mean()
    beta = (rx @ (ry - ry.mean())) / (rx @ rx) if rx @ rx > 0 else 0.0
    return ry - ry.mean() - beta * rx


def daily_vol_ic(pred, bar_idx, bar_day, day_close, trailing: int = 20,
                 ahead: int = 5) -> dict:
    """
    How well an end-of-session volatility forecast predicts the volatility
    that follows — and how much of that trailing volatility already explains.

    For each session d with a scored bar, the forecast is the one at its last
    scored bar. Outcomes, both strictly after the decision:
        abs1  |log close[d+1] − log close[d]|
        rv5   std of the next ``ahead`` daily log returns (d → d+ahead)
    Baseline: std of the ``trailing`` daily returns up to close[d], known at
    the decision. Volatility is persistent, so the baseline alone forecasts
    well; ``partial_*`` is the rank correlation of forecast and outcome after
    removing the baseline from both — the forecast's information beyond
    trailing volatility. Non-overlapping outcomes are deliberately short:
    overlapping 20-day windows make every score look more certain than it is.
    """
    pred = np.asarray(pred, dtype=float)
    bar_idx = np.asarray(bar_idx)
    if np.any(np.diff(bar_idx) < 0):
        raise ValueError("bar_idx must be in time order")
    close = np.asarray(day_close, dtype=float)
    lr = np.diff(np.log(close))                      # lr[k]: close k → k+1
    df = pd.DataFrame({"day": np.asarray(bar_day)[bar_idx], "pred": pred})
    last = df.groupby("day", sort=True).tail(1)
    d = last["day"].to_numpy()
    ok = (d >= trailing) & (d + ahead < len(close))
    d, p = d[ok], last["pred"].to_numpy()[ok]
    abs1 = np.abs(lr[d])
    rv5 = np.array([lr[k:k + ahead].std() for k in d])
    base = np.array([lr[k - trailing:k].std() for k in d])

    def ic(a, b):
        if len(a) < 3 or np.unique(a).size < 2 or np.unique(b).size < 2:
            return float("nan")
        return float(pd.Series(a).rank().corr(pd.Series(b).rank()))

    def partial(a, b):
        if len(a) < 3 or np.unique(a).size < 2:
            return float("nan")
        ra, rb = _rank_resid(a, base), _rank_resid(b, base)
        if ra.std() < 1e-9 * len(a):      # the forecast *is* the baseline:
            return 0.0                     # nothing beyond trailing volatility
        if rb.std() == 0:
            return float("nan")
        return float(np.corrcoef(ra, rb)[0, 1])

    return {"ic_abs1": ic(p, abs1), "ic_rv5": ic(p, rv5),
            "partial_abs1": partial(p, abs1), "partial_rv5": partial(p, rv5),
            "baseline_ic_abs1": ic(base, abs1), "baseline_ic_rv5": ic(base, rv5),
            "n_days": int(len(d))}


# ── Cross-sectional forecasts (F1) ───────────────────────────────────────────

def cross_sectional_ic(forecast: pd.DataFrame, outcome: pd.DataFrame,
                       min_names: int = 5) -> dict:
    """
    How well a forecast ranks contracts against each other.

    forecast, outcome : sessions × contracts, the forecast known at the
                        session's close and the outcome that follows it.
    Per session, the Spearman correlation across the contracts that have
    both (at least ``min_names``); then the mean over sessions, with a
    t-stat from the spread of the per-session values. Shuffling which
    contract each forecast belongs to has mean zero, so t measures distance
    from that null.
    """
    f, y = forecast.align(outcome, join="inner")
    ok = f.notna() & y.notna()
    fr = f.where(ok).rank(axis=1)
    yr = y.where(ok).rank(axis=1)
    keep = ok.sum(axis=1) >= min_names
    fr, yr = fr[keep], yr[keep]
    fc = fr.sub(fr.mean(axis=1), axis=0)
    yc = yr.sub(yr.mean(axis=1), axis=0)
    den = np.sqrt((fc ** 2).sum(axis=1) * (yc ** 2).sum(axis=1))
    ic = ((fc * yc).sum(axis=1) / den.where(den > 0)).dropna()
    n = len(ic)
    if n < 3:
        return {"ic": float("nan"), "t": float("nan"), "n": n, "daily": ic}
    return {"ic": float(ic.mean()), "t": float(ic.mean() / ic.std(ddof=1) * np.sqrt(n)),
            "n": n, "daily": ic}
