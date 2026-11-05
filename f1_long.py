"""
F1 on 14 years of daily settlements, June 2010 – December 2024.

    .venv/bin/python f1_long.py

Same forecasts, outcome, score and stopping rule as f1.py (decision log
entry 20), on the settlement table from daily_settlements.py instead of 18
months of ticks. Two measurements improve:

* Returns are roll-free. Each date's return is the settlement of that
  date's front contract over the *same contract's* settlement the trading
  date before — available from the second-contract file when the contract
  had not yet become the front. No switch gap is ever in a return, and no
  overnight move is ever removed with one (the D0 caveat in entry 20).
* Carry is measured, not inferred from rolls: log(front settlement ÷
  second settlement) on the same date, annualised by the contract's
  listing cycle (months between consecutive front contracts, CYCLE_MONTHS).
  Positive when the curve is backwardated — the roll pays a long holder.

Each forecast is scored on the whole sample and on each half
(2010–2017, 2018–2024): an effect that is real should keep its sign.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from f1 import ALL_SYMBOLS, long_short, outcome, signals
from scoring import cross_sectional_ic

_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(_ROOT, "data", "daily_history")
# Months between consecutive front contracts (CME listing cycles as rolled
# here): monthly energy and bitcoin, the active-month cycles for metals and
# corn, quarterly for equities and Treasuries.
CYCLE_MONTHS = {"CL": 1, "NG": 1, "HG": 2, "GC": 2, "ZC": 2.4, "ZT": 3, "ES": 3, "BTC": 1}
SPLIT = pd.Timestamp("2018-01-01")


def _one_per_date(r: pd.DataFrame) -> pd.DataFrame:
    """
    One row per date for one rank. On a switch date the file holds both the
    outgoing and the incoming contract (a settlement published after the
    switch is filed under the new mapping); keep the one that continues —
    the contract this rank holds on the next date. Either gives a correct
    same-contract return; this way is consistent.
    """
    r = r.sort_values("date")
    dates = np.sort(r["date"].unique())
    nxt = {d: n for d, n in zip(dates[:-1], dates[1:])}
    on = set(zip(r["date"], r["instrument_id"]))
    dup = r["date"].duplicated(keep=False).to_numpy()
    keep = np.ones(len(r), bool)
    for i, (d, inst) in enumerate(zip(r["date"], r["instrument_id"])):
        if dup[i] and d in nxt and (nxt[d], inst) not in on:
            keep[i] = False
    return r[keep].drop_duplicates("date", keep="last").set_index("date")


def roll_free(t: pd.DataFrame) -> pd.DataFrame:
    """
    From a settlement table (date, rank, instrument_id, settle) for one
    contract: per front date, the front's log return on the same contract
    and the log front-over-second settlement ratio.
    """
    t = t.sort_values(["date", "rank"])
    px = t.drop_duplicates(["instrument_id", "date"]).set_index(["instrument_id", "date"])["settle"]
    front = _one_per_date(t[t["rank"] == 0])
    second = _one_per_date(t[t["rank"] == 1])
    dates = front.index.to_numpy()
    inst = front["instrument_id"].to_numpy()
    ret = np.full(len(dates), np.nan)
    for k in range(1, len(dates)):
        prev = px.get((inst[k], dates[k - 1]))
        if prev is not None and prev > 0 and front["settle"].iat[k] > 0:
            ret[k] = np.log(front["settle"].iat[k] / prev)
    sec = second.reindex(front.index)
    diff_inst = sec["instrument_id"].to_numpy() != inst
    ratio = np.where(diff_inst & (sec["settle"].to_numpy() > 0),
                     np.log(front["settle"].to_numpy() / sec["settle"].to_numpy()), np.nan)
    return pd.DataFrame({"ret": ret, "front_over_second": ratio}, index=front.index)


def load_panel(symbols=ALL_SYMBOLS):
    """Roll-free total-return index and annualised carry, dates × contracts."""
    rets, carry = {}, {}
    for s in symbols:
        t = pd.read_parquet(os.path.join(SRC, f"settlements_{s}.parquet"))
        rf = roll_free(t)
        rets[s] = rf["ret"]
        carry[s] = rf["front_over_second"] * 12.0 / CYCLE_MONTHS[s]
    ret = pd.DataFrame(rets).sort_index()
    # A total-return index so f1.signals' price-based momentum applies
    # unchanged; a missing return leaves the level where it was.
    close = np.exp(ret.fillna(0.0).cumsum()).where(ret.notna() | ret.shift(1).notna())
    close = close.ffill()
    return close, pd.DataFrame(carry).sort_index(), ret


def main(argv=None):
    close, carry, ret = load_panel()
    y = outcome(close)
    y = y.where(ret.shift(-1).notna())               # no outcome across a missing settlement
    sig = signals(close, carry=carry)
    print(f"F1 on daily settlements — {close.shape[1]} contracts, "
          f"{close.index[0]:%Y-%m-%d} → {close.index[-1]:%Y-%m-%d}, "
          f"{int((ret.notna().sum(axis=1) >= 5).sum()):,} dates with ≥5 contracts")
    print(f"  roll-free returns per contract: "
          + ", ".join(f"{c} {int(ret[c].notna().sum()):,}" for c in ret.columns) + "\n")
    head = f"  {'forecast':<18}{'IC':>8}{'t':>7}{'n':>7}{'L/S SR':>8}" \
           f"{'IC 10–17':>10}{'t':>6}{'IC 18–24':>10}{'t':>6}"
    print(head)
    rows = []
    for name, f in sig.items():
        r = cross_sectional_ic(f, y, min_names=5)
        a = cross_sectional_ic(f[f.index < SPLIT], y[y.index < SPLIT], min_names=5)
        b = cross_sectional_ic(f[f.index >= SPLIT], y[y.index >= SPLIT], min_names=5)
        ls = long_short(f, close)
        sh = ls.mean() / ls.std() * np.sqrt(252) if len(ls) > 20 else np.nan
        rows.append({"forecast": name, "ic": r["ic"], "t": r["t"], "n": r["n"], "ls_sharpe": sh,
                     "ic_2010_17": a["ic"], "t_2010_17": a["t"],
                     "ic_2018_24": b["ic"], "t_2018_24": b["t"]})
        print(f"  {name:<18}{r['ic']:>+8.3f}{r['t']:>+7.2f}{r['n']:>7}{sh:>+8.2f}"
              f"{a['ic']:>+10.3f}{a['t']:>+6.1f}{b['ic']:>+10.3f}{b['t']:>+6.1f}")
    out = os.path.join(_ROOT, "data", "f1_long.csv")
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\n  Rule (fixed in advance): counts only at t ≥ 2; with ten forecasts, treat"
          f"\n  t below ~2.8 with caution, and look for the same sign in both halves."
          f"\n  L/S SR: rank-weighted long–short, vol-scaled legs, gross of costs. Table: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
