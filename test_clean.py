"""
Tests for clean.py — ingested parquet → docs/data_cleaning_standard.md format.

Run with: python test_clean.py   (no market data required)

Each fixture is a small data/<SYMBOL>/ tree in the layout ingest.py writes.
"""

import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

import clean as C
from clean import CleanError, clean

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _quiet(*_a, **_k):
    pass


def _expect_error(fn, contains):
    try:
        fn()
    except CleanError as e:
        assert contains in str(e), f"wrong error: {e}"
        return
    raise AssertionError(f"expected CleanError mentioning {contains!r}")


UTC = "UTC"
# Settlement days: 3 July, 5 July (4 July is a holiday), 6 July.
DAYS = ["2023-07-03", "2023-07-05", "2023-07-06"]


def _t(s):
    return pd.Timestamp(s, tz=UTC)


def trade(ts, price=70.0, size=1, iid=1, side="B", seq=0, action="T", rtype=0):
    t = _t(ts)
    return {"ts_recv": t + pd.Timedelta(500, "ns"), "ts_event": t, "rtype": np.uint8(rtype),
            "publisher_id": np.uint16(1), "instrument_id": np.uint32(iid),
            "action": action, "side": side, "depth": np.uint8(0), "price": float(price),
            "size": np.uint32(size), "flags": np.uint8(0), "ts_in_delta": np.int32(500),
            "sequence": np.uint32(seq), "symbol": "CL.v.0"}


def tree(d, trades, days=DAYS, defs_extra=None):
    src = f"{d}/data/CL"
    os.makedirs(src, exist_ok=True)
    pd.DataFrame(trades).to_parquet(f"{src}/trades.parquet", index=False)
    bars = pd.DataFrame({"ts_event": [_t("2023-07-03 14:00")], "rtype": np.uint8(33),
                         "publisher_id": np.uint16(1), "instrument_id": np.uint32(1),
                         "open": 70.0, "high": 70.0, "low": 70.0, "close": 70.0,
                         "volume": np.uint64(1), "symbol": "CL.v.0"})
    bars.to_parquet(f"{src}/ohlcv1m.parquet", index=False)
    stats = pd.DataFrame({
        "ts_recv": [_t(x) + pd.Timedelta(hours=20) for x in days],
        "ts_event": [_t(x) + pd.Timedelta(hours=20) for x in days],
        "ts_ref": [_t(x) for x in days], "stat_type": np.uint8(3), "price": 70.0,
        "quantity": pd.array([pd.NA] * len(days), dtype="Int64"),
        "instrument_id": np.uint32(1), "symbol": "CL.v.0"})
    stats.to_parquet(f"{src}/statistics.parquet", index=False)
    defs = pd.DataFrame({"ts_recv": [_t(DAYS[0])], "raw_symbol": ["CLQ3"],
                         "instrument_id": np.uint32(1),
                         "expiration": [_t("2023-07-20")], "min_price_increment": 0.01,
                         "min_price_increment_amount": 10.0, "unit_of_measure_qty": 1000.0,
                         "contract_multiplier": np.int32(2**31 - 1), "symbol": "CL.v.0",
                         **(defs_extra or {})})
    defs.to_parquet(f"{src}/definitions.parquet", index=False)
    with open(f"{src}/ingest_manifest.json", "w") as fh:
        json.dump({"ingested_on": "2026-11-03", "outputs": {"trades": {
            "rows": len(trades), "series": ["CL.v.0"],
            "source_files": [{"path": "x.dbn.zst"}], "checks_passed": []}}}, fh)
    return f"{d}/data", f"{d}/out"


def run(d, trades, **kw):
    i, o = tree(d, trades, **kw)
    r = clean("CL", i, o, "test", log=_quiet)
    return r, pd.read_parquet(f"{o}/CL/trades.parquet")


# ── tests ────────────────────────────────────────────────────────────────────

def test_session_starts_at_1700_chicago_in_daylight_time():
    with tempfile.TemporaryDirectory() as d:
        # 5 July 21:59 UTC = 16:59 CDT → 5 July; 22:00 UTC = 17:00 CDT → 6 July.
        _, t = run(d, [trade("2023-07-05 21:59"), trade("2023-07-05 22:00", seq=1)])
        assert [str(x.date()) for x in t["session_date"]] == ["2023-07-05", "2023-07-06"]


def test_holiday_evening_rolls_to_the_next_settlement_day():
    with tempfile.TemporaryDirectory() as d:
        # 3 July 23:00 UTC = 18:00 CDT → clock says 4 July, a holiday → 5 July.
        r, t = run(d, [trade("2023-07-03 15:00"), trade("2023-07-03 23:00", seq=1)])
        assert [str(x.date()) for x in t["session_date"]] == ["2023-07-03", "2023-07-05"]
        assert r["sessions"] == 2


def test_rows_after_the_last_session_are_dropped_and_counted():
    with tempfile.TemporaryDirectory() as d:
        r, t = run(d, [trade("2023-07-06 15:00"), trade("2023-07-06 22:30", seq=1)])
        assert len(t) == 1 and r["removed"]["partial_session_outside_window"] == 1


def test_non_trades_and_impossible_rows_are_removed_and_counted():
    with tempfile.TemporaryDirectory() as d:
        rows = [trade("2023-07-05 14:00"), trade("2023-07-05 14:01", action="A", seq=1),
                trade("2023-07-05 14:02", price=0, seq=2),
                trade("2023-07-05 14:03", size=0, seq=3),
                trade("2023-07-05 14:04", price=float("nan"), seq=4)]
        r, t = run(d, rows)
        assert len(t) == 1
        assert r["removed"]["non_trade_messages"] == 1
        assert r["removed"]["impossible_rows"] == 3
        assert r["rows_in"] == 5 and r["rows_out"] == 1


def test_multi_fill_packets_and_identical_rows_are_kept():
    with tempfile.TemporaryDirectory() as d:
        # One order filling at three prices: same ts_event and sequence.
        rows = [trade("2023-07-05 14:00", price=p, seq=7) for p in (70.0, 70.01, 70.02)]
        rows.append(trade("2023-07-05 14:00", price=70.02, seq=7))   # identical to the last
        r, t = run(d, rows)
        assert len(t) == 4 and r["removed"]["duplicates"] == 0
        assert r["kept_on_purpose"]["rows_sharing_ts_event_and_sequence_kept"] == 3
        assert r["kept_on_purpose"]["rows_identical_in_every_field_kept"] == 1


def test_sorted_by_ts_event_and_out_of_order_counted():
    with tempfile.TemporaryDirectory() as d:
        rows = [trade("2023-07-05 14:00:02", seq=2), trade("2023-07-05 14:00:01", seq=1)]
        r, t = run(d, rows)
        assert t["ts_event"].is_monotonic_increasing
        assert r["rows_out_of_ts_event_order_in_input"] == 1


def test_contract_switch_is_recorded_not_adjusted():
    with tempfile.TemporaryDirectory() as d:
        rows = [trade("2023-07-05 14:00", price=70.0, iid=1),
                trade("2023-07-06 14:00", price=71.4, iid=2, seq=1)]
        r, t = run(d, rows)
        assert list(t["price"]) == [70.0, 71.4]
        (sw,) = r["contract_switches"]
        assert (sw["from"], sw["to"], sw["session_date"]) == (1, 2, "2023-07-06")
        assert abs(sw["price_gap_pct"] - 2.0) < 1e-9


def test_isolated_bad_print_is_flagged_but_a_real_jump_is_not():
    with tempfile.TemporaryDirectory() as d:
        base = pd.Timestamp("2023-07-05 14:00", tz=UTC)
        px = [70.0] * 15 + [75.0] + [70.0] * 15 + [72.0] * 15   # spike, then a step
        rows = [trade(str(base + pd.Timedelta(seconds=i)), price=p, seq=i)
                for i, p in enumerate(px)]
        r, t = run(d, rows)
        assert list(np.flatnonzero(t["outlier_flag"])) == [15]
        assert r["outliers_flagged"] == 1 and len(t) == len(px)


def test_types_match_the_standard():
    with tempfile.TemporaryDirectory() as d:
        _, t = run(d, [trade("2023-07-05 14:00")])
        assert str(t["ts_event"].dtype) == "datetime64[ns, UTC]"
        assert str(t["ts_recv"].dtype) == "datetime64[ns, UTC]"
        assert t["price"].dtype == "float64" and t["size"].dtype == "float64"
        assert t["instrument_id"].dtype == "int32"
        assert str(t["side"].dtype).startswith("string") and str(t["symbol"].dtype).startswith("string")
        assert str(t["session_date"].dtype) == "datetime64[ns]"


def test_zero_row_sessions_are_listed():
    with tempfile.TemporaryDirectory() as d:
        r, _ = run(d, [trade("2023-07-03 14:00"), trade("2023-07-06 14:00", seq=1)])
        assert r["sessions_with_zero_rows"] == ["2023-07-05"]


def test_batches_splitting_a_session_give_the_same_output():
    with tempfile.TemporaryDirectory() as d:
        base = pd.Timestamp("2023-07-03 14:00", tz=UTC)
        rows = [trade(str(base + pd.Timedelta(hours=h)), price=70 + h / 100, seq=h)
                for h in range(0, 60, 3)]
        _, a = run(f"{d}", rows)
        old = C._BATCH_ROWS
        C._BATCH_ROWS = 3
        try:
            os.makedirs(f"{d}/b")
            _, b = run(f"{d}/b", rows)
        finally:
            C._BATCH_ROWS = old
        pd.testing.assert_frame_equal(a, b)


def test_bars_and_statistics_get_session_dates():
    with tempfile.TemporaryDirectory() as d:
        run(d, [trade("2023-07-05 14:00")])
        b = pd.read_parquet(f"{d}/out/CL/ohlcv1m.parquet")
        s = pd.read_parquet(f"{d}/out/CL/statistics.parquet")
        assert str(b["session_date"].iloc[0].date()) == "2023-07-03"
        assert "session_date" in s and "ts_ref" in s


def test_undefined_integer_in_definitions_becomes_missing():
    with tempfile.TemporaryDirectory() as d:
        run(d, [trade("2023-07-05 14:00")])
        df = pd.read_parquet(f"{d}/out/CL/definitions.parquet")
        assert df["contract_multiplier"].isna().all()
        assert str(df["expiration"].dtype) == "datetime64[ns, UTC]"


def test_existing_output_needs_force():
    with tempfile.TemporaryDirectory() as d:
        i, o = tree(d, [trade("2023-07-05 14:00")])
        clean("CL", i, o, "t", log=_quiet)
        _expect_error(lambda: clean("CL", i, o, "t", log=_quiet), "--force")
        clean("CL", i, o, "t", force=True, log=_quiet)


def test_report_has_every_field_the_standard_asks_for():
    with tempfile.TemporaryDirectory() as d:
        r, _ = run(d, [trade("2023-07-05 14:00")])
        for k in ("symbol", "series", "roll_convention", "source_files", "cleaned_by",
                  "cleaned_on", "date_range", "rows_in", "rows_out", "removed", "sessions",
                  "sessions_with_zero_rows", "contract_switches", "side_unknown_pct",
                  "outliers_flagged", "notes"):
            assert k in r, k
        with open(f"{d}/out/CL/cleaning_report.json") as fh:
            assert json.load(fh)["rows_out"] == 1


if __name__ == "__main__":
    print("clean tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all tests passed")
