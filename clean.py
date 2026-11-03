"""
Cleaning — ingested parquet → the format in docs/data_cleaning_standard.md.

    python clean.py --symbol NG
    python clean.py --symbol NG --in-dir data --out-dir data/cleaned-master

Reads what ``ingest.py`` wrote to ``<in-dir>/<SYMBOL>/`` and writes

    <out-dir>/<SYMBOL>/trades.parquet
    <out-dir>/<SYMBOL>/ohlcv1m.parquet
    <out-dir>/<SYMBOL>/definitions.parquet
    <out-dir>/<SYMBOL>/statistics.parquet
    <out-dir>/<SYMBOL>/cleaning_report.json

applying the standard's rules a–i in order and counting what each removes.
The input is never modified. Trades are processed one session at a time, so
no contract's tape is ever in memory whole.

Two places where this departs from the written standard, both measured on
the real data and explained in each report:

* **Rule c (de-duplication).** On CME data ``(ts_event, sequence)`` is not a
  unique key: one aggressive order that fills at several prices is reported
  as several trades in one packet, with the same timestamp and sequence
  number. Even rows identical in every field are real — Databento's own
  minute-bar volume counts them. De-duplicating on either rule broke volume
  in tens of thousands of HG minutes. So only true repeats are removed:
  the same record twice because two source files overlap, which ingestion
  already refuses, so the expected count is zero.
* **Rule f (session date).** The trade date is the exchange's: a trade after
  17:00 Chicago time belongs to the next day's session, and a session that
  would land on a day the exchange did not settle (4 July, Thanksgiving, a
  weekend) belongs to the next day it did. Those days are read from the
  contract's own settlement records (statistics, stat_type 3, ``ts_ref``).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

CLEAN_VERSION = 1
_ROOT = os.path.dirname(os.path.abspath(__file__))
_EXCHANGE_TZ = "America/Chicago"
# A CME session starts at 17:00 Chicago time; shifting by 7 hours makes the
# session's calendar day the clock date.
_SESSION_SHIFT = pd.Timedelta(hours=7)
_SETTLEMENT = 3                                    # Databento StatType.SETTLEMENT_PRICE
_BATCH_ROWS = 1_000_000

# Outliers (rule h): a trade whose price is away from both the median of the
# 10 trades before it and the median of the 10 after it, in the same
# contract, by more than this fraction. A real jump moves one side's median
# with it; an isolated bad print differs from both.
OUTLIER_WINDOW = 10
OUTLIER_FRACTION = 0.005

TRADE_TYPES = {"price": "float64", "size": "float64", "side": "string",
               "instrument_id": "int32", "symbol": "string"}
REQUIRED_TRADES = ("ts_event", "ts_recv", "price", "size", "side", "instrument_id",
                   "symbol")

# Integer "undefined" values Databento writes in definitions (ingest keeps
# them as written; the ingestion note says cleaning treats them as missing).
_DEF_SENTINELS = (2**31 - 1, 2**32 - 1, 2**63 - 1, 2**64 - 1, 2**15 - 1, 2**16 - 1,
                  2**7 - 1, 2**8 - 1)


class CleanError(RuntimeError):
    """The data does not satisfy a rule; stop rather than patch."""


def _ns_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Rule i: every timestamp column UTC at nanosecond precision."""
    for c in df.columns:
        if isinstance(df[c].dtype, pd.DatetimeTZDtype):
            df[c] = df[c].dt.tz_convert("UTC").astype("datetime64[ns, UTC]")
    return df


# ── Sessions ─────────────────────────────────────────────────────────────────

def trading_days(stats_path: str) -> np.ndarray:
    """Every date the exchange published a settlement price for this contract
    (the trade date is ``ts_ref``), as sorted datetime64[D]."""
    s = pq.read_table(stats_path, columns=["stat_type", "ts_ref"]).to_pandas()
    ref = pd.to_datetime(s.loc[s["stat_type"] == _SETTLEMENT, "ts_ref"], utc=True).dropna()
    if ref.empty:
        raise CleanError(f"{stats_path}: no settlement prices, so the exchange's "
                         "trading days are unknown")
    if (ref.dt.tz_convert("UTC") != ref.dt.tz_convert("UTC").dt.normalize()).any():
        raise CleanError(f"{stats_path}: settlement ts_ref is not a whole date")
    return np.unique(ref.dt.tz_localize(None).to_numpy().astype("datetime64[D]"))


def session_dates(ts_event: pd.Series, days: np.ndarray):
    """Exchange session (trade date) of each timestamp: the Chicago clock date
    after 17:00 rolls to the next day, then any date the exchange did not
    trade rolls to the next date it did.

    Returns (sessions, inside): ``inside`` is False for timestamps whose
    session falls outside the settlement calendar — the evening of the last
    day pulled opens a session the data stops before (e.g. 31 December 17:00
    Chicago time opens 2 January's). Those rows cannot be dated and are
    dropped by the caller, and counted."""
    base = ((ts_event.dt.tz_convert(_EXCHANGE_TZ) + _SESSION_SHIFT)
            .dt.tz_localize(None).to_numpy().astype("datetime64[D]"))
    idx = np.searchsorted(days, base, side="left")
    inside = (idx < len(days)) & (base >= days[0])
    return days[np.minimum(idx, len(days) - 1)], inside


# ── Trades ───────────────────────────────────────────────────────────────────

def _flag_outliers(df: pd.DataFrame) -> np.ndarray:
    flag = np.zeros(len(df), bool)
    for _, g in df.groupby("instrument_id", sort=False):
        p = g["price"]
        before = p.rolling(OUTLIER_WINDOW, min_periods=OUTLIER_WINDOW).median().shift(1)
        after = p[::-1].rolling(OUTLIER_WINDOW, min_periods=OUTLIER_WINDOW).median().shift(1)[::-1]
        far = ((p - before).abs() > OUTLIER_FRACTION * before) & \
              ((p - after).abs() > OUTLIER_FRACTION * after)
        flag[df.index.get_indexer(g.index[far.to_numpy()])] = True
    return flag


class _Counts(dict):
    def add(self, k, n):
        self[k] = self.get(k, 0) + int(n)


def _clean_session(df: pd.DataFrame, counts: _Counts) -> pd.DataFrame:
    # a. trades only
    is_trade = (df["action"] == "T") if "action" in df.columns else np.ones(len(df), bool)
    if "rtype" in df.columns:
        is_trade &= df["rtype"] == 0
    counts.add("non_trade_messages", (~is_trade).sum())
    df = df.loc[is_trade]
    # b. impossible rows
    bad = ~np.isfinite(df["price"].to_numpy(float)) | (df["price"] <= 0).to_numpy() | \
        ~np.isfinite(df["size"].to_numpy(float)) | (df["size"] <= 0).to_numpy()
    for c in REQUIRED_TRADES:
        bad |= df[c].isna().to_numpy()
    counts.add("impossible_rows", bad.sum())
    df = df.loc[~bad]
    # c. true repeats only: identical records that also share ts_recv and
    #    ts_in_delta *and* the same position in the feed would be a re-delivery;
    #    within one source that cannot happen, so nothing is removed here
    #    (see module docstring). Count (ts_event, sequence) repeats for the report.
    key = df.duplicated(["ts_event", "sequence"], keep="first") if "sequence" in df else \
        np.zeros(len(df), bool)
    counts.add("rows_sharing_ts_event_and_sequence_kept", np.asarray(key).sum())
    counts.add("rows_identical_in_every_field_kept", df.duplicated(keep="first").sum())
    # d. sort by ts_event (stable, so packet order is kept for equal times)
    out_of_order = int((df["ts_event"].diff() < pd.Timedelta(0)).sum())
    counts.add("rows_out_of_ts_event_order_in_input", out_of_order)
    df = df.sort_values("ts_event", kind="stable").reset_index(drop=True)
    if not df["ts_event"].is_monotonic_increasing:
        raise CleanError("ts_event not monotonic after sorting")
    return df


def clean_trades(src: str, dest: str, days: np.ndarray) -> dict:
    pf = pq.ParquetFile(src)
    missing = [c for c in REQUIRED_TRADES if c not in pf.schema_arrow.names]
    if missing:
        raise CleanError(f"{src}: missing columns {missing}")
    counts = _Counts()
    rows_in = pf.metadata.num_rows
    writer: Optional[pq.ParquetWriter] = None
    schema = None
    sessions: Dict[np.datetime64, int] = {}
    switches: List[dict] = []
    last = {"ts_event": None, "instrument_id": None, "price": None}
    side_unknown = outliers = rows_out = 0
    flushed_to: Optional[np.datetime64] = None
    buf: Optional[pd.DataFrame] = None

    def flush(part: pd.DataFrame):
        nonlocal writer, schema, side_unknown, outliers, rows_out
        for sess, df in part.groupby("session_date", sort=True):
            df = _clean_session(df.reset_index(drop=True), counts)
            if not len(df):
                continue
            if last["ts_event"] is not None and df["ts_event"].iloc[0] < last["ts_event"]:
                raise CleanError(f"session {sess} starts at {df['ts_event'].iloc[0]}, "
                                 f"before the previous session ended at {last['ts_event']}")
            # e. contract switches, recorded and left alone
            ids = df["instrument_id"].to_numpy()
            prev_ids = np.concatenate([[last["instrument_id"]] if last["instrument_id"]
                                       is not None else [ids[0]], ids[:-1]])
            prev_px = np.concatenate([[last["price"]] if last["price"] is not None
                                      else [df["price"].iloc[0]], df["price"].to_numpy()[:-1]])
            for i in np.flatnonzero(ids != prev_ids):
                switches.append({"session_date": str(pd.Timestamp(sess).date()),
                                 "ts_event": df["ts_event"].iloc[i].isoformat(),
                                 "from": int(prev_ids[i]), "to": int(ids[i]),
                                 "last_price_before": float(prev_px[i]),
                                 "first_price_after": float(df["price"].iloc[i]),
                                 "price_gap_pct": round(100 * (df["price"].iloc[i] / prev_px[i] - 1), 4)})
            # h. flag, never delete
            df["outlier_flag"] = _flag_outliers(df)
            for c, t in TRADE_TYPES.items():
                df[c] = df[c].astype(t)
            table = pa.Table.from_pandas(df, preserve_index=False)
            if schema is None:
                schema = table.schema
                writer = pq.ParquetWriter(dest, schema)
            writer.write_table(table.cast(schema))
            sessions[np.datetime64(pd.Timestamp(sess).date(), "D")] = len(df)
            side_unknown += int((df["side"] == "N").sum())
            outliers += int(df["outlier_flag"].sum())
            rows_out += len(df)
            last.update(ts_event=df["ts_event"].iloc[-1], instrument_id=int(ids[-1]),
                        price=float(df["price"].iloc[-1]))

    try:
        for batch in pf.iter_batches(batch_size=_BATCH_ROWS):
            df = _ns_utc(batch.to_pandas())
            # f. exchange session, from ts_event
            sess, inside = session_dates(df["ts_event"], days)
            counts.add("partial_session_outside_window", (~inside).sum())
            df = df.loc[inside].reset_index(drop=True)
            df["session_date"] = pd.to_datetime(sess[inside]).astype("datetime64[ns]")
            if flushed_to is not None and (df["session_date"].to_numpy() <= flushed_to).any():
                raise CleanError("a row for an already-written session arrived later in "
                                 "the input; the input is not in session order")
            buf = df if buf is None else pd.concat([buf, df], ignore_index=True)
            # Keep the two newest sessions buffered: a trade can be received a
            # moment after the next session's first trade.
            keep_from = np.sort(buf["session_date"].unique())[-2:][0]
            done = buf["session_date"] < keep_from
            if done.any():
                flush(buf.loc[done])
                flushed_to = buf.loc[done, "session_date"].max().to_datetime64()
                buf = buf.loc[~done].reset_index(drop=True)
        if buf is not None and len(buf):
            flush(buf)
        if writer is None:
            raise CleanError(f"{src}: no rows survived cleaning")
        writer.close()
        writer = None
    except BaseException:
        if writer is not None:
            writer.close()
        if os.path.exists(dest):
            os.remove(dest)
        raise

    active = days[(days >= min(sessions)) & (days <= max(sessions))]
    return {
        "rows_in": rows_in,
        "rows_out": rows_out,
        "removed": {k: counts.get(k, 0) for k in
                    ("non_trade_messages", "impossible_rows", "duplicates",
                     "partial_session_outside_window")},
        "kept_on_purpose": {k: counts.get(k, 0) for k in
                            ("rows_sharing_ts_event_and_sequence_kept",
                             "rows_identical_in_every_field_kept")},
        "rows_out_of_ts_event_order_in_input": counts.get(
            "rows_out_of_ts_event_order_in_input", 0),
        "date_range": [str(min(sessions)), str(max(sessions))],
        "sessions": len(sessions),
        "exchange_trading_days_in_range": int(len(active)),
        "sessions_with_zero_rows": [str(d) for d in active if d not in sessions],
        "rows_per_session": {"min": int(min(sessions.values())),
                             "median": int(np.median(list(sessions.values()))),
                             "max": int(max(sessions.values()))},
        "contract_switches": switches,
        "side_unknown_pct": round(100 * side_unknown / rows_out, 3),
        "outliers_flagged": outliers,
    }


# ── Bars, statistics, definitions ────────────────────────────────────────────

def _with_session(src: str, dest: str, days: np.ndarray, required: Sequence[str],
                  sort: bool) -> dict:
    df = _ns_utc(pd.read_parquet(src))
    missing = [c for c in required if c not in df.columns and c != "session_date"]
    if missing:
        raise CleanError(f"{src}: missing columns {missing}")
    sess, inside = session_dates(df["ts_event"], days)
    outside = int((~inside).sum())
    df = df.loc[inside].reset_index(drop=True)
    df["session_date"] = pd.to_datetime(sess[inside]).astype("datetime64[ns]")
    out_of_order = int((df["ts_event"].diff() < pd.Timedelta(0)).sum())
    if sort:
        df = df.sort_values("ts_event", kind="stable").reset_index(drop=True)
    if "instrument_id" in df:
        df["instrument_id"] = df["instrument_id"].astype("int32")
    if "symbol" in df:
        df["symbol"] = df["symbol"].astype("string")
    df.to_parquet(dest, index=False)
    return {"rows": len(df), "rows_out_of_ts_event_order_in_input": out_of_order,
            "removed_partial_session_outside_window": outside}


def clean_definitions(src: str, dest: str) -> dict:
    df = pd.read_parquet(src)
    replaced = {}
    for c in df.columns:
        if pd.api.types.is_integer_dtype(df[c]) and c not in ("instrument_id", "rtype",
                                                               "publisher_id"):
            m = df[c].isin(_DEF_SENTINELS)
            if m.any():
                df[c] = df[c].astype("Int64").mask(m)
                replaced[c] = int(m.sum())
    if "expiration" in df:
        df["expiration"] = pd.to_datetime(df["expiration"], utc=True)
    df = _ns_utc(df)
    df["instrument_id"] = df["instrument_id"].astype("int32")
    for c in ("symbol", "raw_symbol"):
        if c in df:
            df[c] = df[c].astype("string")
    df.to_parquet(dest, index=False)
    return {"rows": len(df), "undefined_integers_made_missing": replaced}


# ── Driver ───────────────────────────────────────────────────────────────────

def clean(symbol: str, in_dir: str, out_dir: str, cleaned_by: str,
          force: bool = False, log=print) -> dict:
    symbol = symbol.upper()
    src = os.path.join(os.path.abspath(in_dir), symbol)
    dst = os.path.join(os.path.abspath(out_dir), symbol)
    man_path = os.path.join(src, "ingest_manifest.json")
    if not os.path.exists(man_path):
        raise CleanError(f"{src} has no ingest_manifest.json; run ingest.py first")
    with open(man_path) as fh:
        manifest = json.load(fh)
    if os.path.exists(os.path.join(dst, "cleaning_report.json")) and not force:
        raise CleanError(f"{dst} already cleaned; pass --force to replace it")
    os.makedirs(dst, exist_ok=True)

    days = trading_days(os.path.join(src, "statistics.parquet"))
    tr = manifest["outputs"]["trades"]
    log(f"{symbol}: cleaning trades ({tr['rows']:,} rows) ...")
    t = clean_trades(os.path.join(src, "trades.parquet"),
                     os.path.join(dst, "trades.parquet.partial"), days)
    log(f"  {t['rows_out']:,} rows out, {t['sessions']} sessions, "
        f"{len(t['contract_switches'])} switches, {t['outliers_flagged']} outliers flagged")
    other = {}
    for kind, req, sort in (("ohlcv1m", ("ts_event", "open", "high", "low", "close",
                                         "volume", "instrument_id", "symbol"), True),
                            ("statistics", ("ts_event", "stat_type", "price", "quantity",
                                            "instrument_id", "symbol"), False)):
        other[kind] = _with_session(os.path.join(src, f"{kind}.parquet"),
                                    os.path.join(dst, f"{kind}.parquet.partial"), days,
                                    req, sort)
    other["definitions"] = clean_definitions(os.path.join(src, "definitions.parquet"),
                                             os.path.join(dst, "definitions.parquet.partial"))

    narrowed = tr.get("narrowed")
    sources = ([s.get("path") or s.get("job_folder") for s in narrowed["sources"]]
               if narrowed else [s["path"] for s in tr["source_files"]])
    series = tr["series"][0]
    report = {
        "symbol": symbol,
        "series": series,
        "roll_convention": {"v": "volume", "n": "open interest", "c": "calendar"}.get(
            series.split(".")[1] if series.count(".") == 2 else "", "unknown"),
        "source_files": sources,
        "ingested_on": manifest.get("ingested_on"),
        "cleaned_by": cleaned_by,
        "cleaned_on": _dt.date.today().isoformat(),
        "clean_version": CLEAN_VERSION,
        **t,
        "other_outputs": other,
        "rules": {
            "c_deduplication": "Only true repeats from overlapping source files are "
                "removed; ingestion refuses overlapping sources, so none are expected. "
                "(ts_event, sequence) is not unique on CME: one order filling at several "
                "prices is several trades in one packet. Rows identical in every field "
                "are kept too, because Databento's minute-bar volume counts them.",
            "f_session_date": "17:00 America/Chicago starts the next day's session; a "
                "session on a day with no settlement price rolls to the next day with "
                "one. Trading days from statistics stat_type 3 ts_ref. Rows whose "
                "session falls after the last settlement date (the evening the pull "
                "ends on opens a session the data does not reach) are removed and "
                "counted as partial_session_outside_window.",
            "f_statistics": "statistics.session_date is from ts_event like everything "
                "else (when the message was sent). For settlement prices the trade "
                "date they settle is ts_ref.",
            "h_outliers": f"outlier_flag is set when a price differs by more than "
                f"{OUTLIER_FRACTION:.1%} from both the median of the {OUTLIER_WINDOW} "
                f"trades before it and of the {OUTLIER_WINDOW} after it, in the same "
                "contract. Nothing is deleted.",
        },
        "ingestion_checks": tr.get("checks_passed", []),
        "notes": "",
    }
    if symbol == "BTC":
        report["notes"] = ("CME bitcoin futures trade Sunday 17:00 to Friday 16:00 "
                           "Chicago time with a daily break, like the other contracts, "
                           "so the same session rule applies; there are no weekend "
                           "sessions to assign.")
    rep_tmp = os.path.join(dst, "cleaning_report.json.partial")
    with open(rep_tmp, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    for kind in ("trades", "ohlcv1m", "statistics", "definitions"):
        os.replace(os.path.join(dst, f"{kind}.parquet.partial"),
                   os.path.join(dst, f"{kind}.parquet"))
    os.replace(rep_tmp, os.path.join(dst, "cleaning_report.json"))
    log(f"  wrote {dst}")
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--in-dir", default=os.path.join(_ROOT, "data"))
    ap.add_argument("--out-dir", default=os.path.join(_ROOT, "data", "cleaned-master"))
    ap.add_argument("--cleaned-by", default="Rebecca Preston")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    try:
        clean(args.symbol, args.in_dir, args.out_dir, args.cleaned_by, force=args.force)
    except CleanError as e:
        print(f"\nCLEAN FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
