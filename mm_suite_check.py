"""
Put the market-making backtest's profit streams through the backtesting
suite's statistical layers.

    .venv/bin/python mm_suite_check.py          # after mm_backtest.py

Reads data/mm_sessions_<SYM>.csv (net dollars per session per lot, one
column per fill rule and policy). Drops the first WARMUP sessions of the test
period, where the volatility gate has too few past forecasts for a threshold
and so does not quote at all (which would flatter it against "always").

  1. Stationary block bootstrap (Politis & Romano 1994, mean block
     BLOCK sessions, analytics/bootstrap_mc.py) of the per-session
     difference between two policies: a 95% interval that allows for
     good and bad days clustering in runs, as a check on the paired t.
  2. Deflated Sharpe ratio (backtesting/metrics.py) for every policy's own
     profit stream, with every configuration tried counted as a trial.
  3. The rule from mm_backtest.py: a policy goes on to the risk layers
     (Monte Carlo drawdowns, crisis scenarios) only if its own net profit is
     positive with t >= 2 under the pessimistic "through" fill rule.

Writes data/mm_suite_check.csv.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, skew

from analytics.bootstrap_mc import _stationary_resample
from backtesting.metrics import deflated_sharpe_ratio
from mm_backtest import FILLS, POLICIES

_ROOT = os.path.dirname(os.path.abspath(__file__))
SYMBOLS = ("CL", "NG", "HG", "ZC")
WARMUP = 2
BLOCK = 5
N_BOOT = 10_000
PAIRS = (("vol", "always"), ("vol", "random"), ("vol_kernel", "vol"), ("vol_book", "vol"))


def block_ci(d, seed=0):
    rng = np.random.default_rng(seed)
    d = np.asarray(d, float)
    means = np.array([_stationary_resample(rng, d, len(d), BLOCK).mean() for _ in range(N_BOOT)])
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main():
    rows = []
    n_trials = len(SYMBOLS) * len(FILLS) * len(POLICIES)
    for sym in SYMBOLS:
        path = os.path.join(_ROOT, "data", f"mm_sessions_{sym}.csv")
        if not os.path.exists(path):
            print(f"{sym}: no results yet")
            continue
        s = pd.read_csv(path, index_col=0).iloc[WARMUP:]
        print(f"\n{sym}: {len(s)} sessions after warm-up")
        for mode in FILLS:
            for a, b in PAIRS:
                d = s[f"{mode}_{a}"] - s[f"{mode}_{b}"]
                t = d.mean() / d.std(ddof=1) * np.sqrt(len(d))
                lo, hi = block_ci(d)
                rows.append({"symbol": sym, "fill": mode, "test": f"{a} - {b}", "mean_usd": d.mean(),
                             "t": t, "ci_lo": lo, "ci_hi": hi})
                print(f"  {mode:<7} {a + ' - ' + b:<18} ${d.mean():+9.1f}/sess  t {t:+5.1f}  "
                      f"95% block CI [{lo:+.1f}, {hi:+.1f}]")
            for p in POLICIES:
                x = s[f"{mode}_{p}"]
                sr = x.mean() / x.std(ddof=1) if x.std() > 0 else 0.0
                dsr = deflated_sharpe_ratio(sr, n_trials, len(x), float(skew(x)), float(kurtosis(x)))
                t = sr * np.sqrt(len(x))
                ok = mode == "through" and x.mean() > 0 and t >= 2
                rows.append({"symbol": sym, "fill": mode, "test": p, "mean_usd": x.mean(), "t": t,
                             "sharpe_ann": sr * np.sqrt(252), "dsr": dsr, "to_risk_layers": ok})
            best = max(POLICIES, key=lambda p: s[f"{mode}_{p}"].mean())
            r = [x for x in rows if x["symbol"] == sym and x["fill"] == mode and x["test"] == best][0]
            print(f"  {mode:<7} best policy {best}: ${r['mean_usd']:+.1f}/sess, annualised Sharpe "
                  f"{r['sharpe_ann']:+.2f}, deflated Sharpe probability {r['dsr']:.3f} ({n_trials} trials)")
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(_ROOT, "data", "mm_suite_check.csv"), index=False)
    go = df[df.get("to_risk_layers", False) == True] if len(df) else df  # noqa: E712
    print(f"\nPolicies that qualify for the risk layers: {len(go)}"
          + ("" if len(go) else " (none makes money after fees under the pessimistic fill rule)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
