"""
Trades that need narrowing to one series — called by ``ingest.py``.

Most contracts' trades were pulled as one continuous series (``ZC.n.0``) in
DBN and go straight through ``ingest.py``. Four did not:

    NG, HG  Nolan's CSV batch job, pulled as the parent (``NG.FUT``): every
            contract month and every calendar spread, in daily .csv.zst files
    ES      Arjan's half is DBN ``ES.v.0``; Demetrios's half is DBN ``ES.FUT``
            (parent), so the two halves are different series
    CL, GC  Connor's raw exports: one CSV each of a continuous pull
            (``CL.v.0``, ``GC.n.0``) written from Databento's own reader,
            with no metadata.json or manifest beside them

Rather than pay to re-pull, this module turns each into the
``data/<SYMBOL>/trades.parquet`` a single continuous DBN pull would give.

Which contract to keep on each date is **not estimated**. Databento records
its own roll in the header of every continuous-series file: the minute bars
(``NG.v.0``, ``ES.n.0`` …) name the instrument id the series pointed at for
each date. Those mappings are read from the bar files ``ingest.py`` is
ingesting alongside, and a row is kept if its ``instrument_id`` is the one
mapped for the UTC date of its ``ts_recv`` (the date Databento's own reader
uses to map symbols). Rows of other contracts are dropped only from parent
pulls; a continuous source holding a row the roll doesn't name stops the
run, and a continuous DBN source whose own header maps a date differently
from the roll stops it too.

The result is checked against Databento's own bars: one-minute bars rebuilt
from the kept trades (bucketed by ``ts_recv``, as Databento builds them) must
equal the ``ohlcv-1m`` file bar for bar, or the run stops.

What each source holds comes from its own metadata or contents —
``metadata.json`` for a CSV job, the DBN header, the CSV header and
``symbol`` column for an export — never from file or folder names. Files
listed in a job's ``manifest.json`` are hashed against it.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import warnings
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ingest import (IngestError, OutputPlan, RawFile, _CHUNK_ROWS, _SCRATCH_PREFIX,
                    _frames, _ns_to_iso, _sha256, _stype_label, root_of)

# Databento's CSV layout of the trades schema, in its column order — the
# same columns, in the same order, as DBNStore.to_df() gives after ts_recv
# leaves the index (which is also what an export of to_df() holds).
CSV_TRADE_COLUMNS = ("ts_recv", "ts_event", "rtype", "publisher_id", "instrument_id",
                     "action", "side", "depth", "price", "size", "flags",
                     "ts_in_delta", "sequence", "symbol")
_CSV_DTYPES = {"rtype": "uint8", "publisher_id": "uint16", "instrument_id": "uint32",
               "action": "str", "side": "str", "depth": "uint8", "price": "float64",
               "size": "uint32", "flags": "uint8", "ts_in_delta": "int32",
               "sequence": "uint32", "symbol": "str"}
_CSV_SUFFIXES = (".csv.zst", ".csv")


# ── Finding CSV sources ──────────────────────────────────────────────────────

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
        return {"source": "databento_csv_job",
                "job_folder": os.path.relpath(self.folder, raw_dir),
                "dataset": self.dataset, "schema": self.schema,
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


@dataclass
class CsvExport:
    """A plain CSV of one continuous series' trades, written from
    ``DBNStore.to_df()``: same columns, ``symbol`` holds the series."""
    path: str
    series: str
    roots: Tuple[str, ...]
    start_ns: int                    # first row's ts_recv
    end_ns: int                      # last row's ts_recv + 1
    sha256: Optional[str] = None

    def describe(self, raw_dir: str) -> dict:
        return {"source": "csv_export_of_continuous_pull",
                "path": os.path.relpath(self.path, raw_dir), "series": self.series,
                "sha256": self.sha256, "first_ts_recv": _ns_to_iso(self.start_ns),
                "last_ts_recv": _ns_to_iso(self.end_ns - 1)}


def _last_line(path: str) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        buf = b""
        while pos > 0 and buf.count(b"\n") < 2:
            step = min(1 << 16, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
    lines = [ln for ln in buf.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def find_csv_exports(raw_dir: str, jobs: Sequence[CsvJob]) -> List[CsvExport]:
    """Plain .csv files outside any CSV job whose header is the trades layout
    and whose rows name one continuous series."""
    in_jobs = {p for j in jobs for p in j.files}
    out = []
    for dirpath, dirnames, files in os.walk(raw_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(_SCRATCH_PREFIX)]
        for name in files:
            p = os.path.abspath(os.path.join(dirpath, name))
            if not name.endswith(".csv") or p in in_jobs:
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                header = tuple(fh.readline().strip().split(","))
                first = fh.readline().strip().split(",")
            if header != CSV_TRADE_COLUMNS or len(first) != len(header):
                continue
            last = _last_line(p).split(",")
            series = first[-1]
            if root_of(series) is None or last[-1] != series:
                continue
            out.append(CsvExport(path=p, series=series, roots=(root_of(series),),
                                 start_ns=pd.Timestamp(first[0]).value,
                                 end_ns=pd.Timestamp(last[0]).value + 1))
    return sorted(out, key=lambda e: e.path)


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


def _day_map(path: str, series: str) -> Dict[np.datetime64, int]:
    import databento as db
    out: Dict[np.datetime64, int] = {}
    for iv in (db.DBNStore.from_file(path).metadata.mappings or {}).get(series, []):
        for d in np.arange(np.datetime64(iv["start_date"], "D"),
                           np.datetime64(iv["end_date"], "D")):
            out[d] = int(iv["symbol"])
    return out


def roll_calendar(plans: Dict[str, OutputPlan], symbol: str) -> RollCalendar:
    """Databento's own date → instrument mapping for the continuous series
    this contract was pulled as, read from the headers of the files being
    ingested alongside (minute bars first)."""
    order = ["ohlcv1m"] + sorted(k for k in plans if k != "ohlcv1m")
    source: Optional[List[RawFile]] = None
    for kind in order:
        files = plans[kind].files if kind in plans else []
        if files and all(_stype_label(f) == "continuous" for f in files):
            source = files
            break
    if source is None:
        raise IngestError(
            f"the {symbol} trades need narrowing to one series (a parent pull, or "
            f"sources of different series), and no continuous-series file for "
            f"{symbol} is here to say which contract to keep on each date. Put the "
            f"{symbol} continuous minute bars in the raw folder too; their headers "
            "carry Databento's roll.")
    series = [s for s in source[0].symbols if root_of(s) == symbol]
    if len(series) != 1:
        raise IngestError(f"cannot tell which continuous series to follow from "
                          f"{source[0].path}: header symbols {list(source[0].symbols)}")
    series = series[0]
    by_day: Dict[np.datetime64, int] = {}
    for f in source:
        for d, iid in _day_map(f.path, series).items():
            if by_day.setdefault(d, iid) != iid:
                raise IngestError(f"{series}: the bar files disagree about {d} "
                                  f"(instrument {by_day[d]} vs {iid}, in {f.path})")
    if not by_day:
        raise IngestError(f"no {series} mappings in the headers of "
                          f"{[f.path for f in source]}")
    days = np.array(sorted(by_day), dtype="datetime64[D]")
    ids = np.array([by_day[d] for d in days], dtype="uint32")
    return RollCalendar(series=series, dataset=source[0].dataset, starts=days,
                        ends=days + np.timedelta64(1, "D"), ids=ids,
                        source_files=[f.path for f in source])


# ── Choosing the trades sources ──────────────────────────────────────────────

@dataclass
class Source:
    """One source of trades for the narrowed output: a CSV job, a CSV
    export, or one DBN file."""
    kind: str                                   # csv_job | csv_export | dbn
    start_ns: int
    end_ns: Optional[int]
    parent: bool                                # rows of other contracts expected
    series: Optional[str]                       # continuous sources
    job: Optional[CsvJob] = None
    export: Optional[CsvExport] = None
    raw: Optional[RawFile] = None

    @property
    def label(self) -> str:
        return (self.job.folder if self.job else
                self.export.path if self.export else self.raw.path)

    def describe(self, raw_dir: str) -> dict:
        if self.job:
            return self.job.describe(raw_dir)
        if self.export:
            return self.export.describe(raw_dir)
        return {"source": "dbn", **self.raw.describe(raw_dir)}


@dataclass
class NarrowedPlan:
    sources: List[Source]
    calendar: Optional[RollCalendar] = None
    kind: str = "trades"


def select_trades_sources(files: Sequence[RawFile], raw_dir: str, symbol: str,
                          log=print) -> Tuple[List[RawFile], Optional[NarrowedPlan], List[dict]]:
    """Decide how ``symbol``'s trades are ingested.

    Returns the DBN files for ``ingest.py``'s ordinary path, the sources to
    narrow (or None), and the sources left out with why. DBN trades that are
    all one pull (one continuous series, or one parent) take the ordinary path
    and win over any CSV. Otherwise every trades source for the root — DBN of
    different series, CSV jobs, CSV exports — is narrowed to the roll together.
    """
    jobs_all = find_csv_jobs(raw_dir)
    jobs = [j for j in jobs_all if symbol in j.roots]
    exports = [e for e in find_csv_exports(raw_dir, jobs_all) if symbol in e.roots]
    not_used = []
    for j in jobs:
        if j.schema != "trades":
            not_used.append({**j.describe(raw_dir), "why": "only CSV trades are supported"})
            log(f"  WARNING: CSV {j.schema} job {j.folder} not ingested (only CSV trades are)")
    jobs = [j for j in jobs if j.schema == "trades"]

    dbn_trades = [f for f in files if symbol in f.roots and f.kind == "trades"]
    # One kind of DBN pull (one continuous series, or one parent pull) is a
    # faithful conversion on ingest.py's own path; only a mix needs narrowing.
    kinds = {(_stype_label(f), tuple(s for s in f.symbols if root_of(s) == symbol))
             for f in dbn_trades}
    ordinary = len(kinds) == 1
    if ordinary or not (dbn_trades or jobs or exports):
        for src in jobs + exports:
            not_used.append({**src.describe(raw_dir), "why": "DBN trades are present"})
            log(f"  NOTE: CSV trades in {getattr(src, 'folder', None) or src.path} "
                "not used; the DBN trades are")
        return list(files), None, not_used

    sources: List[Source] = []
    for j in jobs:
        if j.roots != (symbol,):
            raise IngestError(f"{j.folder} holds several roots {list(j.roots)}; "
                              "only a single-root CSV job is supported")
        if j.stype_in != "parent":
            raise IngestError(f"{j.folder}: CSV trades pulled as {j.stype_in!r}; only "
                              "parent pulls are supported as CSV jobs")
        if not j.files:
            raise IngestError(f"{j.folder}: metadata.json but no .csv/.csv.zst files")
        sources.append(Source("csv_job", j.start_ns, j.end_ns, True, None, job=j))
    for e in exports:
        sources.append(Source("csv_export", e.start_ns, e.end_ns, False, e.series, export=e))
    for f in dbn_trades:
        lab = _stype_label(f)
        own = [s for s in f.symbols if root_of(s) == symbol]
        if lab == "parent":
            sources.append(Source("dbn", f.start_ns, f.end_ns, True, None, raw=f))
        elif lab == "continuous" and len(own) == 1:
            sources.append(Source("dbn", f.start_ns, f.end_ns, False, own[0], raw=f))
        else:
            raise IngestError(f"{f.path}: trades pulled as {lab} {list(f.symbols)}; "
                              "cannot be narrowed to one series")
    sources.sort(key=lambda s: (s.start_ns, s.label))
    inf = np.iinfo(np.int64).max
    for a, b in zip(sources, sources[1:]):
        if b.start_ns < (a.end_ns if a.end_ns is not None else inf):
            raise IngestError(
                f"{symbol} trades: {a.label} ({_ns_to_iso(a.start_ns)} → "
                f"{_ns_to_iso(a.end_ns)}) and {b.label} ({_ns_to_iso(b.start_ns)} → "
                f"{_ns_to_iso(b.end_ns)}) cover the same period; keep one")
    kept = [f for f in files if f not in dbn_trades]
    return kept, NarrowedPlan(sources=sources), not_used


def finish_plan(plan: NarrowedPlan, plans: Dict[str, OutputPlan], symbol: str,
                batch: Dict[str, dict]) -> NarrowedPlan:
    """Hash every source file, read the roll, and check every source against it."""
    cal = roll_calendar(plans, symbol)
    for s in plan.sources:
        paths = (s.job.files if s.job else [s.export.path] if s.export else [s.raw.path])
        for p in paths:
            digest = _sha256(p)
            want = batch.get(os.path.abspath(p), {}).get("sha256")
            if want and want != digest:
                raise IngestError(f"{p}: sha256 {digest[:12]}… does not match the "
                                  f"{want[:12]}… in its job's manifest.json — re-download it")
            if s.job:
                s.job.sha256[p] = digest
            elif s.export:
                s.export.sha256 = digest
            else:
                s.raw.sha256 = digest
        ds = s.job.dataset if s.job else s.raw.dataset if s.raw else None
        if ds is not None and ds != cal.dataset:
            raise IngestError(f"{s.label} is {ds} but the roll comes from {cal.dataset}")
        if s.raw is not None and not s.parent and s.series != cal.series:
            # Another continuous series: its own roll must agree with this one
            # on every date it covers, or its rows are a different contract.
            for d, iid in _day_map(s.raw.path, s.series).items():
                want_id, ok = cal.lookup(np.array([d], dtype="datetime64[D]"))
                if ok[0] and int(want_id[0]) != iid:
                    raise IngestError(
                        f"{s.raw.path} is {s.series}, which maps {d} to instrument "
                        f"{iid}; {cal.series} maps it to {int(want_id[0])}. The two "
                        "series hold different contracts that day.")
        if s.job:
            s.job.files.sort(key=_first_ts_recv)
    plan.calendar = cal
    return plan


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

def _read_chunks(path: str, chunk_rows: int) -> Iterable[pd.DataFrame]:
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


def _source_frames(s: Source, symbol: str, chunk_rows: int,
                   scratch: str) -> Iterable[Tuple[str, pd.DataFrame]]:
    if s.job:
        for p in s.job.files:
            for df in _read_chunks(p, chunk_rows):
                yield p, df
    elif s.export:
        for df in _read_chunks(s.export.path, chunk_rows):
            yield s.export.path, df
    else:
        from databento.common.error import BentoWarning
        with warnings.catch_warnings():
            warnings.simplefilter("error", BentoWarning)
            try:
                for df in _frames(s.raw, symbol, chunk_rows, scratch):
                    yield s.raw.path, df
            except BentoWarning as w:
                raise IngestError(f"{s.raw.path}: {w} — re-download it") from w
            except EOFError as e:
                raise IngestError(f"{s.raw.path}: compressed stream ends early "
                                  f"({e}) — re-download it") from e


def write_narrowed_trades(plan: NarrowedPlan, tmp: str, symbol: str,
                          chunk_rows: Optional[int] = None) -> Tuple[List[dict], dict]:
    """Stream every source into ``tmp``, keeping rows of the contract the roll
    names for each row's ts_recv date. Removes ``tmp`` on failure."""
    chunk_rows = chunk_rows or _CHUNK_ROWS
    cal = plan.calendar
    writer: Optional[pq.ParquetWriter] = None
    schema: Optional[pa.Schema] = None
    stats: Dict[str, dict] = {}
    scratch = tempfile.TemporaryDirectory(prefix=_SCRATCH_PREFIX, dir=os.path.dirname(tmp))
    try:
        for s in plan.sources:
            for path, df in _source_frames(s, symbol, chunk_rows, scratch.name):
                st = stats.setdefault(path, {"path": path, "rows_read": 0, "rows": 0,
                                             "ts_event_min": None, "ts_event_max": None})
                st["rows_read"] += len(df)
                if not len(df):
                    continue
                days = df["ts_recv"].dt.tz_localize(None).to_numpy().astype("datetime64[D]")
                want, covered = cal.lookup(days)
                if not covered.all():
                    bad = df.loc[~covered].iloc[0]
                    raise IngestError(
                        f"{path}: {int((~covered).sum())} row(s) fall on dates the "
                        f"{cal.series} roll does not cover (first ts_recv "
                        f"{bad['ts_recv']}); the bar files must span the trades")
                keep = df["instrument_id"].to_numpy() == want
                if not s.parent and not keep.all():
                    bad = df.loc[~keep].iloc[0]
                    raise IngestError(
                        f"{path} is a {s.series} pull but holds {int((~keep).sum())} "
                        f"row(s) of instruments the {cal.series} roll does not name "
                        f"(first: instrument {bad['instrument_id']} at {bad['ts_recv']})")
                df = df.loc[keep]
                if not len(df):
                    continue
                # What a continuous pull's to_df(map_symbols=True) puts here.
                df = df.assign(symbol=cal.series).reset_index(drop=True)
                table = pa.Table.from_pandas(df, preserve_index=False)
                if schema is None:
                    schema = table.schema
                    writer = pq.ParquetWriter(tmp, schema)
                else:
                    try:
                        table = table.cast(schema)
                    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, ValueError) as e:
                        raise IngestError(f"{path}: column layout differs from the "
                                          f"sources before it:\n  {table.schema}") from e
                writer.write_table(table)
                st["rows"] += len(df)
                lo, hi = df["ts_event"].min(), df["ts_event"].max()
                st["ts_event_min"] = lo if st["ts_event_min"] is None else min(lo, st["ts_event_min"])
                st["ts_event_max"] = hi if st["ts_event_max"] is None else max(hi, st["ts_event_max"])
        if writer is None:
            raise IngestError(f"no {cal.series} rows in any {symbol} trades source")
        writer.close()
        writer = None
    except BaseException:
        if writer is not None:
            writer.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    finally:
        scratch.cleanup()
    per_file = []
    for st in stats.values():
        for k in ("ts_event_min", "ts_event_max"):
            st[k] = None if st[k] is None else st[k].isoformat()
        per_file.append(st)
    read = sum(p["rows_read"] for p in per_file)
    kept = sum(p["rows"] for p in per_file)
    return per_file, {"rows_read": read, "rows_kept": kept,
                      "rows_other_contracts_and_spreads": read - kept}


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
    """Rebuild minute bars from the narrowed trades and require them to equal
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
            f"narrowed trades do not reproduce Databento's {series} minute "
            f"bars: {int((m['_merge'] == 'left_only').sum())} bar(s) missing, "
            f"{int((m['_merge'] == 'right_only').sum())} extra, {int(diff.sum())} "
            f"different, of {len(theirs):,}. First:\n{ex.to_string()}")
    return (f"minute bars rebuilt from these trades equal Databento's {series} "
            f"ohlcv-1m: {len(theirs):,} of {len(theirs):,} bars, {lo} → {hi}")
