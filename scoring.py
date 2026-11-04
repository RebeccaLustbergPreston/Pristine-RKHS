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
