"""
Tests for roll_gaps.py and its use in the loader and the master schedule.

Run with: python test_roll_gaps.py   (no market data required)

The acceptance test for D0 (FALL_2026_TASKS.md) is
test_no_return_contains_a_switch_gap: a series that is flat within each
contract but jumps 5% at every switch must have every return equal to zero
after adjustment.
"""

import sys

import numpy as np
import pandas as pd

from roll_gaps import (adjust_ohlcv, adjust_trades, cumulative_log_gap, switch_rows,
                       switch_table)

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _trades(ids, prices):
    return pd.DataFrame({"instrument_id": np.array(ids, dtype=np.int32),
                         "price": np.array(prices, dtype=float),
                         "size": 1.0})


def test_no_return_contains_a_switch_gap():
    # Flat within each contract; each new contract trades 5% above the last.
    ids = [1] * 5 + [2] * 5 + [3] * 5
    px = [70.0] * 5 + [73.5] * 5 + [77.175] * 5
    t = adjust_trades(_trades(ids, px))
    r = np.diff(np.log(t["price_adj"].to_numpy()))
    assert np.allclose(r, 0.0), r


def test_moves_within_a_contract_are_kept_exactly():
    ids = [1, 1, 1, 2, 2, 2]
    px = [70.0, 70.7, 70.0, 75.0, 75.75, 75.0]          # ±1% inside each contract
    t = adjust_trades(_trades(ids, px))
    raw = np.diff(np.log(px))
    adj = np.diff(np.log(t["price_adj"].to_numpy()))
    keep = np.array([True, True, False, True, True])     # the switch return excepted
    assert np.allclose(adj[keep], raw[keep])
    assert abs(adj[2]) < 1e-12                          # 70.0 → 75.0 was the switch


def test_adjustment_is_forward_only():
    # Prices before the first switch are untouched: no adjusted level
    # depends on a roll that had not happened yet.
    ids = [1, 1, 2, 2]
    t = adjust_trades(_trades(ids, [70.0, 71.0, 80.0, 81.0]))
    assert list(t["price_adj"].iloc[:2]) == [70.0, 71.0]
    # And truncating the tape before the switch gives the same prefix.
    t2 = adjust_trades(_trades(ids[:2], [70.0, 71.0]))
    assert list(t2["price_adj"]) == list(t["price_adj"].iloc[:2])


def test_traded_price_is_left_alone():
    t = adjust_trades(_trades([1, 2], [70.0, 80.0]))
    assert list(t["price"]) == [70.0, 80.0]


def test_switch_back_and_forth_still_chains():
    ids = [1, 2, 1]
    t = adjust_trades(_trades(ids, [70.0, 77.0, 70.0]))
    assert np.allclose(t["price_adj"], 70.0)


def test_bars_use_new_open_over_old_close():
    bars = pd.DataFrame({"instrument_id": [1, 1, 2, 2],
                         "open": [70.0, 70.0, 77.0, 77.0], "high": [70.5, 70.5, 77.5, 77.5],
                         "low": [69.5, 69.5, 76.5, 76.5], "close": [70.0, 70.0, 77.0, 77.7]})
    a = adjust_ohlcv(bars)
    assert np.allclose(a["close"].iloc[:3], 70.0)
    assert abs(a["close"].iloc[3] / a["close"].iloc[2] - 77.7 / 77.0) < 1e-12
    assert list(a["close_raw"]) == [70.0, 70.0, 77.0, 77.7]


def test_switch_table_reports_each_gap():
    sw = switch_table([1, 1, 2, 2, 3], [70.0, 70.0, 77.0, 77.0, 70.0],
                      [70.0, 70.0, 77.0, 77.0, 70.0])
    assert list(sw["row"]) == [2, 4] and list(sw["from"]) == [1, 2]
    assert np.allclose(sw["log_gap"], [np.log(1.1), np.log(70 / 77)])


def test_no_switch_means_no_change():
    assert not len(switch_rows([5, 5, 5]))
    assert np.all(cumulative_log_gap([5, 5, 5], [1, 2, 3], [1, 2, 3]) == 0)



# ── Wired in: loader, master schedule, VPIN/Kyle ─────────────────────────────

def _cleaned_cl(d):
    import json, os
    out = f"{d}/cleaned-master/CL"
    os.makedirs(out)
    t0 = pd.Timestamp("2023-07-05 14:00", tz="UTC")
    ts = [t0 + pd.Timedelta(seconds=i) for i in range(6)]
    pd.DataFrame({"ts_event": ts, "ts_recv": ts, "price": [70.0, 70.0, 70.0, 77.0, 77.0, 77.0],
                  "size": 1.0, "side": "B", "instrument_id": np.int32([1, 1, 1, 2, 2, 2]),
                  "symbol": "CL.v.0", "session_date": pd.Timestamp("2023-07-05"),
                  "outlier_flag": False}).to_parquet(f"{out}/trades.parquet")
    pd.DataFrame({"ts_event": [t0, t0 + pd.Timedelta(minutes=1)], "open": [70.0, 77.0],
                  "high": [70.0, 77.0], "low": [70.0, 77.0], "close": [70.0, 77.0],
                  "volume": [3, 3], "instrument_id": np.int32([1, 2]), "symbol": "CL.v.0",
                  "session_date": pd.to_datetime(["2023-07-05", "2023-07-06"])}
                 ).to_parquet(f"{out}/ohlcv1m.parquet")
    with open(f"{out}/cleaning_report.json", "w") as fh:
        json.dump({"contract_switches": [{"session_date": "2023-07-05", "from": 1, "to": 2,
                                          "ts_event": str(ts[3]), "price_gap_pct": 10.0}]}, fh)


def test_loader_removes_gaps_from_trades_and_daily_bars():
    import tempfile
    from data_loader import DataLoader
    with tempfile.TemporaryDirectory() as d:
        _cleaned_cl(d)
        dl = DataLoader(d, "CL", source="cleaned", switch_gaps="remove")
        t = dl.trades()
        assert np.allclose(t["price_adj"], 70.0) and list(t["price"])[-1] == 77.0
        daily = dl.build_daily_ohlcv()
        assert np.allclose(np.diff(np.log(daily["close"])), 0.0)
        kept = DataLoader(d, "CL", source="cleaned").build_daily_ohlcv()
        assert abs(np.diff(np.log(kept["close"]))[0] - np.log(1.1)) < 1e-12


def test_loader_refuses_bars_that_switch_differently_from_the_tape():
    import json, tempfile
    from data_loader import DataLoader
    with tempfile.TemporaryDirectory() as d:
        _cleaned_cl(d)
        p = f"{d}/cleaned-master/CL/cleaning_report.json"
        with open(p, "w") as fh:
            json.dump({"contract_switches": [{"session_date": "2023-07-05", "from": 1,
                                              "to": 3, "ts_event": "2023-07-05T14:00:03+00:00",
                                              "price_gap_pct": 1}]}, fh)
        try:
            DataLoader(d, "CL", source="cleaned", switch_gaps="remove").ohlcv()
        except ValueError as e:
            assert "switch contract" in str(e)
            return
        raise AssertionError("expected a mismatch error")


def test_master_bars_sized_on_traded_price_closed_on_adjusted():
    from feature_alignment import build_master_bar_schedule
    t = _trades([1, 1, 2, 2], [70.0, 70.0, 77.0, 77.0])
    t["ts_event"] = pd.date_range("2023-07-05 14:00", periods=4, freq="s", tz="UTC")
    adjust_trades(t)
    m = build_master_bar_schedule(t, dollar_threshold=140.0)
    # Notional on traded prices: bars close after rows 1 and 2 (70+70, then 77 > 140-…).
    m_raw = build_master_bar_schedule(t.drop(columns="price_adj"), dollar_threshold=140.0)
    assert list(m["boundary"]) == list(m_raw["boundary"])
    assert np.allclose(m["close_price"], 70.0)
    assert list(m["close_price_raw"]) == list(m_raw["close_price"])


def test_vpin_and_kyle_read_moves_from_the_adjusted_price():
    from run_backtest import build_kyle_features, build_vpin_features
    n = 20000                                          # enough bins for the z-scores
    ids = np.where(np.arange(n) < 15050, 1, 2)         # switch falls mid-bar and mid-bin
    px = np.where(ids == 1, 70.0, 77.0)
    sides = np.random.default_rng(0).choice(["A", "B"], n)   # net flow must vary for Kyle
    t = pd.DataFrame({"ts_event": pd.date_range("2023-07-05", periods=n, freq="s", tz="UTC"),
                      "price": px, "size": 1.0, "side": sides, "instrument_id": ids})
    adjust_trades(t)
    f_adj = build_vpin_features(t, dollar_threshold=7_000, vpin_window=5)
    raw = t.drop(columns="price_adj")
    f_raw = build_vpin_features(raw, dollar_threshold=7_000, vpin_window=5)
    # bar_vol (column 2) jumps at the switch only when the gap is left in
    assert np.ptp(f_adj[:, 2]) < 1e-6 < np.ptp(f_raw[:, 2])
    k_adj = build_kyle_features(t)
    k_raw = build_kyle_features(raw)
    assert not np.allclose(k_adj, k_raw)

if __name__ == "__main__":
    print("roll_gaps tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all tests passed")
