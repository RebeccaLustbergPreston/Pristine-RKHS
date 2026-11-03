"""
Ingestion for Databento CSV trade downloads — called by ``ingest.py``.

Two contracts' trades (NG, HG) reached us as a Databento batch job encoded as
CSV, pulled as the *parent* symbol (``NG.FUT``): every contract month and every
calendar spread, in daily ``.trades.csv.zst`` files. Every other contract was
pulled as one continuous series (``NG.v.0``). Rather than pay to re-pull, this
module narrows the parent CSVs to the series the rest of the contract uses, so
``data/<SYMBOL>/trades.parquet`` comes out the same as if that series had been
pulled as DBN.

Which contract to keep on each date is **not estimated**. Databento records
its own roll in the header of every continuous-series file: the ``NG.v.0``
minute bars name the instrument id the series pointed at for each date. Those
mappings are read from the bar files ``ingest.py`` is already ingesting, and a
CSV row is kept only if its ``instrument_id`` is the one mapped for its date.
The date is the UTC date of ``ts_recv``, the same rule Databento's reader uses
to map symbols.

The result is checked against Databento's own bars: one-minute bars rebuilt
from the converted trades (bucketed by ``ts_recv``, as Databento builds them)
must equal the ``ohlcv-1m`` file bar for bar. On 3 months of real HG this
matched 84,524 of 84,524 bars exactly.

Like the DBN path, the CSV job is identified from its own metadata
(``metadata.json``), never from file or folder names, every file is hashed
against the job's ``manifest.json``, and anything that does not line up stops
the run instead of being patched.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ingest import (IngestError, OutputPlan, RawFile, _CHUNK_ROWS, _SCRATCH_PREFIX,
                    _ns_to_iso, _sha256, root_of)

# Databento's CSV encoding of the trades schema, in its column order — the
# same columns, in the same order, as DBNStore.to_df() gives after ts_recv
# leaves the index.
CSV_TRADE_COLUMNS = ("ts_recv", "ts_event", "rtype", "publisher_id", "instrument_id",
                     "action", "side", "depth", "price", "size", "flags",
                     "ts_in_delta", "sequence", "symbol")
_CSV_DTYPES = {"rtype": "uint8", "publisher_id": "uint16", "instrument_id": "uint32",
               "action": "str", "side": "str", "depth": "uint8", "price": "float64",
               "size": "uint32", "flags": "uint8", "ts_in_delta": "int32",
               "sequence": "uint32", "symbol": "str"}
_CSV_SUFFIXES = (".csv.zst", ".csv")


# ── Finding CSV jobs ─────────────────────────────────────────────────────────

@dataclass
class CsvJob:
    folder: str
    dataset: str
    schema: str
    stype_in: str
    symbols: Tuple[str, ...]
    start_ns: int
    end_ns: Optional[int]
    roots: Tuple[str, ...]
    files: List[str] = field(default_factory=list)
    sha256: Dict[str, str] = field(default_factory=dict)

    def describe(self, raw_dir: str) -> dict:
        return {"job_folder": os.path.relpath(self.folder, raw_dir),
                "encoding": "csv", "dataset": self.dataset, "schema": self.schema,
                "stype_in": self.stype_in, "symbols": list(self.symbols),
                "metadata_start": _ns_to_iso(self.start_ns),
                "metadata_end": _ns_to_iso(self.end_ns), "files": len(self.files)}


def find_csv_jobs(raw_dir: str) -> List[CsvJob]:
    """Every Databento batch job under ``raw_dir`` whose ``metadata.json``
    says it was encoded as CSV. What the job holds comes from that file."""
    jobs = []
    for dirpath, dirnames, files in os.walk(raw_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(_SCRATCH_PREFIX)]
        if "metadata.json" not in files:
            continue
        try:
            with open(os.path.join(dirpath, "metadata.json")) as fh:
                md = json.load(fh)
        except (OSError, ValueError):
            continue
        q = md.get("query", md) if isinstance(md, dict) else {}
        if not isinstance(q, dict) or q.get("encoding") != "csv":
            continue
        symbols = tuple(str(s) for s in q.get("symbols") or ())
        jobs.append(CsvJob(
            folder=os.path.abspath(dirpath), dataset=str(q.get("dataset")),
            schema=str(q.get("schema")), stype_in=str(q.get("stype_in")),
            symbols=symbols, start_ns=int(q["start"]),
            end_ns=None if q.get("end") is None else int(q["end"]),
            roots=tuple(sorted({r for r in map(root_of, symbols) if r})),
            files=sorted(os.path.join(dirpath, n) for n in files
                         if n.endswith(_CSV_SUFFIXES))))
    return sorted(jobs, key=lambda j: j.folder)


# ── The roll, as Databento recorded it ───────────────────────────────────────

@dataclass
class RollCalendar:
    series: str                       # e.g. "NG.v.0"
    dataset: str
    starts: np.ndarray                # datetime64[D], sorted
    ends: np.ndarray                  # datetime64[D], exclusive
    ids: np.ndarray                   # uint32 instrument id per interval
    source_files: List[str]

    def lookup(self, days: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Instrument id mapped for each UTC date, and a mask of dates the
        calendar covers at all."""
        idx = np.searchsorted(self.starts, days, side="right") - 1
        ok = idx >= 0
        safe = np.where(ok, idx, 0)
        ok &= days < self.ends[safe]
        return np.where(ok, self.ids[safe], 0).astype("uint32"), ok

    def describe(self, raw_dir: str) -> dict:
        rolls = [{"from": str(s), "instrument_id": int(i)}
                 for k, (s, i) in enumerate(zip(self.starts, self.ids))
                 if k == 0 or i != self.ids[k - 1]]
        return {"series": self.series,
                "taken_from": [os.path.relpath(p, raw_dir) for p in self.source_files],
                "covers": [str(self.starts[0]), str(self.ends[-1])],
                "contracts": rolls}


def roll_calendar(plans: Dict[str, OutputPlan], symbol: str) -> RollCalendar:
    """Databento's own date → instrument mapping for the continuous series
    this contract was pulled as, read from the headers of the files being
    ingested alongside (minute bars first)."""
    import databento as db

    order = ["ohlcv1m"] + sorted(k for k in plans if k != "ohlcv1m")
    source: Optional[List[RawFile]] = None
    for kind in order:
        files = plans[kind].files if kind in plans else []
        if files and all(f.stype_in == "continuous" for f in files):
            source = files
            break
    if source is None:
        raise IngestError(
            f"the {symbol} trades are a CSV parent pull (every contract month), and "
            f"no continuous-series file for {symbol} is here to say which contract "
            f"to keep on each date. Put the {symbol}.v.0 (or .n.0/.c.0) minute bars "
            "in the raw folder too; their headers carry Databento's roll.")
    series = [s for s in source[0].symbols if root_of(s) == symbol]
    if len(series) != 1:
        raise IngestError(f"cannot tell which continuous series to follow from "
                          f"{source[0].path}: header symbols {list(source[0].symbols)}")
    series = series[0]
    by_day: Dict[np.datetime64, int] = {}
    for f in source:
        mappings = db.DBNStore.from_file(f.path).metadata.mappings or {}
        for iv in mappings.get(series, []):
            iid = int(iv["symbol"])
            for d in np.arange(np.datetime64(iv["start_date"], "D"),
                               np.datetime64(iv["end_date"], "D")):
                if by_day.setdefault(d, iid) != iid:
                    raise IngestError(
                        f"{series}: the bar files disagree about {d} "
                        f"(instrument {by_day[d]} vs {iid}, in {f.path})")
    if not by_day:
        raise IngestError(f"no {series} mappings in the headers of "
                          f"{[f.path for f in source]}")
    days = np.array(sorted(by_day), dtype="datetime64[D]")
    ids = np.array([by_day[d] for d in days], dtype="uint32")
    return RollCalendar(series=series, dataset=source[0].dataset, starts=days,
                        ends=days + np.timedelta64(1, "D"), ids=ids,
                        source_files=[f.path for f in source])


# ── Plan ─────────────────────────────────────────────────────────────────────

@dataclass
class CsvTradesPlan:
    job: CsvJob
    calendar: RollCalendar
    kind: str = "trades"


def plan_csv_trades(raw_dir: str, symbol: str, plans: Dict[str, OutputPlan],
                    batch: Dict[str, dict], log=print) -> Tuple[Optional[CsvTradesPlan], List[dict]]:
    """The CSV trades job to ingest for ``symbol``, if any, and the CSV jobs
    left out (with why). A DBN trades download always wins over a CSV one."""
    jobs = [j for j in find_csv_jobs(raw_dir) if symbol in j.roots]
    ignored = []
    for j in jobs:
        if j.schema != "trades":
            ignored.append({**j.describe(raw_dir), "why": "only CSV trades are supported"})
            log(f"  WARNING: CSV {j.schema} job {j.folder} not ingested (only CSV trades are)")
    jobs = [j for j in jobs if j.schema == "trades"]
    if not jobs:
        return None, ignored
    if "trades" in plans:
        for j in jobs:
            ignored.append({**j.describe(raw_dir), "why": "DBN trades are present"})
            log(f"  NOTE: CSV trades in {j.folder} not used; the DBN trades are")
        return None, ignored
    if len(jobs) > 1:
        raise IngestError(f"{symbol}: {len(jobs)} CSV trades jobs "
                          f"({[j.folder for j in jobs]}); keep one")
    job = jobs[0]
    if job.roots != (symbol,):
        raise IngestError(f"{job.folder} holds several roots {list(job.roots)}; "
                          "only a single-root CSV job is supported")
    if job.stype_in != "parent":
        raise IngestError(f"{job.folder}: CSV trades pulled as {job.stype_in!r}; only "
                          "parent pulls are supported (narrowed by the roll)")
    if not job.files:
        raise IngestError(f"{job.folder}: metadata.json but no .csv/.csv.zst files")
    for p in job.files:
        digest = _sha256(p)
        want = batch.get(os.path.abspath(p), {}).get("sha256")
        if want and want != digest:
            raise IngestError(f"{p}: sha256 {digest[:12]}… does not match the "
                              f"{want[:12]}… in its job's manifest.json — re-download it")
        job.sha256[p] = digest
    cal = roll_calendar(plans, symbol)
    if cal.dataset != job.dataset:
        raise IngestError(f"CSV trades are {job.dataset} but the roll comes from "
                          f"{cal.dataset}")
    # Order by each file's first record, not its name.
    job.files.sort(key=_first_ts_recv)
    return CsvTradesPlan(job=job, calendar=cal), ignored


def _open_text(path: str):
    if path.endswith(".zst"):
        import zstandard
        return io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(open(path, "rb"),
                                closefd=True), encoding="utf-8")
    return open(path, encoding="utf-8")


def _first_ts_recv(path: str) -> pd.Timestamp:
    with _open_text(path) as fh:
        fh.readline()
        first = fh.readline().split(",", 1)[0]
    return pd.Timestamp(first) if first else pd.Timestamp.max.tz_localize("UTC")


# ── Write ────────────────────────────────────────────────────────────────────

def _read_chunks(path: str, chunk_rows: int):
    with _open_text(path) as fh:
        header = tuple(fh.readline().strip().split(","))
        if header != CSV_TRADE_COLUMNS:
            raise IngestError(f"{path}: columns {list(header)} are not Databento's "
                              f"CSV trades layout {list(CSV_TRADE_COLUMNS)}")
        # na_filter off: an empty field must fail the dtype cast, not become NaN.
        for df in pd.read_csv(fh, names=list(CSV_TRADE_COLUMNS), header=None,
                              dtype=_CSV_DTYPES, na_filter=False, chunksize=chunk_rows):
            for c in ("ts_recv", "ts_event"):
                df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601").astype(
                    "datetime64[ns, UTC]")
            yield df


def write_csv_trades(plan: CsvTradesPlan, tmp: str,
                     chunk_rows: Optional[int] = None) -> Tuple[List[dict], dict]:
    """Stream the job's CSVs into ``tmp``, keeping only rows of the contract
    the roll names for each row's ts_recv date. Removes ``tmp`` on failure."""
    chunk_rows = chunk_rows or _CHUNK_ROWS
    cal = plan.calendar
    writer: Optional[pq.ParquetWriter] = None
    schema: Optional[pa.Schema] = None
    per_file, read_total, kept_total = [], 0, 0
    try:
        for path in plan.job.files:
            n_read = n_kept = 0
            t0 = t1 = None
            for df in _read_chunks(path, chunk_rows):
                n_read += len(df)
                days = df["ts_recv"].dt.tz_localize(None).to_numpy().astype("datetime64[D]")
                want, covered = cal.lookup(days)
                if not covered.all():
                    bad = df.loc[~covered].iloc[0]
                    raise IngestError(
                        f"{path}: {int((~covered).sum())} row(s) fall on dates the "
                        f"{cal.series} roll does not cover (first ts_recv "
                        f"{bad['ts_recv']}); the bar files must span the trades")
                df = df.loc[df["instrument_id"].to_numpy() == want]
                if not len(df):
                    continue
                # What a continuous pull's to_df(map_symbols=True) puts here.
                df = df.assign(symbol=cal.series).reset_index(drop=True)
                table = pa.Table.from_pandas(df, preserve_index=False)
                if schema is None:
                    schema = table.schema
                    writer = pq.ParquetWriter(tmp, schema)
                else:
                    table = table.cast(schema)
                writer.write_table(table)
                n_kept += len(df)
                lo, hi = df["ts_event"].min(), df["ts_event"].max()
                t0 = lo if t0 is None or lo < t0 else t0
                t1 = hi if t1 is None or hi > t1 else t1
            per_file.append({"path": path, "sha256": plan.job.sha256.get(path),
                             "rows_read": n_read, "rows": n_kept,
                             "ts_event_min": None if t0 is None else t0.isoformat(),
                             "ts_event_max": None if t1 is None else t1.isoformat()})
            read_total += n_read
            kept_total += n_kept
        if writer is None:
            raise IngestError(f"no {cal.series} rows in {plan.job.folder}")
        writer.close()
        writer = None
    except BaseException:
        if writer is not None:
            writer.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return per_file, {"rows_read": read_total, "rows_kept": kept_total,
                      "rows_other_contracts_and_spreads": read_total - kept_total}


# ── Check against Databento's own bars ───────────────────────────────────────

_BAR_COLS = ["open", "high", "low", "close", "volume"]


def _minute_bars(trades_path: str) -> pd.DataFrame:
    """One-minute OHLCV from trades, bucketed by ts_recv. Partial bars per
    row group are combined, so the whole file is never in memory at once."""
    pf = pq.ParquetFile(trades_path)
    parts = []
    for i in range(pf.num_row_groups):
        t = pf.read_row_group(i, columns=["ts_recv", "instrument_id", "price", "size"]
                              ).to_pandas()
        g = t.groupby([t["ts_recv"].dt.floor("min").rename("ts_event"),
                       "instrument_id"], sort=False)
        parts.append(g.agg(open=("price", "first"), high=("price", "max"),
                           low=("price", "min"), close=("price", "last"),
                           volume=("size", "sum")).reset_index())
    allp = pd.concat(parts, ignore_index=True)
    return (allp.groupby(["ts_event", "instrument_id"], sort=True)
            .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                 close=("close", "last"), volume=("volume", "sum")).reset_index())


def check_against_bars(trades_path: str, ohlcv_path: str, series: str) -> str:
    """Rebuild minute bars from the converted trades and require them to equal
    Databento's own ``ohlcv-1m`` bars for the same series, over the period
    both cover. Returns the line recorded in checks_passed."""
    ours = _minute_bars(trades_path)
    theirs = pq.read_table(ohlcv_path, columns=["ts_event", "instrument_id"] + _BAR_COLS
                           ).to_pandas()
    lo = max(ours["ts_event"].min(), theirs["ts_event"].min())
    hi = min(ours["ts_event"].max(), theirs["ts_event"].max())
    ours = ours[(ours["ts_event"] >= lo) & (ours["ts_event"] <= hi)]
    theirs = theirs[(theirs["ts_event"] >= lo) & (theirs["ts_event"] <= hi)]
    theirs = theirs.astype({"instrument_id": "uint32"})
    ours = ours.astype({"instrument_id": "uint32", "volume": theirs["volume"].dtype})
    m = theirs.merge(ours, on=["ts_event", "instrument_id"], how="outer",
                     suffixes=("_databento", "_rebuilt"), indicator=True)
    only = m[m["_merge"] != "both"]
    both = m[m["_merge"] == "both"]
    diff = np.zeros(len(both), bool)
    for c in _BAR_COLS:
        diff |= (both[f"{c}_databento"] != both[f"{c}_rebuilt"]).to_numpy()
    if len(only) or diff.any():
        ex = pd.concat([only.head(3), both[diff].head(3)])
        raise IngestError(
            f"trades converted from CSV do not reproduce Databento's {series} minute "
            f"bars: {int((m['_merge'] == 'left_only').sum())} bar(s) missing, "
            f"{int((m['_merge'] == 'right_only').sum())} extra, {int(diff.sum())} "
            f"different, of {len(theirs):,}. First:\n{ex.to_string()}")
    return (f"minute bars rebuilt from these trades equal Databento's {series} "
            f"ohlcv-1m: {len(theirs):,} of {len(theirs):,} bars, {lo} → {hi}")
