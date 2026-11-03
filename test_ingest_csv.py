"""
Tests for ingest_csv.py — a Databento CSV parent pull of trades, narrowed to
the continuous series by Databento's own roll, inside ingest.py's run.

Run with: python test_ingest_csv.py   (no market data required)

Fixtures are written the way Databento writes them: CSV in its column layout
and timestamp/price formatting, a metadata.json and a manifest.json beside
the files, and the continuous-series minute bars as real DBN files from
Databento's own encoder. The central test ingests the same trades once as CSV
and once as a DBN continuous pull and requires identical parquet files.
"""

import json
import os
import sys
import tempfile

import pandas as pd
import pyarrow.parquet as pq

try:
    import databento_dbn as dbn
    import zstandard
except ImportError:
    print("FAIL: test_ingest_csv needs the `databento` package (pip install -r requirements.txt)")
    sys.exit(1)

from ingest import IngestError, ingest
from test_ingest import NS, _expect_error, _mapping, _ns, _quiet, check, write_dbn
import test_ingest

DAY1, DAY2, END = "2023-12-01", "2023-12-02", "2023-12-03"
FRONT, NEXT, SPREAD = 101, 102, 900          # NGZ3, NGF4, NGZ3-NGF4


def _ts(ns: int) -> str:
    """Databento CSV timestamp: ISO 8601, nanoseconds, Z."""
    return pd.Timestamp(ns, unit="ns", tz="UTC").strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{ns % NS:09d}Z"


def _px(fixed: int) -> str:
    sign, v = ("-", -fixed) if fixed < 0 else ("", fixed)
    return f"{sign}{v // NS}.{v % NS:09d}"


def _rows():
    """(ts_recv, ts_event, instrument_id, price_fixed, size, side, sequence, raw symbol).
    The roll moves FRONT → NEXT at midnight UTC between DAY1 and DAY2."""
    d1, d2 = _ns(DAY1), _ns(DAY2)
    r = []
    seq = 0
    for day, iids in ((d1, (FRONT, NEXT, SPREAD)), (d2, (FRONT, NEXT, SPREAD))):
        for k in range(6):
            iid = iids[k % 3]
            ts = day + 3600 * NS + k * 20 * NS          # spread over two minutes
            px = -500_000 if iid == SPREAD else 3_125_000_000 + 1_000_000 * k + iid
            sym = {FRONT: "NGZ3", NEXT: "NGF4", SPREAD: "NGZ3-NGF4"}[iid]
            r.append((ts + 900, ts, iid, px, 1 + k, "ABN"[k % 3], seq, sym))
            seq += 1
    # Across midnight: sent on DAY1, received on DAY2. Kept by the DAY2 contract.
    late = d2 - 100
    r.append((d2 + 200, late, NEXT, 3_200_000_000, 7, "B", seq, "NGF4"))
    r.append((d2 + 300, late + 1, FRONT, 3_190_000_000, 8, "A", seq + 1, "NGZ3"))
    return sorted(r)                       # Databento delivers in ts_recv order


def _kept(rows):
    return [x for x in rows
            if x[2] == (FRONT if pd.Timestamp(x[0], unit="ns").date()
                        < pd.Timestamp(DAY2).date() else NEXT)]


def write_csv_job(folder, rows, files_by_day=True, metadata=None, bad_hash=False,
                  header=None):
    os.makedirs(folder, exist_ok=True)
    head = header or ("ts_recv,ts_event,rtype,publisher_id,instrument_id,action,side,"
                      "depth,price,size,flags,ts_in_delta,sequence,symbol")
    groups = {}
    for x in rows:
        # Databento splits daily files by the UTC date of ts_recv.
        key = pd.Timestamp(x[0], unit="ns").strftime("%Y%m%d") if files_by_day else "all"
        groups.setdefault(key, []).append(x)
    entries = []
    for key, rs in sorted(groups.items()):
        lines = [head] + [
            f"{_ts(rcv)},{_ts(evt)},0,1,{iid},T,{side},0,{_px(px)},{size},0,"
            f"{rcv - evt},{seq},{sym}" for rcv, evt, iid, px, size, side, seq, sym in rs]
        data = zstandard.ZstdCompressor().compress(("\n".join(lines) + "\n").encode())
        name = f"glbx-mdp3-{key}.trades.csv.zst"
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(data)
        import hashlib
        digest = hashlib.sha256(data).hexdigest()
        entries.append({"filename": name, "size": len(data),
                        "hash": f"sha256:{'0' * 64 if bad_hash else digest}"})
    with open(os.path.join(folder, "manifest.json"), "w") as fh:
        json.dump({"job_id": "GLBX-TEST", "files": entries}, fh)
    q = {"dataset": "GLBX.MDP3", "schema": "trades", "symbols": ["NG.FUT"],
         "stype_in": "parent", "stype_out": "instrument_id", "start": _ns(DAY1),
         "end": _ns(END), "encoding": "csv", "compression": "zstd"}
    q.update(metadata or {})
    with open(os.path.join(folder, "metadata.json"), "w") as fh:
        json.dump({"query": q}, fh)


def _bars(kept):
    """Databento's minute bars for the kept trades: bucketed by ts_recv."""
    out = {}
    for rcv, _evt, iid, px, size, *_ in kept:
        m = rcv - rcv % (60 * NS)
        b = out.setdefault((m, iid), [px, px, px, px, 0])
        b[1], b[2], b[3], b[4] = max(b[1], px), min(b[2], px), px, b[4] + size
    return [dbn.OHLCVMsg(rtype=dbn.RType.OHLCV_1M, publisher_id=1, instrument_id=iid,
                         ts_event=m, open=o, high=h, low=lo, close=c, volume=v)
            for (m, iid), (o, h, lo, c, v) in sorted(out.items())]


def write_bars(path, kept, tamper=False):
    recs = _bars(kept)
    if tamper:
        recs[0].volume += 1
    m = _mapping("NG.v.0", FRONT, DAY1, DAY2)
    m.intervals += _mapping("NG.v.0", NEXT, DAY2, END).intervals
    write_dbn(path, dbn.Schema.OHLCV_1M, ["NG.v.0"], DAY1, END, recs, [m])


def csv_tree(raw, **kw):
    rows = _rows()
    write_csv_job(f"{raw}/CL_trades_from_nolan/GLBX-TEST", rows, **kw)  # misleading name
    write_bars(f"{raw}/bars/x.dbn.zst", _kept(rows))
    return rows


def dbn_tree(raw):
    """The same kept trades as a DBN continuous NG.v.0 pull."""
    kept = _kept(_rows())
    recs = [dbn.TradeMsg(publisher_id=1, instrument_id=iid, ts_event=evt, price=px,
                         size=size, action=dbn.Action.TRADE,
                         side={"A": dbn.Side.ASK, "B": dbn.Side.BID, "N": dbn.Side.NONE}[side],
                         depth=0, ts_recv=rcv, flags=0, ts_in_delta=rcv - evt, sequence=seq)
            for rcv, evt, iid, px, size, side, seq, _ in kept]
    m = _mapping("NG.v.0", FRONT, DAY1, DAY2)
    m.intervals += _mapping("NG.v.0", NEXT, DAY2, END).intervals
    write_dbn(f"{raw}/trades/t.dbn.zst", dbn.Schema.TRADES, ["NG.v.0"], DAY1, END, recs, [m])
    write_bars(f"{raw}/bars/x.dbn.zst", kept)


# ── tests ────────────────────────────────────────────────────────────────────

def test_csv_parent_pull_equals_the_dbn_continuous_pull():
    with tempfile.TemporaryDirectory() as d:
        csv_tree(f"{d}/a")
        dbn_tree(f"{d}/b")
        ingest("NG", f"{d}/a", f"{d}/out_a", log=_quiet)
        ingest("NG", f"{d}/b", f"{d}/out_b", log=_quiet)
        ta = pq.read_table(f"{d}/out_a/NG/trades.parquet")
        tb = pq.read_table(f"{d}/out_b/NG/trades.parquet")
        assert ta.schema.equals(tb.schema, check_metadata=False), f"{ta.schema}\nvs\n{tb.schema}"
        pd.testing.assert_frame_equal(ta.to_pandas(), tb.to_pandas())


def test_only_the_rolled_contract_is_kept_and_spreads_dropped():
    with tempfile.TemporaryDirectory() as d:
        rows = csv_tree(f"{d}/raw")
        m = ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet)
        t = pd.read_parquet(f"{d}/out/NG/trades.parquet")
        day = t["ts_recv"].dt.date.astype(str)
        assert set(t.loc[day == DAY1, "instrument_id"]) == {FRONT}
        assert set(t.loc[day == DAY2, "instrument_id"]) == {NEXT}
        assert set(t["symbol"]) == {"NG.v.0"}
        e = m["outputs"]["trades"]
        assert e["rows"] == len(_kept(rows)) and e["rows_read"] == len(rows)
        assert e["converted_from_csv"]["roll"]["contracts"] == [
            {"from": DAY1, "instrument_id": FRONT}, {"from": DAY2, "instrument_id": NEXT}]
        assert any("ohlcv-1m" in c and "bars" in c for c in e["checks_passed"])


def test_roll_date_is_the_ts_recv_date():
    with tempfile.TemporaryDirectory() as d:
        csv_tree(f"{d}/raw")
        ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet)
        t = pd.read_parquet(f"{d}/out/NG/trades.parquet")
        late = t[t["ts_event"] < pd.Timestamp(DAY2, tz="UTC")]
        late = late[late["ts_recv"] >= pd.Timestamp(DAY2, tz="UTC")]
        assert list(late["instrument_id"]) == [NEXT], late


def test_bars_that_disagree_fail_loudly():
    with tempfile.TemporaryDirectory() as d:
        rows = csv_tree(f"{d}/raw")
        write_bars(f"{d}/raw/bars/x.dbn.zst", _kept(rows), tamper=True)
        _expect_error(lambda: ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet),
                      "do not reproduce")
        assert not os.path.exists(f"{d}/out/NG/trades.parquet")


def test_without_a_continuous_series_it_refuses_to_guess_the_roll():
    with tempfile.TemporaryDirectory() as d:
        rows = _rows()
        write_csv_job(f"{d}/raw/job", rows)
        # Bars pulled as the parent carry no roll.
        write_dbn(f"{d}/raw/bars/x.dbn.zst", dbn.Schema.OHLCV_1M, ["NG.FUT"], DAY1, END,
                  _bars(_kept(rows)), [_mapping("NGZ3", FRONT, DAY1, END)],
                  stype=dbn.SType.PARENT)
        _expect_error(lambda: ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet),
                      "no continuous-series file")


def test_dbn_trades_take_precedence_and_the_csv_job_is_recorded():
    with tempfile.TemporaryDirectory() as d:
        dbn_tree(f"{d}/raw")
        write_csv_job(f"{d}/raw/csvjob", _rows())
        m = ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet)
        assert "converted_from_csv" not in m["outputs"]["trades"]
        assert [j["why"] for j in m["csv_jobs_not_used"]] == ["DBN trades are present"]


def test_hash_mismatch_with_job_manifest_fails_loudly():
    with tempfile.TemporaryDirectory() as d:
        csv_tree(f"{d}/raw", bad_hash=True)
        _expect_error(lambda: ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet),
                      "does not match")


def test_rows_outside_the_roll_fail_loudly():
    with tempfile.TemporaryDirectory() as d:
        rows = _rows()
        t = _ns("2023-12-05")
        write_csv_job(f"{d}/raw/job", rows + [(t, t, FRONT, 3 * NS, 1, "N", 99, "NGZ3")])
        write_bars(f"{d}/raw/bars/x.dbn.zst", _kept(rows))
        _expect_error(lambda: ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet),
                      "does not cover")


def test_unexpected_csv_columns_fail_loudly():
    with tempfile.TemporaryDirectory() as d:
        csv_tree(f"{d}/raw", header="ts_event,ts_recv,rtype,publisher_id,instrument_id,"
                 "action,side,depth,price,size,flags,ts_in_delta,sequence,symbol")
        _expect_error(lambda: ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet),
                      "CSV trades layout")


def test_continuous_csv_pull_is_refused_not_misread():
    with tempfile.TemporaryDirectory() as d:
        csv_tree(f"{d}/raw", metadata={"stype_in": "continuous", "symbols": ["NG.v.0"]})
        _expect_error(lambda: ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet),
                      "only parent pulls")


def test_csv_job_for_another_root_is_left_alone():
    with tempfile.TemporaryDirectory() as d:
        dbn_tree(f"{d}/raw")
        write_csv_job(f"{d}/raw/hg", _rows(), metadata={"symbols": ["HG.FUT"]})
        m = ingest("NG", f"{d}/raw", f"{d}/out", log=_quiet)
        assert m["csv_jobs_not_used"] == []


def test_dry_run_writes_nothing_and_describes_the_roll():
    with tempfile.TemporaryDirectory() as d:
        csv_tree(f"{d}/raw")
        m = ingest("NG", f"{d}/raw", f"{d}/out", dry_run=True, log=_quiet)
        assert not os.path.exists(f"{d}/out")
        assert m["outputs"]["trades"]["converted_from_csv"]["roll"]["series"] == "NG.v.0"


def test_one_file_or_daily_files_give_the_same_output():
    with tempfile.TemporaryDirectory() as d:
        csv_tree(f"{d}/a")
        csv_tree(f"{d}/b", files_by_day=False)
        ingest("NG", f"{d}/a", f"{d}/oa", log=_quiet)
        ingest("NG", f"{d}/b", f"{d}/ob", log=_quiet)
        pd.testing.assert_frame_equal(pd.read_parquet(f"{d}/oa/NG/trades.parquet"),
                                      pd.read_parquet(f"{d}/ob/NG/trades.parquet"))


if __name__ == "__main__":
    print("ingest_csv tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    failures = test_ingest._failures
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("all tests passed")
