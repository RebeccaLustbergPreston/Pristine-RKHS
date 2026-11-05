"""
Order book depth (mbp-10) → one row per second with book and flow summaries.

    ~/Desktop/TradersAtSMU/.venv_databento/bin/python depth_seconds.py \\
        --symbol CL --months 2023-08,2024-02,2024-08

Needs the databento package, which is kept out of the project venv (same as
ingest.py). Streams each monthly file in chunks; nothing is loaded whole.
Writes data/depth_seconds/<SYM>_<YYYY-MM>.npz for horizon_check.py.

Per second (bucketed by ts_recv, the time we would have seen it), for
seconds with at least one record:
    sec        epoch second
    bid, ask   best bid and ask after the second's last update
    bsz, asz   sizes at the best bid and ask, same moment
    bdep, adep total size over the top five levels, same moment
    ofi        order-flow imbalance summed over the second's updates
               (Cont, Kukanov & Stoikov 2014): bid-side size added at or
               above the previous best bid, minus ask-side size added at or
               below the previous best ask
    tflow      signed traded size: + buyer-initiated, − seller-initiated
    ntr        number of trades
    inst       instrument at the second's end (a change marks a switch)

Crossed or locked books (bid ≥ ask, as in pre-open snapshots) contribute no
quote and no OFI. OFI is not carried across an instrument change.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.abspath(__file__))
_TAS = os.path.dirname(os.path.realpath(_ROOT))      # ~/Desktop/TradersAtSMU
DEPTH_DIRS = {
    "CL": os.path.join(_ROOT, "data", "raw", "CL", "mbp-10"),
    "NG": os.path.join(_TAS, "data_pull", "NG", "depth"),
    "HG": os.path.join(_TAS, "data_pull", "HG", "depth"),
    "ZC": os.path.join(_TAS, "data_pull", "ZC", "depth"),
}
OUT = os.path.join(_ROOT, "data", "depth_seconds")
PX = 1e-9                                   # DBN fixed-point price scale
CHUNK = 5_000_000


def month_file(symbol, month):
    tag = month.replace("-", "")
    hits = glob.glob(os.path.join(DEPTH_DIRS[symbol], "**",
                                  f"glbx-mdp3-{tag}*.mbp-10.dbn.zst"), recursive=True)
    if len(hits) != 1:
        raise FileNotFoundError(f"{symbol} {month}: {len(hits)} depth files found")
    return hits[0]


def event_ofi(bid, bsz, ask, asz, pbid, pbsz, pask, pasz):
    """Cont–Kukanov–Stoikov OFI of each update against the previous best quotes."""
    return ((bid >= pbid) * bsz - (bid <= pbid) * pbsz
            - (ask <= pask) * asz + (ask >= pask) * pasz)


def summarise(a, carry):
    """
    One chunk of mbp-10 records → per-second partial sums and end-of-second
    snapshots. ``carry`` holds the previous valid quote and instrument across
    chunks. Returns (dict of per-second arrays, new carry).
    """
    sec = (a["ts_recv"] // 1_000_000_000).astype(np.int64)
    inst = a["instrument_id"].astype(np.int64)
    bid = a["bid_px_00"].astype(np.float64) * PX
    ask = a["ask_px_00"].astype(np.float64) * PX
    bsz = a["bid_sz_00"].astype(np.float64)
    asz = a["ask_sz_00"].astype(np.float64)
    bdep = sum(a[f"bid_sz_0{i}"].astype(np.float64) for i in range(5))
    adep = sum(a[f"ask_sz_0{i}"].astype(np.float64) for i in range(5))
    valid = (bid > 0) & (ask > 0) & (bid < ask) & (bid < 1e9) & (ask < 1e9)

    # Previous valid quote for each record, carried across the chunk boundary.
    idx = np.where(valid, np.arange(len(a)), -1)
    np.maximum.accumulate(idx, out=idx)
    prev = np.concatenate([[-1], idx[:-1]])
    def _prev(x, c):
        out = np.where(prev >= 0, x[np.maximum(prev, 0)], c)
        return out
    pbid, pask = _prev(bid, carry["bid"]), _prev(ask, carry["ask"])
    pbsz, pasz = _prev(bsz, carry["bsz"]), _prev(asz, carry["asz"])
    pinst = _prev(inst, carry["inst"])
    ofi = event_ofi(bid, bsz, ask, asz, pbid, pbsz, pask, pasz)
    ok = valid & np.isfinite(pbid) & (pinst == inst)
    ofi = np.where(ok, ofi, 0.0)

    act, side = a["action"], a["side"]
    is_tr = act == b"T"
    sign = np.where(side == b"B", 1.0, np.where(side == b"A", -1.0, 0.0))
    tflow = np.where(is_tr, sign * a["size"].astype(np.float64), 0.0)

    # Per-second sums, and the last valid quote in each second.
    useq, start = np.unique(sec, return_index=True)
    ofi_s = np.add.reduceat(ofi, start)
    tfl_s = np.add.reduceat(tflow, start)
    ntr_s = np.add.reduceat(is_tr.astype(np.int64), start)
    end = np.r_[start[1:], len(a)] - 1
    lastv = idx[end]                                  # last valid record ≤ end
    has = lastv >= start                              # a valid quote inside the second
    li = np.maximum(lastv, 0)
    snap = {k: np.where(has, v[li], np.nan) for k, v in
            (("bid", bid), ("ask", ask), ("bsz", bsz), ("asz", asz),
             ("bdep", bdep), ("adep", adep))}
    out = {"sec": useq, "ofi": ofi_s, "tflow": tfl_s, "ntr": ntr_s,
           "inst": inst[end], **snap}
    if idx[-1] >= 0:
        j = idx[-1]
        carry = {"bid": bid[j], "ask": ask[j], "bsz": bsz[j], "asz": asz[j],
                 "inst": inst[j]}
    return out, carry


def merge(parts):
    """Concatenate chunk summaries, combining a second split across chunks."""
    cat = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    useq, inv = np.unique(cat["sec"], return_inverse=True)
    out = {"sec": useq}
    for k in ("ofi", "tflow", "ntr"):
        out[k] = np.bincount(inv, weights=cat[k], minlength=len(useq))
    # Snapshots: the last row for each second that has a quote.
    for k in ("bid", "ask", "bsz", "asz", "bdep", "adep", "inst"):
        v = cat[k].astype(np.float64)
        good = np.isfinite(v) if k != "inst" else np.ones(len(v), bool)
        if k != "inst":
            good &= np.isfinite(cat["bid"])
        o = np.full(len(useq), np.nan)
        rows = np.flatnonzero(good)
        o[inv[rows]] = v[rows]                       # later rows overwrite earlier
        out[k] = o
    out["ntr"] = out["ntr"].astype(np.int64)
    return out


def extract(symbol, month):
    import databento as db
    path = month_file(symbol, month)
    store = db.DBNStore.from_file(path)
    carry = {"bid": np.nan, "ask": np.nan, "bsz": np.nan, "asz": np.nan, "inst": -1}
    parts, n = [], 0
    for a in store.to_ndarray(count=CHUNK):
        part, carry = summarise(a, carry)
        parts.append(part); n += len(a)
    out = merge(parts)
    os.makedirs(OUT, exist_ok=True)
    dest = os.path.join(OUT, f"{symbol}_{month}.npz")
    np.savez_compressed(dest, **out, source=os.path.basename(path),
                        symbols=str(store.symbols))
    q = np.isfinite(out["bid"])
    print(f"  {symbol} {month}: {n:,} records → {len(out['sec']):,} seconds "
          f"({q.mean():.0%} with a quote), {int(out['ntr'].sum()):,} trades → {dest}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbol", required=True, choices=sorted(DEPTH_DIRS))
    ap.add_argument("--months", required=True, help="comma-separated YYYY-MM")
    a = ap.parse_args(argv)
    for m in a.months.split(","):
        extract(a.symbol, m.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
