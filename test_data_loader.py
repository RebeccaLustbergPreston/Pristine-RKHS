"""
Tests for data_loader.py's cleaned source — data/cleaned-master/<SYMBOL>/.

Run with: python test_data_loader.py   (no market data required)
"""

import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

from data_loader import DataLoader

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _t(s):
    return pd.Timestamp(s, tz="UTC")


# Real definitions values for the eight contracts (min_price_increment,
# unit_of_measure_qty, and the misleading min_price_increment_amount).
DEFS = {"CL": (0.01, 1000.0, 0.1), "NG": (0.001, 10000.0, 0.01),
        "HG": (0.0005, 25000.0, 0.00125), "GC": (0.1, 100.0, 1.0),
        "ZC": (0.25, 5000.0, 1.0), "ZT": (0.00390625, 200000.0, 0.0002),
        "ES": (0.25, 50.0, 0.125), "BTC": (5.0, 5.0, 25.0)}
TICK_VALUE = {"CL": 10.0, "NG": 10.0, "HG": 12.5, "GC": 10.0, "ZC": 12.5,
              "ZT": 7.8125, "ES": 12.5, "BTC": 25.0}


def cleaned_tree(d, sym="CL"):
    out = f"{d}/cleaned-master/{sym}"
    os.makedirs(out)
    tick, uom, amt = DEFS[sym]
    pd.DataFrame({"instrument_id": np.int32([1, 2, 9]),
                  "raw_symbol": [f"{sym}Q3", f"{sym}U3", f"{sym}Q3-{sym}U3"],
                  "min_price_increment": tick, "unit_of_measure_qty": uom,
                  "min_price_increment_amount": amt,
                  "expiration": [_t("2023-07-20")] * 3}).to_parquet(f"{out}/definitions.parquet")
    pd.DataFrame({"ts_event": [_t("2023-07-03 14:00"), _t("2023-07-05 23:00"),
                               _t("2023-07-06 14:00")],
                  "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0],
                  "close": [1.0, 2.0, 3.0], "volume": [1, 1, 1],
                  "instrument_id": np.int32(1), "symbol": f"{sym}.v.0",
                  "session_date": pd.to_datetime(["2023-07-03", "2023-07-06", "2023-07-06"])}
                 ).to_parquet(f"{out}/ohlcv1m.parquet")
    pd.DataFrame({"ts_event": [_t("2023-07-03 14:00")], "ts_recv": [_t("2023-07-03 14:00")],
                  "price": [70.0], "size": [1.0], "side": ["B"], "instrument_id": np.int32(1),
                  "symbol": [f"{sym}.v.0"], "session_date": pd.to_datetime(["2023-07-03"]),
                  "outlier_flag": [False], "flags": np.uint8(0)}
                 ).to_parquet(f"{out}/trades.parquet")
    with open(f"{out}/cleaning_report.json", "w") as fh:
        json.dump({"contract_switches": [{"session_date": "2023-07-05",
                                          "ts_event": "2023-07-05T14:00:00+00:00",
                                          "from": 1, "to": 2, "price_gap_pct": 1.2}]}, fh)
    return out


def test_tick_values_are_computed_not_read_from_the_misleading_field():
    for sym, want in TICK_VALUE.items():
        with tempfile.TemporaryDirectory() as d:
            cleaned_tree(d, sym)
            dl = DataLoader(d, sym, source="cleaned")
            assert abs(dl.tick_value() - want) < 1e-9, (sym, dl.tick_value())


def test_tick_comes_from_the_contract_held_last_not_a_spread():
    with tempfile.TemporaryDirectory() as d:
        cleaned_tree(d)
        assert DataLoader(d, "CL", source="cleaned")._front_definition()["raw_symbol"] == "CLU3"


def test_cost_per_trade_is_a_fraction_of_price():
    with tempfile.TemporaryDirectory() as d:
        cleaned_tree(d)
        assert abs(DataLoader(d, "CL", source="cleaned").cost_per_trade(70.0) - 0.01 / 70) < 1e-12


def test_daily_bars_group_by_exchange_session():
    with tempfile.TemporaryDirectory() as d:
        cleaned_tree(d)
        daily = DataLoader(d, "CL", source="cleaned").build_daily_ohlcv()
        # The 5 July 23:00 UTC bar belongs to the 6 July session.
        assert [str(x.date()) for x in daily.index] == ["2023-07-03", "2023-07-06"]
        assert list(daily["close"]) == [1.0, 3.0] and list(daily["open"]) == [1.0, 2.0]


def test_ohlcv_is_indexed_by_ts_event_like_the_legacy_file():
    with tempfile.TemporaryDirectory() as d:
        cleaned_tree(d)
        o = DataLoader(d, "CL", source="cleaned").ohlcv()
        assert o.index.name == "ts_event" and "ts_event" not in o.columns


def test_trades_read_only_the_columns_the_kernels_use():
    with tempfile.TemporaryDirectory() as d:
        cleaned_tree(d)
        t = DataLoader(d, "CL", source="cleaned").trades()
        assert "flags" not in t.columns and "session_date" in t.columns


def test_contract_switches_come_from_the_report():
    with tempfile.TemporaryDirectory() as d:
        cleaned_tree(d)
        sw = DataLoader(d, "CL", source="cleaned").contract_switches()
        assert list(sw[["from", "to"]].iloc[0]) == [1, 2]
        assert str(sw["ts_event"].dtype) == "datetime64[ns, UTC]"


def test_legacy_source_is_cl_only_and_keeps_its_paths():
    dl = DataLoader("data", "CL")
    assert dl.trades_path.endswith("trades_CL_full.parquet")
    try:
        DataLoader("data", "NG")
    except ValueError:
        return
    raise AssertionError("legacy NG should be refused")


if __name__ == "__main__":
    print("data_loader tests\n")
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            check(_name, _fn)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all tests passed")
