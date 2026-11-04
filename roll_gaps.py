"""
Contract-switch gaps — take them out of every price change, forward only.

A vendor's continuous futures series (``CL.v.0``) switches from one contract
to the next without adjusting prices, so every switch is a price move nobody
traded. This module measures each gap — the new contract's first price over
the old contract's last — and divides it out of every later price, so that
returns are each contract's own moves chained together. See
``docs/architecture_decisions.md`` entry 8 for why this and not dropping the
returns that span a switch.

Two rules:

* **Forward only.** A price is adjusted by the switches *before* it, never
  after, so no adjusted level depends on a roll that had not happened yet.
  (Classic back-adjustment rewrites history with future rolls: harmless to
  returns, a lookahead for anything built from price levels.)
* **Returns only.** Adjusted prices are for price *changes*. Anything sized
  by the level of price — dollar-bar notional, cost per trade, position
  size — keeps the actual traded price.

Every row of a contract keeps its own instrument_id; a switch is any row
whose instrument_id differs from the row before (rows in time order).
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd

OHLC = ("open", "high", "low", "close")


def switch_rows(instrument_ids) -> np.ndarray:
    """Row positions where the contract changes (the first row of each new
    contract)."""
    ids = np.asarray(instrument_ids)
    return np.flatnonzero(ids[1:] != ids[:-1]) + 1


def cumulative_log_gap(instrument_ids, price_before, price_after) -> np.ndarray:
    """For every row, the sum of the log gaps of all switches at or before
    it. A switch at row k has gap log(price_after[k]) - log(price_before[k-1]):
    for trades both are the trade price; for bars, the new bar's open over
    the old bar's close."""
    ids = np.asarray(instrument_ids)
    before = np.asarray(price_before, dtype=float)
    after = np.asarray(price_after, dtype=float)
    out = np.zeros(len(ids))
    k = switch_rows(ids)
    if len(k):
        gaps = np.log(after[k]) - np.log(before[k - 1])
        if not np.all(np.isfinite(gaps)):
            raise ValueError("non-finite price at a contract switch")
        step = np.zeros(len(ids))
        step[k] = gaps
        out = np.cumsum(step)
    return out


def switch_table(instrument_ids, price_before, price_after, ts=None) -> pd.DataFrame:
    """One row per switch: position, from/to instrument, prices either side,
    and the gap removed."""
    ids = np.asarray(instrument_ids)
    before = np.asarray(price_before, dtype=float)
    after = np.asarray(price_after, dtype=float)
    k = switch_rows(ids)
    df = pd.DataFrame({"row": k, "from": ids[k - 1], "to": ids[k],
                       "last_price_before": before[k - 1],
                       "first_price_after": after[k],
                       "log_gap": np.log(after[k]) - np.log(before[k - 1])})
    if ts is not None:
        df.insert(1, "ts", np.asarray(ts)[k])
    return df


def adjust_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Add ``price_adj`` in place: trade price with every earlier switch gap
    divided out. ``price`` is left as traded. In place, because the tape is
    tens of millions of rows and a copy would double its memory."""
    cum = cumulative_log_gap(trades["instrument_id"].to_numpy(),
                             trades["price"].to_numpy(), trades["price"].to_numpy())
    trades["price_adj"] = trades["price"].to_numpy(dtype=float) * np.exp(-cum)
    return trades


def adjust_ohlcv(bars: pd.DataFrame, columns: Sequence[str] = OHLC) -> pd.DataFrame:
    """Divide every earlier switch gap out of open/high/low/close. The gap at
    a switch is the new contract's first open over the old one's last close.
    Raw values are kept as ``<col>_raw``."""
    cum = cumulative_log_gap(bars["instrument_id"].to_numpy(),
                             bars["close"].to_numpy(), bars["open"].to_numpy())
    out = bars.copy()
    factor = np.exp(-cum)
    for c in columns:
        out[f"{c}_raw"] = out[c]
        out[c] = out[c].to_numpy(dtype=float) * factor
    return out
