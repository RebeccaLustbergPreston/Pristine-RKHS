"""
What is speed worth to a market maker? The market-making backtest
(mm_backtest.py) with our view of the book delayed.

    .venv/bin/python mm_latency.py                       # CL, NG, HG, ZC
    .venv/bin/python mm_latency.py --symbols CL

mm_backtest.py reacts to the book once a second: a quote set at the end of a
second is live from the next one, so the reaction time is about 1 s. Here our
view of the book (and the policy's flags) lags by DELAYS more seconds, while
fills happen against the real book, so reaction time = 1 + delay seconds. A
stale quote is picked off or crosses the real market (see simulate's
docstring). Pessimistic "through" fill rule only; the queue at a stale price
is unknown. Policies: quote all the time, and stand aside when the
volatility forecast is loud ("vol").

We cannot go below 1 s with one snapshot a second. The question is the slope:
if loss per fill grows steeply with reaction time from 1 to 31 s, speed is
what the loss is made of, and the curve shows which way a faster firm moves.

Also writes the fee arithmetic from data/mm_backtest.csv: the fee per side at
which each policy would break even (negative = it would need a rebate).

Writes data/mm_latency.csv and data/mm_breakeven.csv.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

import mm_backtest as mm

_ROOT = os.path.dirname(os.path.abspath(__file__))
DELAYS = (0, 1, 2, 5, 10, 30)
POLICIES = ("always", "vol")


def sweep(symbol, delays=DELAYS, policies=POLICIES, seed=0):
    d = mm.prepare(symbol)
    usd = mm.TICK_USD[symbol]
    n_sess = len(d["test_sessions"])
    rows = []
    for p in policies:
        ab, aa = mm.policy_flags(p, d["sec"], d["minutes"], d["fr"], d["fk"], d["book"],
                                 d["eligible"], np.random.default_rng(seed))
        for lag in delays:
            f = mm.simulate(d["bid"], d["ask"], d["bsz"], d["asz"], d["seg"], ab, aa, "through",
                            delay=lag)
            assert np.isin(d["session"][f[0]], d["test_sessions"]).all()
            gross = mm.session_pnl(f, d["session"], 0.0).reindex(d["test_sessions"], fill_value=0.0)
            net = mm.session_pnl(f, d["session"], d["fee"]).reindex(d["test_sessions"], fill_value=0.0)
            n_f = len(f[0])
            rows.append({"symbol": symbol, "policy": p, "delay_s": lag, "reaction_s": 1 + lag,
                         "fills_per_session": n_f / n_sess,
                         "gross_ticks_per_fill": float(gross.sum() / max(n_f, 1)),
                         "markout60_ticks": mm.markout(f, d["mid"], d["seg"]),
                         "net_usd_per_session": float(net.mean() * usd)})
            r = rows[-1]
            print(f"  {symbol} {p:<6} reaction {1 + lag:>2}s  fills/sess {r['fills_per_session']:7.0f}"
                  f"  gross/fill {r['gross_ticks_per_fill']:+.3f}t  markout60 {r['markout60_ticks']:+.2f}t"
                  f"  net ${r['net_usd_per_session']:+9.0f}/sess", flush=True)
    return rows


def breakeven(path=os.path.join(_ROOT, "data", "mm_backtest.csv")):
    """Fee per side, in dollars, at which each policy's profit is zero."""
    d = pd.read_csv(path)
    d = d[d.policy.isin(mm.POLICIES)].copy()
    d["breakeven_fee_usd"] = d.gross_usd_per_session / d.fills_per_session
    d["fee_now_usd"] = mm.FEE_USD
    return d[["symbol", "fill", "policy", "fills_per_session", "gross_usd_per_session",
              "breakeven_fee_usd", "fee_now_usd"]]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="CL,NG,HG,ZC")
    a = ap.parse_args(argv)
    be = breakeven()
    be.to_csv(os.path.join(_ROOT, "data", "mm_breakeven.csv"), index=False)
    print("Break-even fee per side (dollars; negative = needs a rebate):")
    print(be.round(3).to_string(index=False), flush=True)
    rows = []
    for s in a.symbols.split(","):
        rows += sweep(s.strip().upper())
    pd.DataFrame(rows).to_csv(os.path.join(_ROOT, "data", "mm_latency.csv"), index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
