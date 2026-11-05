"""
F1 — which of the eight contracts does better tomorrow? Rule baselines.

    DATA_SOURCE=cleaned .venv/bin/python f1.py

FALL_2026_TASKS.md section F, "F1 plan". Each session d, every contract gets
a forecast known at d's close; the outcome is its next-session return
divided by its trailing 20-session volatility (so natural gas and bitcoin
do not take the extreme ranks every day just by being volatile). The score
is the Spearman correlation across the contracts, per session
(scoring.cross_sectional_ic), averaged, with a t-stat across sessions.

Forecasts (all causal, all vol-scaled like the outcome):
    mom21, mom63, mom126   log return over the last 21 / 63 / 126 sessions
    rev1                   minus the last session's return (one-day reversal)
    carry                  roll yield from the last contract switch: minus the
                           log gap (new contract over old), annualised by the
                           spacing of the two previous switches; known from the
                           switch session onward
    static_*               the rule averaged over all its past sessions — is it
                           timing, or a fixed tilt toward some contracts?

Stopping rule, fixed before running (section F): a forecast counts only if
its mean cross-sectional IC is at least 2 s.e. above zero. Within a session,
shuffling which contract each forecast belongs to has mean exactly zero, so
zero is the shuffled null; the s.e. is the spread of daily ICs over sessions.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from scoring import cross_sectional_ic

_ROOT = os.path.dirname(os.path.abspath(__file__))
ALL_SYMBOLS = ("CL", "NG", "HG", "GC", "ZC", "ZT", "ES", "BTC")
CACHE = os.path.join(_ROOT, "data", "f1_panel.npz")
VOL_WINDOW = 20


def load_panel(symbols=ALL_SYMBOLS):
    """
    Session closes (switch-adjusted), and per-contract switch tables, on the
    sessions all contracts share. Cached.
    """
    if os.path.exists(CACHE):
        z = np.load(CACHE, allow_pickle=True)
        close = pd.DataFrame(z["close"], index=pd.to_datetime(z["dates"]), columns=z["symbols"])
        return close, z["switches"].item()
    from data_loader import DataLoader
    closes, switches = {}, {}
    for s in symbols:
        dl = DataLoader(os.path.join(_ROOT, "data"), s, source="cleaned", switch_gaps="remove")
        closes[s] = dl.build_daily_ohlcv()["close"]
        sw = dl.contract_switches()
        switches[s] = pd.DataFrame({
            "session_date": sw["session_date"].to_numpy(),
            "log_gap": np.log(sw["first_price_after"].to_numpy(float)
                              / sw["last_price_before"].to_numpy(float)),
            "at_open": (sw["ts_event"].dt.hour.isin([22, 23])
                        & (sw["ts_event"].dt.minute < 5)).to_numpy(),
        })
    close = pd.DataFrame(closes).dropna()
    np.savez(CACHE, close=close.to_numpy(), dates=close.index.to_numpy(),
             symbols=np.array(close.columns), switches=np.array(switches, dtype=object))
    return close, switches


def carry_signal(dates, sw):
    """
    Annualised roll yield per session from the switch table: −log_gap ÷ the
    years between the two previous switches, held from each switch onward.
    NaN before the second switch.
    """
    sd = pd.to_datetime(sw["session_date"]).to_numpy()
    gaps = np.asarray(sw["log_gap"], float)
    out = pd.Series(np.nan, index=dates)
    for k in range(1, len(sd)):
        years = (sd[k] - sd[k - 1]) / np.timedelta64(365, "D")
        if years <= 0:
            continue
        out[out.index >= sd[k]] = -gaps[k] / years
    return out


def signals(close, switches=None, carry=None):
    """
    Every forecast, a dict of DataFrames (sessions × contracts), causal.
    Carry comes from the switch gaps unless a carry table is given
    (f1_long.py measures it from front and second settlements).
    """
    lr = np.log(close).diff()
    vol = lr.rolling(VOL_WINDOW).std()               # up to and including d
    sig = {}
    for n in (21, 63, 126):
        sig[f"mom{n}"] = (np.log(close) - np.log(close.shift(n))) / (vol * np.sqrt(n))
    sig["rev1"] = -lr / vol
    if carry is None:
        carry = pd.DataFrame({s: carry_signal(close.index, switches[s])
                              for s in close.columns})
    sig["carry"] = carry.reindex(index=close.index, columns=close.columns)
    for k in list(sig):
        sig[f"static_{k}"] = sig[k].expanding().mean()
    return sig


def outcome(close):
    """Next-session log return over trailing vol known at d's close."""
    lr = np.log(close).diff()
    vol = lr.rolling(VOL_WINDOW).std()
    return lr.shift(-1) / vol


def long_short(forecast, close):
    """
    Daily return of a rank-weighted long–short book: weights are demeaned
    cross-sectional ranks scaled to unit gross, each leg vol-scaled, held one
    session. Gross of costs; a description of the IC, not a strategy.
    """
    lr = np.log(close).diff()
    vol = lr.rolling(VOL_WINDOW).std()
    r = forecast.rank(axis=1)
    w = r.sub(r.mean(axis=1), axis=0)
    w = w.div(w.abs().sum(axis=1), axis=0) / vol
    w = w.div((w.abs()).sum(axis=1), axis=0)
    pnl = (w * lr.shift(-1)).sum(axis=1, min_count=5)
    return pnl.dropna()


def main(argv=None):
    close, switches = load_panel()
    y = outcome(close)
    print(f"F1 rule baselines — {close.shape[1]} contracts, {len(close)} shared sessions "
          f"({close.index[0]:%Y-%m-%d} → {close.index[-1]:%Y-%m-%d})")
    n_sw = sum(len(v) for v in switches.values())
    n_open = sum(int(v["at_open"].sum()) for v in switches.values())
    print(f"Contract switches: {n_sw}, of which {n_open} at a session's first trade "
          "(their gap includes the overnight move — carry is noisy there)\n")
    print(f"  {'forecast':<18}{'mean IC':>9}{'t':>7}{'sessions':>10}{'L/S Sharpe':>12}")
    rows = []
    for name, f in signals(close, switches).items():
        r = cross_sectional_ic(f, y, min_names=5)
        ls = long_short(f, close)
        sh = ls.mean() / ls.std() * np.sqrt(252) if len(ls) > 20 else np.nan
        rows.append({"forecast": name, **{k: r[k] for k in ("ic", "t", "n")}, "ls_sharpe": sh})
        print(f"  {name:<18}{r['ic']:>+9.3f}{r['t']:>+7.2f}{r['n']:>10}{sh:>+12.2f}")
    out = os.path.join(_ROOT, "data", "f1_baselines.csv")
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\n  Rule: counts only if t ≥ 2. Per-session IC over 8 contracts has s.e. ≈ 0.38,"
          f"\n  so ~{len(close)} sessions resolve a mean IC of about ±0.04. Table: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
