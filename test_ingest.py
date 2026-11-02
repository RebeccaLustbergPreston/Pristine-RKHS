"""
Tests for ingest.py — raw Databento .dbn.zst → data/<SYMBOL>/*.parquet.

Run with: python test_ingest.py   (no market data required)

Every fixture is a real DBN file written with Databento's own encoder, so
the reader under test is the one that will read the real downloads. File and
folder names in the fixtures are deliberately misleading: the ingester must
go by what the header says.
"""

import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace as _NS

import pandas as pd
import pyarrow.parquet as pq

try:
    import databento_dbn as dbn
    import zstandard
except ImportError:
    # Fail, don't skip: a silently skipped suite is how test_pipeline.py
    # stopped running without anyone noticing.
    print("FAIL: test_ingest needs the `databento` package (pip install -r requirements.txt)")
    sys.exit(1)

from ingest import IngestError, ingest, root_of, verify_output

_failures = []
NS = 10**9
DAY = 86400 * NS


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


# ── fixture writer ───────────────────────────────────────────────────────────

def _ns(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").value)


def _mapping(sym, iid, start, end):
    return _NS(raw_symbol=sym, intervals=[_NS(
        start_date=pd.Timestamp(start).date(), end_date=pd.Timestamp(end).date(),
        symbol=str(iid))])


def write_dbn(path, schema, symbols, start, end, records, mappings,
              stype=dbn.SType.CONTINUOUS, dataset="GLBX.MDP3", version=None,
              not_found=None):
    kw = {} if version is None else {"version": version}
    md = dbn.Metadata(dataset=dataset, start=_ns(start), end=_ns(end),
                      stype_in=stype, stype_out=dbn.SType.INSTRUMENT_ID,
                      schema=schema, symbols=list(symbols), mappings=mappings,
                      not_found=not_found, **kw)
    buf = io.BytesIO()
    buf.write(md.encode())
    for r in records:
        buf.write(bytes(r))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = buf.getvalue()
    if path.endswith(".zst"):
        data = zstandard.ZstdCompressor().compress(data)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def trades_file(path, sym, iid, start, end, n=3, price=3.0, first_ts=None, seq0=0,
                recv_delay_ns=1234):
    t0 = _ns(start) if first_ts is None else _ns(first_ts)
    recs = [dbn.TradeMsg(publisher_id=1, instrument_id=iid, ts_event=t0 + i * NS,
                         price=int((price + 0.001 * i) * NS), size=1 + i,
                         action=dbn.Action.TRADE,
                         side=[dbn.Side.ASK, dbn.Side.BID, dbn.Side.NONE][i % 3],
                         depth=0, ts_recv=t0 + i * NS + recv_delay_ns, flags=0,
                         sequence=seq0 + i)
            for i in range(n)]
    return write_dbn(path, dbn.Schema.TRADES, [sym], start, end, recs,
                     [_mapping(sym, iid, start, end)])


def full_ng_tree(raw):
    """NG trades for two 'months' in job-named folders, plus 1-min bars,
    definitions and statistics; names chosen to mislead."""
    trades_file(f"{raw}/job-8812/CL_december.trades.dbn.zst", "NG.v.0", 101,
                "2023-11-30", "2023-12-31", n=4)
    trades_file(f"{raw}/job-9001/whatever.dbn.zst", "NG.v.0", 102,
                "2023-12-31", "2024-01-31", n=5, price=2.5)
    ohlcv = [dbn.OHLCVMsg(rtype=dbn.RType.OHLCV_1M, publisher_id=1, instrument_id=101,
                          ts_event=_ns("2023-12-01") + i * 60 * NS, open=3 * NS,
                          high=4 * NS, low=2 * NS, close=3 * NS, volume=10 + i)
             for i in range(3)]
    write_dbn(f"{raw}/misc/trades_NG.dbn.zst", dbn.Schema.OHLCV_1M, ["NG.v.0"],
              "2023-11-30", "2024-01-31", ohlcv,
              [_mapping("NG.v.0", 101, "2023-11-30", "2024-01-31")])
    t = _ns("2023-12-01")
    defs = [dbn.InstrumentDefMsg(
        publisher_id=1, instrument_id=iid, ts_event=t, ts_recv=t,
        min_price_increment=1_000_000, display_factor=NS, raw_symbol=raw_sym,
        asset="NG", security_type="FUT", instrument_class=dbn.InstrumentClass.FUTURE,
        security_update_action=dbn.SecurityUpdateAction.ADD,
        expiration=t + 25 * DAY, unit_of_measure_qty=10_000 * NS,
        min_price_increment_amount=10 * NS)
        for iid, raw_sym in ((101, "NGZ3"), (102, "NGF4"))]
    write_dbn(f"{raw}/defs.dbn", dbn.Schema.DEFINITION, ["NG.FUT"],
              "2023-11-30", "2024-01-31", defs,
              [_mapping("NGZ3", 101, "2023-11-30", "2024-01-31"),
               _mapping("NGF4", 102, "2023-11-30", "2024-01-31")],
              stype=dbn.SType.PARENT)
    stats = [
        dbn.StatMsg(publisher_id=1, instrument_id=101, ts_event=t, ts_recv=t, ts_ref=t,
                    price=3 * NS, quantity=dbn.UNDEF_STAT_QUANTITY,
                    stat_type=dbn.StatType.SETTLEMENT_PRICE),
        dbn.StatMsg(publisher_id=1, instrument_id=101, ts_event=t, ts_recv=t, ts_ref=t,
                    price=dbn.UNDEF_PRICE, quantity=12345,
                    stat_type=dbn.StatType.OPEN_INTEREST),
    ]
    write_dbn(f"{raw}/stats/x.dbn.zst", dbn.Schema.STATISTICS, ["NG.FUT"],
              "2023-11-30", "2024-01-31", stats,
              [_mapping("NGZ3", 101, "2023-11-30", "2024-01-31")],
              stype=dbn.SType.PARENT)


def _quiet(*_a, **_k):
    pass


def _expect_error(fn, contains):
    try:
        fn()
    except IngestError as e:
        assert contains in str(e), f"wrong error: {e}"
        return
    raise AssertionError(f"expected IngestError mentioning {contains!r}")


# ── tests ────────────────────────────────────────────────────────────────────

def test_root_of_reads_every_symbology():
    cases = {"NG.v.0": "NG", "CL.c.0": "CL", "ZC.n.1": "ZC", "NG.FUT": "NG",
             "LO.OPT": "LO", "NGZ3": "NG", "ZCZ23": "ZC", "6EH4": "6E",
             "NGZ3-NGF4": "NG", "LOZ3 C4800": "LO", "BTCZ3": "BTC",
             "4242": None, "ALL_SYMBOLS": None, "NGZ3-CLF4": None}
    for s, want in cases.items():
        assert root_of(s) == want, f"{s}: {root_of(s)} != {want}"


def test_end_to_end_writes_standard_layout():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        full_ng_tree(raw)
        m = ingest("NG", raw, out, log=_quiet)
        for kind in ("trades", "ohlcv1m", "definitions", "statistics"):
            assert os.path.exists(f"{out}/NG/{kind}.parquet"), kind
            assert m["outputs"][kind]["checks_passed"], kind
        assert os.path.exists(f"{out}/NG/ingest_manifest.json")
        tr = pd.read_parquet(f"{out}/NG/trades.parquet")
        assert len(tr) == 9, len(tr)
        assert str(tr["ts_event"].dtype) == "datetime64[ns, UTC]"
        assert str(tr["ts_recv"].dtype) == "datetime64[ns, UTC]"
        assert set(tr["symbol"]) == {"NG.v.0"}
        assert set(tr["instrument_id"]) == {101, 102}   # switch preserved
        assert set(tr["side"]) == {"A", "B", "N"}       # N kept, not guessed
        assert tr["ts_event"].is_monotonic_increasing   # file order by header start
        assert abs(tr["price"].iloc[0] - 3.0) < 1e-12   # float dollars, unscaled
        ohlcv = pd.read_parquet(f"{out}/NG/ohlcv1m.parquet")
        assert "ts_event" in ohlcv.columns and len(ohlcv) == 3
        defs = pd.read_parquet(f"{out}/NG/definitions.parquet")
        assert set(defs["raw_symbol"]) == {"NGZ3", "NGF4"}
        assert str(defs["expiration"].dtype) == "datetime64[ns, UTC]"


def test_names_are_ignored_metadata_decides():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        full_ng_tree(raw)   # a file is literally named CL_december
        _expect_error(lambda: ingest("CL", raw, out, log=_quiet), "roots found: ['NG']")
        m = ingest("NG", raw, out, log=_quiet)
        srcs = [s["path"] for s in m["outputs"]["trades"]["source_files"]]
        assert any("CL_december" in s for s in srcs), srcs
        # The misleadingly named 'trades_NG' file holds bars, so it lands in ohlcv1m.
        srcs = [s["path"] for s in m["outputs"]["ohlcv1m"]["source_files"]]
        assert any("trades_NG" in s for s in srcs), srcs


def test_manifest_records_actual_record_range_not_the_name():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        full_ng_tree(raw)
        ingest("NG", raw, out, log=_quiet)
        with open(f"{out}/NG/ingest_manifest.json") as fh:
            m = json.load(fh)
        first = m["outputs"]["trades"]["source_files"][0]
        assert first["metadata_start"].startswith("2023-11-30"), first
        assert first["ts_event_min"].startswith("2023-11-30"), first
        assert first["rows"] == 4 and len(first["sha256"]) == 64


def test_undefined_stat_quantity_becomes_missing_not_a_number():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        full_ng_tree(raw)
        ingest("NG", raw, out, log=_quiet)
        st = pd.read_parquet(f"{out}/NG/statistics.parquet")
        q = st.set_index("stat_type")["quantity"]
        assert pd.isna(q.loc[3]), q          # settlement: no quantity
        assert q.loc[9] == 12345, q          # open interest
        assert pd.isna(st.set_index("stat_type")["price"].loc[9])


def test_identical_copy_is_skipped_and_recorded():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        p = trades_file(f"{raw}/a/x.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        os.makedirs(f"{raw}/b")
        with open(p, "rb") as src, open(f"{raw}/b/copy.dbn.zst", "wb") as dst:
            dst.write(src.read())
        m = ingest("NG", raw, out, log=_quiet)
        assert m["outputs"]["trades"]["rows"] == 3
        assert len(m["outputs"]["trades"]["duplicates_skipped"]) == 1


def test_overlapping_different_files_fail_loudly():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        trades_file(f"{raw}/b.dbn.zst", "NG.v.0", 1, "2024-01-15", "2024-03-01", n=2)
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "same instruments")
        assert not os.path.exists(f"{out}/NG/trades.parquet")


def test_mixed_roll_conventions_fail_loudly():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        trades_file(f"{raw}/b.dbn.zst", "NG.c.0", 1, "2024-02-01", "2024-03-01")
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "different series")


def test_mixed_datasets_fail_loudly():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        p = f"{raw}/b.dbn.zst"
        write_dbn(p, dbn.Schema.TRADES, ["NG.v.0"], "2024-02-01", "2024-03-01", [],
                  [_mapping("NG.v.0", 1, "2024-02-01", "2024-03-01")], dataset="IFEU.IMPACT")
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "different datasets")


def test_coverage_gap_is_recorded_not_filled():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        trades_file(f"{raw}/b.dbn.zst", "NG.v.0", 1, "2024-03-01", "2024-04-01")
        m = ingest("NG", raw, out, log=_quiet)
        gaps = m["outputs"]["trades"]["coverage_gaps"]
        assert len(gaps) == 1 and gaps[0]["from"].startswith("2024-02-01"), gaps
        assert m["outputs"]["trades"]["rows"] == 6


def test_record_overlap_across_files_is_flagged_for_cleaning():
    # Feb file's first trade happened at 23:59:59.9 on 31 Jan but was received
    # after midnight — it belongs to February's file yet sits before January's
    # last trade in event time. Legitimate, but cleaning must know.
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01",
                    n=1, first_ts="2024-01-31T23:59:59.95")
        trades_file(f"{raw}/b.dbn.zst", "NG.v.0", 1, "2024-02-01", "2024-03-01",
                    n=1, first_ts="2024-01-31T23:59:59.90", recv_delay_ns=200_000_000)
        m = ingest("NG", raw, out, log=_quiet)
        assert len(m["outputs"]["trades"]["record_time_overlaps"]) == 1


def test_row_outside_the_files_own_mappings_fails_loudly():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn.zst", "NG.v.0", 1, "2024-02-01", "2024-03-01",
                    first_ts="2024-01-15")
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "no symbol mapping")
        assert not os.path.exists(f"{out}/NG/trades.parquet")


def test_file_with_several_roots_keeps_only_requested_rows():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        recs = [dbn.TradeMsg(publisher_id=1, instrument_id=iid,
                             ts_event=_ns("2024-01-02") + i * NS, price=3 * NS, size=1,
                             action=dbn.Action.TRADE, side=dbn.Side.BID, depth=0,
                             ts_recv=_ns("2024-01-02") + i * NS, flags=0, sequence=i)
                for i, iid in enumerate((1, 2, 1, 2, 2))]
        write_dbn(f"{raw}/combo.dbn.zst", dbn.Schema.TRADES, ["NG.v.0", "HG.v.0"],
                  "2024-01-01", "2024-02-01", recs,
                  [_mapping("NG.v.0", 1, "2024-01-01", "2024-02-01"),
                   _mapping("HG.v.0", 2, "2024-01-01", "2024-02-01")])
        ingest("NG", raw, out, log=_quiet)
        ingest("HG", raw, out, log=_quiet)
        ng = pd.read_parquet(f"{out}/NG/trades.parquet")
        hg = pd.read_parquet(f"{out}/HG/trades.parquet")
        assert len(ng) == 2 and set(ng["symbol"]) == {"NG.v.0"}
        assert len(hg) == 3 and set(hg["symbol"]) == {"HG.v.0"}


def test_existing_output_is_not_overwritten_without_force():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        ingest("NG", raw, out, log=_quiet)
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "--force")
        ingest("NG", raw, out, force=True, log=_quiet)


def test_failure_part_way_leaves_previous_outputs_untouched():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        full_ng_tree(raw)
        ingest("NG", raw, out, log=_quiet)
        before = pd.read_parquet(f"{out}/NG/definitions.parquet")
        # A new trades month whose rows fall outside its own mappings: the
        # run must fail, and definitions (written first, alphabetically) must
        # not be replaced by a half-finished run.
        trades_file(f"{raw}/late.dbn.zst", "NG.v.0", 102, "2024-02-01", "2024-03-01",
                    first_ts="2024-01-15")
        _expect_error(lambda: ingest("NG", raw, out, force=True, log=_quiet),
                      "no symbol mapping")
        pd.testing.assert_frame_equal(before, pd.read_parquet(f"{out}/NG/definitions.parquet"))
        leftovers = [n for n in os.listdir(f"{out}/NG") if n.endswith(".partial")]
        assert not leftovers, leftovers


def test_truncated_download_fails_loudly():
    # An interrupted download: the records stop part-way through one.
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        for name in ("a.dbn", "b.dbn.zst"):
            p = trades_file(f"{d}/full.dbn", "NG.v.0", 1, "2024-01-01", "2024-02-01", n=500)
            with open(p, "rb") as fh:
                data = fh.read()[:-20]
            if name.endswith(".zst"):
                data = zstandard.ZstdCompressor().compress(data)
            os.makedirs(raw, exist_ok=True)
            with open(f"{raw}/{name}", "wb") as fh:
                fh.write(data)
            _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "truncated")
            os.remove(f"{raw}/{name}")


def test_old_dbn_version_is_upgraded_and_its_sentinel_becomes_missing():
    # A statistics download from before DBN v3 stores "no quantity" as the
    # 32-bit INT32_MAX. It must come out missing, just like a v3 file's.
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        for ver, cls, start, end, undef in (
                (1, dbn.v1.StatMsg, "2024-01-01", "2024-02-01", 2**31 - 1),
                (3, dbn.StatMsg, "2024-02-01", "2024-03-01", dbn.UNDEF_STAT_QUANTITY)):
            t = _ns(start) + NS
            recs = [cls(publisher_id=1, instrument_id=101, ts_event=t, ts_recv=t, ts_ref=t,
                        price=3 * NS, quantity=undef,
                        stat_type=dbn.StatType.SETTLEMENT_PRICE),
                    cls(publisher_id=1, instrument_id=101, ts_event=t, ts_recv=t, ts_ref=t,
                        price=dbn.UNDEF_PRICE, quantity=777,
                        stat_type=dbn.StatType.OPEN_INTEREST)]
            write_dbn(f"{raw}/v{ver}.dbn", dbn.Schema.STATISTICS, ["NG.FUT"], start, end,
                      recs, [_mapping("NGH4", 101, start, end)],
                      stype=dbn.SType.PARENT, version=ver)
        m = ingest("NG", raw, out, log=_quiet)
        st = pd.read_parquet(f"{out}/NG/statistics.parquet")
        assert len(st) == 4
        settle = st[st["stat_type"] == 3]["quantity"]
        assert settle.isna().all(), settle.tolist()
        assert st[st["stat_type"] == 9]["quantity"].tolist() == [777, 777]
        versions = [s["dbn_version"] for s in m["outputs"]["statistics"]["source_files"]]
        assert versions == [1, 3], versions


def test_shared_file_with_unassignable_spread_fails_rather_than_dropping():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        t = _ns("2024-01-02")
        recs = [dbn.TradeMsg(publisher_id=1, instrument_id=iid, ts_event=t + i * NS,
                             price=3 * NS, size=1, action=dbn.Action.TRADE,
                             side=dbn.Side.BID, depth=0, ts_recv=t + i * NS, flags=0,
                             sequence=i)
                for i, iid in enumerate((1, 3, 2))]
        write_dbn(f"{raw}/p.dbn", dbn.Schema.TRADES, ["NG.FUT", "HG.FUT"],
                  "2024-01-01", "2024-02-01", recs,
                  [_mapping("NGH4", 1, "2024-01-01", "2024-02-01"),
                   _mapping("HGH4", 2, "2024-01-01", "2024-02-01"),
                   _mapping("NGH4-HGJ4", 3, "2024-01-01", "2024-02-01")],
                  stype=dbn.SType.PARENT)
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "no single root")


def test_stale_output_from_earlier_run_is_refused_not_deleted():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        full_ng_tree(raw)
        ingest("NG", raw, out, log=_quiet)
        import shutil
        shutil.rmtree(f"{raw}/stats")
        _expect_error(lambda: ingest("NG", raw, out, force=True, log=_quiet),
                      "from an earlier run")
        assert os.path.exists(f"{out}/NG/statistics.parquet")


def test_symbols_databento_did_not_find_do_not_claim_a_file():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        write_dbn(f"{raw}/a.dbn", dbn.Schema.TRADES, ["NG.v.0", "HG.v.0"],
                  "2024-01-01", "2024-02-01", [],
                  [_mapping("NG.v.0", 1, "2024-01-01", "2024-02-01")],
                  not_found=["HG.v.0"])
        _expect_error(lambda: ingest("HG", raw, out, log=_quiet), "roots found: ['NG']")


def test_misleading_name_is_flagged_but_header_still_decides():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/CL_jan.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        trades_file(f"{raw}/crude/x.dbn.zst", "CL.v.0", 2, "2024-01-01", "2024-02-01")
        trades_file(f"{raw}/NG_feb.dbn.zst", "NG.v.0", 1, "2024-02-01", "2024-03-01")
        m = ingest("NG", raw, out, log=_quiet)
        assert m["outputs"]["trades"]["rows"] == 6
        mm = m["name_mismatches"]
        assert [x["path"] for x in mm] == ["CL_jan.dbn.zst"], mm
        assert mm[0]["name_says"] == ["CL"] and mm[0]["header_says"] == ["NG"]


def test_list_reports_every_file_and_writes_nothing():
    from ingest import list_raw
    with tempfile.TemporaryDirectory() as d:
        raw = f"{d}/raw"
        full_ng_tree(raw)
        trades_file(f"{raw}/other/x.dbn.zst", "CL.v.0", 9, "2024-01-01", "2024-02-01")
        lines = []
        files = list_raw(raw, log=lines.append)
        assert len(files) == 6
        text = "\n".join(lines)
        assert "CL" in text and "statistics" in text and "ohlcv-1m" in text
        assert "CL_december" in text and "WARNING" in text   # misleading name
        assert os.listdir(d) == ["raw"]


def test_chunked_decoding_gives_identical_output():
    with tempfile.TemporaryDirectory() as d:
        raw = f"{d}/raw"
        full_ng_tree(raw)
        ingest("NG", raw, f"{d}/one", log=_quiet)
        ingest("NG", raw, f"{d}/many", chunk_rows=2, log=_quiet)
        for kind in ("trades", "statistics", "definitions"):
            a = pd.read_parquet(f"{d}/one/NG/{kind}.parquet")
            b = pd.read_parquet(f"{d}/many/NG/{kind}.parquet")
            pd.testing.assert_frame_equal(a, b)
        # Each chunk is its own row group, so this proves chunking happened.
        assert pq.ParquetFile(f"{d}/many/NG/trades.parquet").num_row_groups > 1


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        full_ng_tree(raw)
        m = ingest("NG", raw, out, dry_run=True, log=_quiet)
        assert set(m["outputs"]) == {"trades", "ohlcv1m", "definitions", "statistics"}
        assert not os.path.exists(f"{out}/NG")


def test_unreadable_file_fails_loudly():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        with open(f"{raw}/broken.dbn.zst", "wb") as fh:
            fh.write(b"not a dbn file")
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "could not read")


def test_verify_output_rejects_missing_column_and_missing_ids():
    with tempfile.TemporaryDirectory() as d:
        ts = pd.to_datetime(["2024-01-02"], utc=True).as_unit("ns")
        good = dict(ts_event=ts, ts_recv=ts, price=[1.0], size=[1], side=["A"],
                    instrument_id=[1], symbol=["NG.v.0"], sequence=[0])
        pd.DataFrame({k: v for k, v in good.items() if k != "side"}).to_parquet(f"{d}/a.parquet")
        _expect_error(lambda: verify_output(f"{d}/a.parquet", "trades"), "missing required")
        bad = dict(good, instrument_id=pd.array([None], dtype="Int64"))
        pd.DataFrame(bad).to_parquet(f"{d}/b.parquet")
        _expect_error(lambda: verify_output(f"{d}/b.parquet", "trades"), "missing values")
        naive = dict(good, ts_event=pd.to_datetime(["2024-01-02"]))
        pd.DataFrame(naive).to_parquet(f"{d}/c.parquet")
        _expect_error(lambda: verify_output(f"{d}/c.parquet", "trades"), "UTC")
        chicago = dict(good, ts_event=ts.tz_convert("America/Chicago"))
        pd.DataFrame(chicago).to_parquet(f"{d}/tz.parquet")
        _expect_error(lambda: verify_output(f"{d}/tz.parquet", "trades"), "UTC")
        nosym = dict(good, symbol=pd.array([None], dtype="str"))
        pd.DataFrame(nosym).to_parquet(f"{d}/s.parquet")
        _expect_error(lambda: verify_output(f"{d}/s.parquet", "trades"), "missing values")
        nots = dict(good, ts_event=pd.to_datetime([None], utc=True).as_unit("ns"))
        pd.DataFrame(nots).to_parquet(f"{d}/t.parquet")
        _expect_error(lambda: verify_output(f"{d}/t.parquet", "trades"), "no ts_event")
        pd.DataFrame(good).to_parquet(f"{d}/ok.parquet")
        assert verify_output(f"{d}/ok.parquet", "trades")
        _expect_error(lambda: verify_output(f"{d}/ok.parquet", "trades", expected_rows=2),
                      "decoded")


def _trade(iid, ts, seq=0):
    return dbn.TradeMsg(publisher_id=1, instrument_id=iid, ts_event=ts, price=3 * NS,
                        size=1, action=dbn.Action.TRADE, side=dbn.Side.BID, depth=0,
                        ts_recv=ts, flags=0, sequence=seq)


def test_files_are_ordered_by_header_start_not_by_name():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a_later.dbn.zst", "NG.v.0", 1, "2024-02-01", "2024-03-01")
        trades_file(f"{raw}/z_earlier.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        ingest("NG", raw, out, log=_quiet)
        tr = pd.read_parquet(f"{out}/NG/trades.parquet")
        assert tr["ts_event"].is_monotonic_increasing, tr["ts_event"].tolist()


def test_rows_are_not_sorted_within_a_file():
    # Sorting is cleaning rule d — B must see the raw order to assert on it.
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        t = _ns("2024-01-02")
        recs = [_trade(1, t + k * NS, seq=i) for i, k in enumerate((3, 1, 2))]
        write_dbn(f"{raw}/a.dbn", dbn.Schema.TRADES, ["NG.v.0"], "2024-01-01",
                  "2024-02-01", recs, [_mapping("NG.v.0", 1, "2024-01-01", "2024-02-01")])
        ingest("NG", raw, out, log=_quiet)
        tr = pd.read_parquet(f"{out}/NG/trades.parquet")
        assert tr["sequence"].tolist() == [0, 1, 2]
        assert not tr["ts_event"].is_monotonic_increasing


def test_incompatible_column_layouts_fail_loudly():
    from ingest import OutputPlan, write_output
    import ingest as ing
    with tempfile.TemporaryDirectory() as d:
        raw = f"{d}/raw"
        a = trades_file(f"{raw}/a.dbn", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        b = trades_file(f"{raw}/b.dbn", "NG.v.0", 1, "2024-02-01", "2024-03-01")
        files = [ing.read_header(a), ing.read_header(b)]
        for f in files:
            f.sha256 = "x" + f.path[-6:]
        real = ing._frames

        def frames(f, *args):
            for df in real(f, *args):
                yield df.assign(price="not a price") if f.path == files[1].path else df
        ing._frames = frames
        try:
            plan = OutputPlan(kind="trades", schema="trades", files=files)
            _expect_error(lambda: write_output(plan, f"{d}/t.parquet.partial", "NG"),
                          "different column layout")
            assert not os.path.exists(f"{d}/t.parquet.partial")
        finally:
            ing._frames = real


def test_batch_split_by_symbol_is_accepted():
    # One parent pull, split per contract: same period, disjoint instruments.
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        t = _ns("2024-01-02")
        for iid, sym in ((1, "NGG4"), (2, "NGH4")):
            write_dbn(f"{raw}/job/{sym}.dbn", dbn.Schema.TRADES, ["NG.FUT"],
                      "2024-01-01", "2024-02-01", [_trade(iid, t + iid * NS)],
                      [_mapping(sym, iid, "2024-01-01", "2024-02-01")],
                      stype=dbn.SType.PARENT)
        m = ingest("NG", raw, out, log=_quiet)
        assert m["outputs"]["trades"]["rows"] == 2
        assert m["outputs"]["trades"]["coverage_gaps"] == []


def _write_job_manifest(folder, names):
    import hashlib
    files = []
    for n in names:
        with open(f"{folder}/{n}", "rb") as fh:
            files.append({"filename": n, "hash": "sha256:" + hashlib.sha256(fh.read()).hexdigest()})
    with open(f"{folder}/manifest.json", "w") as fh:
        json.dump({"files": files}, fh)


def test_batch_split_by_size_needs_the_jobs_manifest():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        t = _ns("2024-01-02")
        for part, k in (("p1", 0), ("p2", 5)):
            write_dbn(f"{raw}/job/{part}.dbn", dbn.Schema.TRADES, ["NG.v.0"],
                      "2024-01-01", "2024-02-01", [_trade(1, t + k * NS, seq=k)],
                      [_mapping("NG.v.0", 1, "2024-01-01", "2024-02-01")])
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "manifest.json")
        _write_job_manifest(f"{raw}/job", ["p1.dbn", "p2.dbn"])
        m = ingest("NG", raw, out, log=_quiet)
        assert m["outputs"]["trades"]["rows"] == 2
        assert all(s["batch_job"] for s in m["outputs"]["trades"]["source_files"])


def test_size_parts_keep_their_order_past_part_nine():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        t = _ns("2024-01-02")
        names = [f"part{i}.dbn" for i in range(12)]
        for i, n in enumerate(names):
            write_dbn(f"{raw}/job/{n}", dbn.Schema.TRADES, ["NG.v.0"],
                      "2024-01-01", "2024-02-01", [_trade(1, t + i * NS, seq=i)],
                      [_mapping("NG.v.0", 1, "2024-01-01", "2024-02-01")])
        _write_job_manifest(f"{raw}/job", names)
        ingest("NG", raw, out, log=_quiet)
        tr = pd.read_parquet(f"{out}/NG/trades.parquet")
        assert tr["sequence"].tolist() == list(range(12)), tr["sequence"].tolist()


def test_foreign_manifest_json_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/a.dbn", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        for i, body in enumerate(({"files": ["a", None]}, {"files": [{"filename": 123}]},
                                  ["x"], "not json")):
            os.makedirs(f"{raw}/other{i}")
            with open(f"{raw}/other{i}/manifest.json", "w") as fh:
                fh.write(body if isinstance(body, str) else json.dumps(body))
        assert ingest("NG", raw, out, log=_quiet)["outputs"]["trades"]["rows"] == 3


def test_compression_is_detected_by_content_not_extension():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/plain_but_named.dbn.zst".replace(".zst", ""), "NG.v.0", 1,
                    "2024-01-01", "2024-02-01")
        os.rename(f"{raw}/plain_but_named.dbn", f"{raw}/plain_but_named.dbn.zst")
        assert ingest("NG", raw, out, log=_quiet)["outputs"]["trades"]["rows"] == 3


def test_upgraded_copies_are_cleaned_up():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        t = _ns("2024-01-01") + NS
        rec = dbn.v1.StatMsg(publisher_id=1, instrument_id=101, ts_event=t, ts_recv=t,
                             ts_ref=t, price=3 * NS, quantity=5,
                             stat_type=dbn.StatType.SETTLEMENT_PRICE)
        write_dbn(f"{raw}/v1.dbn", dbn.Schema.STATISTICS, ["NG.FUT"], "2024-01-01",
                  "2024-02-01", [rec], [_mapping("NGH4", 101, "2024-01-01", "2024-02-01")],
                  stype=dbn.SType.PARENT, version=1)
        ingest("NG", raw, out, log=_quiet)
        assert sorted(os.listdir(f"{out}/NG")) == ["ingest_manifest.json", "statistics.parquet"]


def test_hash_mismatch_with_job_manifest_fails_loudly():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        trades_file(f"{raw}/job/a.dbn", "NG.v.0", 1, "2024-01-01", "2024-02-01")
        _write_job_manifest(f"{raw}/job", ["a.dbn"])
        trades_file(f"{raw}/job/a.dbn", "NG.v.0", 1, "2024-01-01", "2024-02-01", n=2)
        _expect_error(lambda: ingest("NG", raw, out, log=_quiet), "does not match")


def test_zstd_stream_cut_short_fails_loudly():
    # Cut the *compressed* bytes, as an interrupted download does. Whether
    # or not the cut lands mid-record, the unfinished zstd frame must be caught.
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        p = trades_file(f"{d}/full.dbn.zst", "NG.v.0", 1, "2024-01-01", "2024-02-01", n=3000)
        with open(p, "rb") as fh:
            data = fh.read()
        os.makedirs(raw)
        for cut in (len(data) // 2, len(data) - 3):
            with open(f"{raw}/a.dbn.zst", "wb") as fh:
                fh.write(data[:cut])
            from ingest import _sha256
            _expect_error(lambda: _sha256(f"{raw}/a.dbn.zst"), "re-download")
            try:
                ingest("NG", raw, out, log=_quiet)
            except IngestError:
                continue
            raise AssertionError(f"cut at {cut}/{len(data)} was ingested")


def test_strategy_symbols_and_smart_stype():
    assert root_of("NG:BF H4-J4-K4") == "NG"
    assert root_of("NG:C1 F4-G4") == "NG"
    assert root_of("UD:1V: VT 0102999") is None
    from ingest import RawFile, _stype_label
    f = RawFile(path="x", sha256=None, n_bytes=0, dataset="GLBX.MDP3", schema="trades",
                stype_in="smart", symbols=("NG.v.0",), start_ns=0, end_ns=1,
                roots=("NG",), unidentified_symbols=())
    assert _stype_label(f) == "continuous"


def test_dry_run_predicts_the_refusal_of_the_real_run():
    with tempfile.TemporaryDirectory() as d:
        raw, out = f"{d}/raw", f"{d}/data"
        full_ng_tree(raw)
        ingest("NG", raw, out, log=_quiet)
        _expect_error(lambda: ingest("NG", raw, out, dry_run=True, log=_quiet), "--force")


if __name__ == "__main__":
    print("ingest tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all tests passed")
