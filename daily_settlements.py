"""
Settlement statistics (from pull_daily_history.py) → one settlement price
per contract per trading date, for the front and second contract.

    ~/Desktop/TradersAtSMU/.venv_databento/bin/python daily_settlements.py

Needs the databento package (kept out of the project venv). Writes
data/daily_history/settlements_<ROOT>.parquet with columns
    date (trading date, from ts_ref), rank (0 front, 1 second),
    instrument_id, settle
Settlement is stat_type 3. CME publishes several updates per trading date
(preliminary, then final); the last one received is kept. f1_long.py builds
returns and carry from this table.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
ROLL = {"CL": "v", "NG": "v", "HG": "v", "GC": "n", "ZC": "n", "ZT": "n", "ES": "n", "BTC": "c"}
OUT = Path(_ROOT, "data", "daily_history")
SETTLEMENT = 3


def settlements(path, rank):
    import databento as db
    df = db.DBNStore.from_file(path).to_df()
    s = df[df["stat_type"] == SETTLEMENT].sort_index(kind="stable")
    s = s.assign(date=s["ts_ref"].dt.tz_convert(None).dt.normalize())
    last = s.groupby(["instrument_id", "date"], sort=True).tail(1)
    return pd.DataFrame({"date": last["date"].to_numpy(), "rank": rank,
                         "instrument_id": last["instrument_id"].to_numpy(),
                         "settle": last["price"].to_numpy(float)})


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for root, r in ROLL.items():
        parts = []
        for rank in (0, 1):
            p = Path(_ROOT, "data", "raw", root, "daily_history", f"{root}.{r}.{rank}.statistics.dbn.zst")
            if not p.exists():
                print(f"  {root}: missing {p.name}")
                break
            parts.append(settlements(p, rank))
        else:
            t = pd.concat(parts, ignore_index=True).sort_values(["date", "rank"])
            t.to_parquet(OUT / f"settlements_{root}.parquet", index=False)
            n0 = (t["rank"] == 0).sum()
            print(f"  {root}: {n0:,} front settlements, {len(t) - n0:,} second, "
                  f"{t['date'].min():%Y-%m-%d} → {t['date'].max():%Y-%m-%d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
