"""
Volatility ripples on the 1-minute bars built from the per-second book.

    .venv/bin/python vol_ripples.py

Three views, per contract (CL, NG, HG, ZC; three sample months):
  clock    mean realised variance by minute of the Chicago day, relative to
           the contract's own daily mean — the intraday pattern
  echo     autocorrelation of log 1-minute realised variance at lags 1..240
           minutes — how long a burst keeps echoing — raw, and with each
           minute-of-day's mean removed so the clock does not count
  event    crude only: realised variance per minute from 30 min before to
           90 min after the weekly EIA petroleum inventory release
           (Wednesdays 9:30 Chicago; holiday weeks shift and are left in),
           relative to the same-minute average on other weekdays
Each contract's final session before the series switches to the next is
left out (expiry-day books are empty; see the comment in main). Writes
data/vol_ripples_{clock,echo,event}.csv.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

from horizon_check import TICK, load, to_grid
from minute_check import minute_bars

_ROOT = os.path.dirname(os.path.abspath(__file__))
SYMBOLS = ("CL", "NG", "HG", "ZC")


def chicago(minute):
    return pd.to_datetime(minute * 60, unit="s", utc=True).tz_convert("America/Chicago")


def main():
    clock, echo, event = [], [], []
    for s in SYMBOLS:
        g = to_grid(load(s))
        b = minute_bars(g, TICK[s])
        b["inst"] = b["minute"].map(g.groupby(g.index // 60)["inst"].last())
        t = chicago(b["minute"].to_numpy())
        b["tod"] = t.hour * 60 + t.minute
        b["dow"] = t.dayofweek
        b["date"] = t.date
        b["lrv"] = np.log(b["rv"] + 1e-3)
        # Drop each contract's last session before the series moves to the
        # next: CL's depth follows the calendar roll (CL.c.0), so it holds the
        # expiring contract through expiry day, when its book empties and the
        # spread reaches dollars. Not the market a trader would be in.
        inst = b["inst"].to_numpy()
        last = {}
        for i_, d_ in zip(inst, b["date"]):
            last[i_] = d_
        final = inst[-1]
        expiry = b["inst"].ne(final) & (b["date"] == b["inst"].map(last))
        b = b[~expiry].copy()
        print(f"{s}: dropped {int(expiry.sum())} minutes, the "
              f"{len({k for k in last if k != final})} final sessions before a switch")
        # Clock: mean variance by 30-minute slot of the Chicago day, over the overall mean.
        slot = (b["tod"] // 30) * 30
        c = b.groupby(slot)["rv"].mean() / b["rv"].mean()
        n = b.groupby(slot).size()
        for k, v in c.items():
            if n[k] >= 200:
                clock.append({"symbol": s, "slot": int(k), "rel_var": float(v)})
        # Echo: autocorrelation within segments, pooled; "deseason" first
        # removes each minute-of-day's mean, so busy hours following busy
        # hours (the clock) do not count as echo.
        b["lrv_ds"] = b["lrv"] - b.groupby("tod")["lrv"].transform("mean")
        for lag in (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240):
            row = {"symbol": s, "lag": lag}
            for col, key in (("lrv", "acf"), ("lrv_ds", "acf_deseason")):
                x, y = [], []
                for _, g in b.groupby("seg"):
                    v = g[col].to_numpy()
                    m = g["minute"].to_numpy()
                    if len(v) > lag:
                        ok = m[lag:] - m[:-lag] == lag
                        x.append(v[:-lag][ok]); y.append(v[lag:][ok])
                x, y = np.concatenate(x), np.concatenate(y)
                row[key] = float(np.corrcoef(x, y)[0, 1])
            echo.append(row)
        if s == "CL":
            rel = b["tod"] - (9 * 60 + 30)
            base = b[(b["dow"] != 2)].groupby("tod")["rv"].mean()
            w = b[(b["dow"] == 2) & (rel >= -30) & (rel <= 90)]
            m = w.groupby("tod")["rv"].mean()
            for tod, v in m.items():
                event.append({"symbol": s, "rel_min": int(tod - 570), "wed": float(v),
                              "other": float(base.get(tod, np.nan)),
                              "days": int(w[w["tod"] == tod]["date"].nunique())})
    out = {"clock": clock, "echo": echo, "event": event}
    for k, rows in out.items():
        pd.DataFrame(rows).to_csv(os.path.join(_ROOT, "data", f"vol_ripples_{k}.csv"), index=False)
    print(pd.DataFrame(echo).pivot(index="lag", columns="symbol", values=["acf", "acf_deseason"]).round(3))
    cl = pd.DataFrame(clock).pivot(index="slot", columns="symbol", values="rel_var").round(2)
    cl.index = [f"{i // 60:02d}:{i % 60:02d}" for i in cl.index]
    print(cl.to_string())
    ev = pd.DataFrame(event)
    ev["ratio"] = ev["wed"] / ev["other"]
    print(ev[ev.rel_min.isin([-30, -10, -5, -1, 0, 1, 2, 3, 5, 10, 15, 30, 60, 90])].round(2).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
