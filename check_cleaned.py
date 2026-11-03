"""
The "before you call it done" checks of docs/data_cleaning_standard.md §5,
run on a cleaned contract.

    python check_cleaned.py --symbol NG [--dir data/cleaned-master] [--seed 0]

Prints each check and its evidence; exits 1 if any fails. Two of the
standard's checks ask for a person — "price range sanity-checked against a
public chart" and "a second person has read the report". The first is done
here against the exchange's own settlement prices for two random sessions;
the second is still a person's job.
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from clean import REQUIRED_TRADES, TRADE_TYPES, _SETTLEMENT

TYPES = {"ts_event": "datetime64[ns, UTC]", "ts_recv": "datetime64[ns, UTC]",
         "session_date": "datetime64[ns]", **TRADE_TYPES}


def run(symbol: str, base: str, seed: int) -> bool:
    d = os.path.join(base, symbol)
    with open(os.path.join(d, "cleaning_report.json")) as fh:
        rep = json.load(fh)
    pf = pq.ParquetFile(os.path.join(d, "trades.parquet"))
    ok = True

    def say(passed, msg):
        nonlocal ok
        ok &= bool(passed)
        print(f"  {'PASS' if passed else 'FAIL'}  {msg}")

    cols = list(REQUIRED_TRADES) + ["session_date", "sequence"]
    nulls = {c: 0 for c in cols}
    prev = None
    mono = True
    switches = 0
    last_id = None
    per_session = {}
    for i in range(pf.num_row_groups):
        t = pf.read_row_group(i, columns=cols).to_pandas()
        if i == 0:
            bad = {c: str(t[c].dtype) for c, want in TYPES.items()
                   if not str(t[c].dtype).startswith(want)}
            say(not bad, f"required columns present with the standard's types {bad or ''}")
        for c in cols:
            nulls[c] += int(t[c].isna().sum())
        ts = t["ts_event"]
        if prev is not None and ts.iloc[0] < prev:
            mono = False
        mono &= bool(ts.is_monotonic_increasing)
        prev = ts.iloc[-1]
        ids = t["instrument_id"].to_numpy()
        switches += int((ids[1:] != ids[:-1]).sum()) + int(last_id is not None and ids[0] != last_id)
        last_id = ids[-1]
        for s, n in t.groupby("session_date").size().items():
            per_session[s] = per_session.get(s, 0) + int(n)
    say(not any(nulls.values()), f"no missing values in required columns ({sum(nulls.values())})")
    say(mono, "ts_event monotonically non-decreasing across the whole file")
    say(switches == len(rep["contract_switches"]),
        f"every instrument switch is in the report ({switches} found, "
        f"{len(rep['contract_switches'])} listed)")
    say(sum(per_session.values()) == rep["rows_out"] == pf.metadata.num_rows,
        f"rows per session add up to the report's rows_out ({pf.metadata.num_rows:,})")
    zero = rep["sessions_with_zero_rows"]
    say(not zero, f"sessions with zero rows: {zero or 'none'}; "
        f"min/median/max rows per session {rep['rows_per_session']}")

    # session_date on an evening trade: first trade after 17:00 Chicago time
    # on a normal weekday must carry the next trading day's date.
    rng = np.random.default_rng(seed)
    t = pf.read_row_group(int(rng.integers(pf.num_row_groups)),
                          columns=["ts_event", "session_date"]).to_pandas()
    ct = t["ts_event"].dt.tz_convert("America/Chicago")
    ev = t[(ct.dt.hour >= 17) & (ct.dt.hour < 23)]
    if len(ev):
        r = ev.iloc[0]
        c = r["ts_event"].tz_convert("America/Chicago")
        say(r["session_date"].date() > c.date(),
            f"evening trade {c:%Y-%m-%d %H:%M:%S} Chicago is in session "
            f"{r['session_date'].date()} (the next trading day)")
    else:
        print("  NOTE  no evening trade in the sampled row group")

    # Prices against the exchange's own settlement on two random sessions.
    st = pd.read_parquet(os.path.join(d, "statistics.parquet"),
                         columns=["stat_type", "ts_ref", "price", "instrument_id"])
    st = st[st["stat_type"] == _SETTLEMENT].dropna(subset=["price"])
    st["day"] = pd.to_datetime(st["ts_ref"], utc=True).dt.tz_localize(None).dt.normalize()
    st = st.drop_duplicates(["day", "instrument_id"], keep="last")
    sessions = sorted(per_session)
    for s in rng.choice(len(sessions), 2, replace=False):
        day = pd.Timestamp(sessions[s])
        tr = pq.read_table(os.path.join(d, "trades.parquet"),
                           columns=["session_date", "price", "instrument_id"],
                           filters=[("session_date", "=", day)]).to_pandas()
        iid = tr["instrument_id"].mode().iloc[0]
        stl = st[(st["day"] == day) & (st["instrument_id"] == iid)]
        if stl.empty:
            print(f"  NOTE  no settlement for {day.date()} instrument {iid}")
            continue
        sp = float(stl["price"].iloc[0])
        lo, hi = tr.loc[tr["instrument_id"] == iid, "price"].agg(["min", "max"])
        say(lo <= sp <= hi or abs(sp / tr["price"].iloc[-1] - 1) < 0.01,
            f"{day.date()}: traded {lo:g}–{hi:g}, exchange settled at {sp:g}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "data", "cleaned-master"))
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    print(a.symbol.upper())
    sys.exit(0 if run(a.symbol.upper(), a.dir, a.seed) else 1)


if __name__ == "__main__":
    main()
