"""
Performance scorecard and equity curves for the market-making backtest.

    .venv/bin/python mm_scorecard.py          # after mm_backtest.py

Reads data/mm_sessions_<SYM>.csv (net dollars per session for one lot, one
column per fill rule and policy) and data/mm_backtest.csv (fills), drops the
first WARMUP test sessions as mm_suite_check.py does, and computes per
contract, fill rule and policy:

  net_per_session   mean net profit per session, dollars
  total             summed over the test sessions
  sharpe_ann        mean / standard deviation of session profit x sqrt(252)
  sortino_ann       mean / downside deviation x sqrt(252); downside
                    deviation = sqrt(mean(min(profit, 0)^2)) over all sessions
  max_drawdown      largest fall of cumulative profit from its running peak
  profitable_share  share of sessions with positive profit
  win_loss_ratio    mean winning session / |mean losing session|
  best, worst       best and worst session
  dsr               deflated Sharpe probability, all 48 configurations as trials

No capital base is assumed, so drawdown is in dollars per lot, not percent.
Writes data/mm_scorecard.csv and data/mm_equity.csv (cumulative profit by
session date).
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, skew

from backtesting.metrics import deflated_sharpe_ratio
from mm_backtest import FILLS, POLICIES

_ROOT = os.path.dirname(os.path.abspath(__file__))
SYMBOLS = ("CL", "NG", "HG", "ZC")
WARMUP = 2


def scorecard(x, n_trials=1):
    """Metrics for a series of per-session profits."""
    x = np.asarray(x, float)
    sd = x.std(ddof=1)
    down = np.sqrt(np.mean(np.minimum(x, 0.0) ** 2))
    cum = np.cumsum(x)
    peak = np.maximum.accumulate(np.r_[0.0, cum])[1:]
    wins, losses = x[x > 0], x[x < 0]
    sr = x.mean() / sd if sd > 0 else 0.0
    return {
        "net_per_session": float(x.mean()),
        "total": float(x.sum()),
        "sharpe_ann": float(sr * np.sqrt(252)),
        "sortino_ann": float(x.mean() / down * np.sqrt(252)) if down > 0 else float("inf"),
        "max_drawdown": float((cum - peak).min()),
        "profitable_share": float((x > 0).mean()),
        "win_loss_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else float("nan"),
        "best": float(x.max()),
        "worst": float(x.min()),
        "dsr": float(deflated_sharpe_ratio(sr, n_trials, len(x), float(skew(x)), float(kurtosis(x)))),
    }


def session_dates(index):
    """Session number (days since 1970, sessions start 22:00 UTC) to date."""
    return pd.to_datetime(np.asarray(index, np.int64), unit="D").strftime("%Y-%m-%d")


def main():
    fills = pd.read_csv(os.path.join(_ROOT, "data", "mm_backtest.csv"))
    n_trials = len(SYMBOLS) * len(FILLS) * len(POLICIES)
    rows, eq = [], []
    for sym in SYMBOLS:
        s = pd.read_csv(os.path.join(_ROOT, "data", f"mm_sessions_{sym}.csv"), index_col=0).iloc[WARMUP:]
        for mode in FILLS:
            for p in POLICIES:
                f = fills[(fills.symbol == sym) & (fills.fill == mode) & (fills.policy == p)]
                rows.append({"symbol": sym, "fill": mode, "policy": p, "sessions": len(s),
                             "fills_per_session": float(f.fills_per_session.iloc[0]),
                             **scorecard(s[f"{mode}_{p}"], n_trials)})
        e = s.cumsum().round(2)
        e.insert(0, "date", session_dates(s.index))
        e.insert(0, "symbol", sym)
        eq.append(e)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(_ROOT, "data", "mm_scorecard.csv"), index=False)
    pd.concat(eq).to_csv(os.path.join(_ROOT, "data", "mm_equity.csv"), index=False)
    with pd.option_context("display.width", 200, "display.max_rows", 100):
        print(df.round({"net_per_session": 0, "total": 0, "sharpe_ann": 2, "sortino_ann": 2,
                        "max_drawdown": 0, "profitable_share": 3, "win_loss_ratio": 2, "best": 0,
                        "worst": 0, "dsr": 3, "fills_per_session": 0}).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
