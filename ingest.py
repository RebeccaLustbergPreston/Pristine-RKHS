"""
Ingestion — raw Databento downloads → one standard parquet layout per contract.

    python ingest.py --symbol NG
    python ingest.py --symbol NG --raw-dir ~/Downloads/databento --dry-run
    python ingest.py --symbol NG --check          # re-verify existing outputs

What it does
------------
Walks ``--raw-dir`` (default ``data/raw/``) recursively for ``*.dbn.zst`` /
``*.dbn`` files, however they are organised, and decides what each file holds
by reading the DBN header — dataset, schema, symbols, symbology, start/end and
the instrument mappings. **File and folder names are never used**: six people
organise downloads six ways, and names lie (the corn file "for December"
opens with records from 30 November).

Every file whose header names the requested contract root is decoded with
Databento's own reader and written to

    data/<SYMBOL>/trades.parquet        schema trades
    data/<SYMBOL>/ohlcv1m.parquet       schema ohlcv-1m
    data/<SYMBOL>/definitions.parquet   schema definition
    data/<SYMBOL>/statistics.parquet    schema statistics
    data/<SYMBOL>/<schema>.parquet      anything else (e.g. mbp10.parquet)
    data/<SYMBOL>/ingest_manifest.json  what went in, from where, and checks

This step is a faithful format conversion, not cleaning. It does not drop,
sort, de-duplicate, adjust or fill anything — that is piece B
(docs/data_cleaning_standard.md). The two exceptions are representational:
``ts_recv`` becomes a column instead of the index, so every timestamp is a
column, and Databento's "undefined quantity" sentinel in statistics becomes a
real missing value instead of 9.2e18, so missing stays missing.

It stops with an error, rather than guessing, when the inputs do not line up:
two different files covering the same period, files for one output that come
from different datasets or different series (``NG.v.0`` mixed with
``NG.c.0``), mismatched column layouts, or a written file that fails the
checks in ``verify_output``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import tempfile
import warnings
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

INGEST_VERSION = 1
_ROOT = os.path.dirname(os.path.abspath(__file__))

# Databento schema → output file stem. Anything not listed falls back to the
# schema name with dashes removed (mbp-10 → mbp10).
SCHEMA_TO_KIND = {
    "trades": "trades",
    "ohlcv-1m": "ohlcv1m",
    "definition": "definitions",
    "statistics": "statistics",
}

# Columns each output must carry for downstream code (piece B cleans these
# into the exact names/types in docs/data_cleaning_standard.md §2).
# ``session_date`` is deliberately absent: tagging the exchange session is
# cleaning rule f, not ingestion.
REQUIRED_COLUMNS = {
    "trades": ("ts_event", "ts_recv", "price", "size", "side",
               "instrument_id", "symbol", "sequence"),
    "ohlcv1m": ("ts_event", "open", "high", "low", "close", "volume",
                "instrument_id", "symbol"),
    "definitions": ("ts_recv", "raw_symbol", "instrument_id", "expiration",
                    "min_price_increment", "min_price_increment_amount",
                    "unit_of_measure_qty", "symbol"),
    "statistics": ("ts_event", "ts_recv", "stat_type", "price", "quantity",
                   "instrument_id", "symbol"),
}
_TS_COLUMNS = ("ts_event", "ts_recv")

# Databento marks an absent statistics quantity with INT64_MAX.
_UNDEF_STAT_QUANTITY = np.iinfo(np.int64).max

_MONTH_CODES = "FGHJKMNQUVXZ"
_RE_CONTINUOUS = re.compile(r"^([A-Z0-9]+)\.[CNV]\.\d+$")
_RE_PARENT = re.compile(r"^([A-Z0-9]+)\.(?:FUT|OPT)$")
_RE_OUTRIGHT = re.compile(rf"^([A-Z0-9]+?)[{_MONTH_CODES}]\d{{1,2}}$")

_CHUNK_ROWS = 2_000_000
# Definitions rows are ~70 columns with many strings; decode them in smaller
# chunks so a whole-range parent pull doesn't need several GB at once.
_CHUNK_ROWS_BY_SCHEMA = {"definition": 200_000}


class IngestError(RuntimeError):
    """Inputs do not line up. Raised instead of patching around the problem."""


# ── Identifying a file from its own metadata ─────────────────────────────────

def root_of(symbol: str) -> Optional[str]:
    """
    Contract root of a Databento symbol, or None if it cannot be told.

        NG.v.0 → NG      (continuous)       NG.FUT → NG   (parent)
        NGZ3   → NG      (outright)         NGZ3-NGF4 → NG (calendar spread)
        LOZ3 C4800 → LO  (option)           4242 → None   (instrument id)
    """
    s = str(symbol).strip().upper()
    for rx in (_RE_CONTINUOUS, _RE_PARENT):
        m = rx.match(s)
        if m:
            return m.group(1)
    s = s.split(" ")[0]                         # drop an option strike suffix
    legs = re.split(r"[-:]", s)
    roots = set()
    for leg in legs:
        m = _RE_OUTRIGHT.match(leg)
        if not m:
            return None
        roots.add(m.group(1))
    return roots.pop() if len(roots) == 1 else None


def _ns_to_iso(ns: Optional[int]) -> Optional[str]:
    if ns is None:
        return None
    return pd.Timestamp(int(ns), unit="ns", tz="UTC").isoformat()


@dataclass
class RawFile:
    path: str
    sha256: Optional[str]                # filled only for files that are used
    n_bytes: int
    dataset: str
    schema: str
    stype_in: str
    symbols: Tuple[str, ...]
    start_ns: int
    end_ns: Optional[int]
    roots: Tuple[str, ...]                 # contract roots the header names
    unidentified_symbols: Tuple[str, ...]  # header symbols with no root
    dbn_version: int = 3

    @property
    def kind(self) -> str:
        return SCHEMA_TO_KIND.get(self.schema, self.schema.replace("-", ""))

    def describe(self, raw_dir: str) -> dict:
        return {
            "path": os.path.relpath(self.path, raw_dir),
            "sha256": self.sha256,
            "bytes": self.n_bytes,
            "dbn_version": self.dbn_version,
            "dataset": self.dataset,
            "schema": self.schema,
            "stype_in": self.stype_in,
            "symbols": list(self.symbols),
            "metadata_start": _ns_to_iso(self.start_ns),
            "metadata_end": _ns_to_iso(self.end_ns),
        }


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _enum_str(v) -> str:
    return str(getattr(v, "value", v))


def read_header(path: str) -> RawFile:
    """Identify a raw file from its DBN header alone — never from its name."""
    import databento as db

    md = db.DBNStore.from_file(path).metadata
    # Symbols Databento reported as not found carry no data; don't let them
    # make a file look like it belongs to a root.
    not_found = {str(s) for s in (md.not_found or ())}
    symbols = tuple(str(s) for s in (md.symbols or ()) if str(s) not in not_found)
    candidates = list(symbols)
    # A whole-venue pull says ALL_SYMBOLS; an instrument-id pull lists bare
    # numbers. In both cases the mappings carry the real symbols.
    if not candidates or any(s == "ALL_SYMBOLS" or s.isdigit() for s in symbols):
        candidates = []
        for key, intervals in (md.mappings or {}).items():
            candidates.append(key)
            # Mapping values are instrument ids for most pulls; keep only
            # the ones that are symbols.
            candidates.extend(iv["symbol"] for iv in intervals
                              if not str(iv["symbol"]).isdigit())
    roots, unknown = set(), []
    for s in candidates:
        r = root_of(s)
        if r is None:
            unknown.append(s)
        else:
            roots.add(r)
    end = md.end
    return RawFile(
        path=os.path.abspath(path),
        sha256=None,
        n_bytes=os.path.getsize(path),
        dataset=str(md.dataset),
        schema=_enum_str(md.schema),
        stype_in=_enum_str(md.stype_in),
        symbols=symbols,
        start_ns=int(md.start),
        end_ns=None if end is None else int(end),
        roots=tuple(sorted(roots)),
        unidentified_symbols=tuple(sorted(set(unknown)))[:20],
        dbn_version=int(md.version),
    )


def find_raw_files(raw_dir: str) -> List[str]:
    out = []
    for dirpath, _, files in os.walk(raw_dir):
        for name in files:
            if name.endswith(".dbn.zst") or name.endswith(".dbn"):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


# ── Grouping and the alignment checks ────────────────────────────────────────

@dataclass
class OutputPlan:
    kind: str
    schema: str
    files: List[RawFile] = field(default_factory=list)
    duplicates_skipped: List[dict] = field(default_factory=list)
    coverage_gaps: List[dict] = field(default_factory=list)


def plan_outputs(files: Sequence[RawFile], symbol: str) -> Dict[str, OutputPlan]:
    """
    Group the files for ``symbol`` by output and check that they line up.

    Raises IngestError when files for one output disagree on dataset, on the
    series pulled for this root, or cover overlapping periods with different
    contents. Byte-identical copies are skipped and recorded.
    """
    plans: Dict[str, OutputPlan] = {}
    for f in files:
        if symbol not in f.roots:
            continue
        plan = plans.setdefault(f.kind, OutputPlan(kind=f.kind, schema=f.schema))
        if plan.schema != f.schema:
            raise IngestError(
                f"{f.kind}: schemas {plan.schema!r} and {f.schema!r} would both "
                f"write {f.kind}.parquet ({f.path})")
        plan.files.append(f)

    for plan in plans.values():
        # 1. Byte-identical copies of the same download: keep the first.
        #    Hashing is done here, only for files this run will use.
        for f in plan.files:
            if f.sha256 is None:
                f.sha256 = _sha256(f.path)
        seen: Dict[str, RawFile] = {}
        unique = []
        for f in sorted(plan.files, key=lambda x: (x.start_ns, x.path)):
            if f.sha256 in seen:
                plan.duplicates_skipped.append(
                    {"path": f.path, "identical_to": seen[f.sha256].path})
                continue
            seen[f.sha256] = f
            unique.append(f)
        plan.files = unique

        # 2. One dataset and one series per output.
        datasets = {f.dataset for f in plan.files}
        if len(datasets) > 1:
            raise IngestError(
                f"{plan.kind}: files come from different datasets {sorted(datasets)}; "
                + _list_paths(plan.files))
        series = {(f.stype_in, _series_for(f, symbol)) for f in plan.files}
        if len(series) > 1:
            raise IngestError(
                f"{plan.kind}: files pull different series for {symbol} "
                f"{sorted(series)} — e.g. a volume roll mixed with a calendar "
                "roll. Pick one and remove the others; " + _list_paths(plan.files))

        # 3. No two distinct files may cover the same period.
        for a, b in zip(plan.files, plan.files[1:]):
            a_end = a.end_ns if a.end_ns is not None else np.iinfo(np.int64).max
            if b.start_ns < a_end:
                raise IngestError(
                    f"{plan.kind}: {a.path} covers {_ns_to_iso(a.start_ns)} → "
                    f"{_ns_to_iso(a.end_ns)} and {b.path} starts at "
                    f"{_ns_to_iso(b.start_ns)}. Overlapping downloads with "
                    "different contents would duplicate or contradict records; "
                    "remove one.")
            if a.end_ns is not None and b.start_ns > a.end_ns:
                plan.coverage_gaps.append(
                    {"from": _ns_to_iso(a.end_ns), "to": _ns_to_iso(b.start_ns),
                     "after": a.path, "before": b.path})
    return plans


def _series_for(f: RawFile, symbol: str) -> Tuple[str, ...]:
    """Header symbols that belong to ``symbol`` (other roots in a shared file
    don't count). Outright contracts collapse to the root, so pulling NGZ3
    in one month and NGF4 in the next is still one series."""
    own = [s for s in f.symbols if root_of(s) == symbol]
    if f.stype_in == "raw_symbol" and "ALL_SYMBOLS" not in f.symbols:
        return (f"{symbol} outrights",)
    return tuple(sorted(own)) or (f"{symbol} via mappings",)


def _list_paths(files: Iterable[RawFile]) -> str:
    return "files: " + ", ".join(f.path for f in files)


# ── Decoding and writing ─────────────────────────────────────────────────────

def _upgrade_to_v3(src: str, dest: str) -> None:
    """Rewrite a DBN v1/v2 file as v3. ``DBNStore.to_df`` decodes with the
    file's own version, so without this an older download comes out with a
    different column layout (definitions) or a 32-bit "undefined" sentinel
    that survives as the number 2147483647 (statistics)."""
    import databento as db
    import databento_dbn as dbn

    store = db.DBNStore.from_file(src)
    md3 = dbn.Metadata.decode(store.metadata.encode(),
                              upgrade_policy=dbn.VersionUpgradePolicy.UPGRADE_TO_V3)
    with open(dest, "wb") as out:
        out.write(md3.encode())
        for rec in store:                      # DBNStore iteration upgrades to v3
            out.write(bytes(rec))


def _frames(f: RawFile, symbol: str, chunk_rows: int,
            scratch_dir: str) -> Iterable[pd.DataFrame]:
    import databento as db

    path = f.path
    if f.dbn_version < 3:
        path = os.path.join(scratch_dir, f"{f.sha256}.v3.dbn")
        _upgrade_to_v3(f.path, path)
    store = db.DBNStore.from_file(path)
    if int(store.metadata.version) != 3:
        raise IngestError(f"{f.path}: could not bring DBN v{f.dbn_version} up to v3")
    shared = f.roots != (symbol,)
    for df in store.to_df(price_type="float", pretty_ts=True, map_symbols=True,
                          count=chunk_rows):
        if df.index.name is not None:
            df = df.reset_index()
        # Every row must resolve to a symbol through the file's own mappings.
        # One that does not lies outside the dates the header vouches for, so
        # we cannot say which contract it is — stop rather than guess.
        if "symbol" not in df.columns or df["symbol"].isna().any():
            n_bad = len(df) if "symbol" not in df.columns else int(df["symbol"].isna().sum())
            first = df.loc[df["symbol"].isna()].iloc[0] if "symbol" in df.columns else df.iloc[0]
            raise IngestError(
                f"{f.path}: {n_bad} row(s) have no symbol mapping in the file's "
                f"own metadata (first: instrument_id={first.get('instrument_id')}, "
                f"ts_event={first.get('ts_event')}), so they cannot be attributed "
                "to a contract.")
        if shared:
            # A file with several roots: keep only rows for this one. A row
            # whose symbol has no recognisable root (a user-defined or
            # inter-commodity spread) cannot be assigned, and dropping it
            # silently would make the output depend on how the download was
            # bundled — stop instead.
            uniq = pd.unique(df["symbol"])
            root_map = {u: root_of(u) for u in uniq}
            orphans = [u for u, r in root_map.items() if r is None]
            if orphans:
                raise IngestError(
                    f"{f.path} holds several roots {list(f.roots)} and rows for "
                    f"symbols with no single root {orphans[:5]}; they cannot be "
                    "assigned to one contract. Download this root separately.")
            df = df[df["symbol"].map(root_map) == symbol]
        if f.kind == "statistics" and "quantity" in df.columns:
            q = df["quantity"]
            df = df.assign(quantity=q.astype("Int64").mask(q == _UNDEF_STAT_QUANTITY))
        yield df.reset_index(drop=True)


def _frame_stats(df: pd.DataFrame) -> Tuple[int, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    if len(df) == 0 or "ts_event" not in df.columns:
        return len(df), None, None
    ts = df["ts_event"]
    return len(df), ts.min(), ts.max()


def write_output(plan: OutputPlan, tmp: str, symbol: str,
                 chunk_rows: Optional[int] = None) -> List[dict]:
    """Stream every file of one output into a single parquet at ``tmp``, in
    header-start order. Removes ``tmp`` if anything fails part-way."""
    from databento.common.error import BentoWarning

    if chunk_rows is None:
        chunk_rows = _CHUNK_ROWS_BY_SCHEMA.get(plan.schema, _CHUNK_ROWS)
    writer: Optional[pq.ParquetWriter] = None
    schema: Optional[pa.Schema] = None
    per_file = []
    scratch = tempfile.TemporaryDirectory(prefix=".ingest-", dir=os.path.dirname(tmp))
    try:
        for f in plan.files:
            n, t0, t1 = 0, None, None
            # Databento only *warns* when a file is cut short; an interrupted
            # download would otherwise be ingested as if it were complete.
            with warnings.catch_warnings():
                warnings.simplefilter("error", BentoWarning)
                try:
                    for df in _frames(f, symbol, chunk_rows, scratch.name):
                        table = pa.Table.from_pandas(df, preserve_index=False)
                        if schema is None:
                            schema = table.schema
                            writer = pq.ParquetWriter(tmp, schema)
                        elif not table.schema.equals(schema, check_metadata=False):
                            try:
                                table = table.cast(schema)
                            except (pa.ArrowInvalid, pa.ArrowNotImplementedError,
                                    ValueError) as e:
                                raise IngestError(
                                    f"{plan.kind}: {f.path} has a different column "
                                    f"layout from the files before it:\n  {table.schema}"
                                    f"\nvs\n  {schema}") from e
                        if table.num_rows:
                            writer.write_table(table)
                        k, lo, hi = _frame_stats(df)
                        n += k
                        t0 = lo if t0 is None or (lo is not None and lo < t0) else t0
                        t1 = hi if t1 is None or (hi is not None and hi > t1) else t1
                except BentoWarning as w:
                    raise IngestError(f"{f.path}: {w} — re-download it") from w
            per_file.append({"path": f.path, "rows": n,
                             "ts_event_min": None if t0 is None else t0.isoformat(),
                             "ts_event_max": None if t1 is None else t1.isoformat()})
        if writer is None:
            raise IngestError(f"{plan.kind}: no records decoded from {_list_paths(plan.files)}")
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
    return per_file


# ── Output checks ────────────────────────────────────────────────────────────

def verify_output(path: str, kind: str, expected_rows: Optional[int] = None) -> List[str]:
    """
    Structural checks on one written output. Returns a list of the checks
    that passed; raises IngestError on the first one that fails.

    These are the properties crude's files have and that every loader and
    cleaning step relies on: the file opens, has rows, carries the required
    columns, every timestamp column is UTC at nanosecond precision, and
    ``instrument_id`` / ``symbol`` are never missing (they are how contract
    switches are found).
    """
    passed = []
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    n = pf.metadata.num_rows
    if n == 0:
        raise IngestError(f"{path}: no rows")
    passed.append(f"opens, {n:,} rows")
    if expected_rows is not None and n != expected_rows:
        raise IngestError(f"{path}: {n} rows on disk but {expected_rows} decoded")
    required = REQUIRED_COLUMNS.get(kind, ("instrument_id",))
    missing = [c for c in required if c not in schema.names]
    if missing:
        raise IngestError(f"{path}: missing required columns {missing}")
    passed.append(f"required columns present: {', '.join(required)}")
    for c in _TS_COLUMNS + (("expiration",) if kind == "definitions" else ()):
        if c not in schema.names:
            continue
        t = schema.field(c).type
        if not (pa.types.is_timestamp(t) and t.unit == "ns" and t.tz in ("UTC", "+00:00")):
            raise IngestError(f"{path}: {c} is {t}, expected timestamp[ns, tz=UTC]")
    passed.append("timestamps are UTC, ns")
    cols = [c for c in ("instrument_id", "symbol") if c in schema.names]
    nulls = {c: 0 for c in cols}
    for i in range(pf.num_row_groups):
        rg = pf.read_row_group(i, columns=cols)
        for c in cols:
            nulls[c] += rg.column(c).null_count
    bad = {c: k for c, k in nulls.items() if k}
    if bad:
        raise IngestError(f"{path}: missing values in {bad}")
    passed.append("instrument_id and symbol never missing")
    if "ts_event" in schema.names:
        nat = sum(pf.read_row_group(i, columns=["ts_event"]).column(0).null_count
                  for i in range(pf.num_row_groups))
        if nat:
            raise IngestError(f"{path}: {nat} rows with no ts_event")
        passed.append("ts_event never missing")
    return passed


# ── Driver ───────────────────────────────────────────────────────────────────

def ingest(symbol: str, raw_dir: str, out_dir: str, force: bool = False,
           dry_run: bool = False, chunk_rows: Optional[int] = None,
           log=print) -> dict:
    symbol = symbol.upper()
    raw_dir = os.path.abspath(raw_dir)
    if not os.path.isdir(raw_dir):
        raise IngestError(f"raw directory {raw_dir} does not exist")
    paths = find_raw_files(raw_dir)
    if not paths:
        raise IngestError(f"no .dbn.zst / .dbn files under {raw_dir}")

    log(f"Reading headers of {len(paths)} raw files under {raw_dir} ...")
    files, unreadable = [], []
    for p in paths:
        try:
            files.append(read_header(p))
        except Exception as e:  # noqa: BLE001 — reported, then fatal below
            unreadable.append({"path": p, "error": f"{type(e).__name__}: {e}"})
    if unreadable:
        raise IngestError("could not read the DBN header of: "
                          + "; ".join(f"{u['path']} ({u['error']})" for u in unreadable))

    unidentified = [f for f in files if not f.roots]
    for f in unidentified:
        log(f"  WARNING: cannot tell which contract {f.path} holds "
            f"(symbols {list(f.unidentified_symbols or f.symbols)[:5]}); skipped")
    other_roots = sorted({r for f in files for r in f.roots} - {symbol})

    plans = plan_outputs(files, symbol)
    if not plans:
        found = sorted({r for f in files for r in f.roots})
        raise IngestError(f"no raw file's metadata names root {symbol}; "
                          f"roots found: {found}")

    out_sym = os.path.join(os.path.abspath(out_dir), symbol)
    manifest = {
        "symbol": symbol,
        "ingest_version": INGEST_VERSION,
        "ingested_on": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "raw_dir": raw_dir,
        "outputs": {},
        "files_for_other_roots": other_roots,
        "unidentified_files": [f.describe(raw_dir) for f in unidentified],
    }

    targets = {kind: os.path.join(out_sym, f"{kind}.parquet") for kind in plans}
    if not dry_run and not force:
        exists = [p for p in targets.values() if os.path.exists(p)]
        if exists:
            raise IngestError(f"already exist: {exists}; pass --force to replace them")
    if not dry_run and os.path.isdir(out_sym):
        # An earlier run's output that this run would not rewrite would sit
        # next to a manifest that no longer mentions it. Don't delete data —
        # make the person decide.
        stale = sorted(n for n in os.listdir(out_sym) if n.endswith(".parquet")
                       and os.path.join(out_sym, n) not in targets.values())
        if stale:
            raise IngestError(
                f"{out_sym} holds {stale} from an earlier run, but no raw file in "
                f"{raw_dir} produces them now. Move or delete them, then re-run.")

    # Every output is written to <name>.partial and verified; only when all of
    # them pass are they renamed into place, followed by the manifest. A
    # failure while decoding or checking leaves the previous outputs untouched.
    staged: Dict[str, str] = {}
    try:
        for kind, plan in sorted(plans.items()):
            out_path = targets[kind]
            log(f"\n{kind}: {len(plan.files)} file(s), schema {plan.schema} → {out_path}")
            for g in plan.coverage_gaps:
                log(f"  WARNING: no file covers {g['from']} → {g['to']}")
            for d in plan.duplicates_skipped:
                log(f"  skipped identical copy {d['path']}")
            entry = {
                "path": os.path.relpath(out_path, os.path.abspath(out_dir)),
                "schema": plan.schema,
                "dataset": plan.files[0].dataset,
                "stype_in": plan.files[0].stype_in,
                "series": list(_series_for(plan.files[0], symbol)),
                "source_files": [f.describe(raw_dir) for f in plan.files],
                "duplicates_skipped": [
                    {k: os.path.relpath(v, raw_dir) for k, v in d.items()}
                    for d in plan.duplicates_skipped],
                "coverage_gaps": [
                    {**g, "after": os.path.relpath(g["after"], raw_dir),
                     "before": os.path.relpath(g["before"], raw_dir)}
                    for g in plan.coverage_gaps],
            }
            manifest["outputs"][kind] = entry
            if dry_run:
                continue
            os.makedirs(out_sym, exist_ok=True)
            tmp = out_path + ".partial"
            staged[kind] = tmp
            per_file = write_output(plan, tmp, symbol, chunk_rows=chunk_rows)
            rows = sum(p["rows"] for p in per_file)
            for src, stats in zip(entry["source_files"], per_file):
                src.update({k: v for k, v in stats.items() if k != "path"})
            _flag_record_overlaps(entry, log)
            entry["rows"] = rows
            entry["checks_passed"] = verify_output(tmp, kind, expected_rows=rows)
            log(f"  decoded {rows:,} rows; checks passed")

        if not dry_run:
            man_path = os.path.join(out_sym, "ingest_manifest.json")
            staged["__manifest__"] = man_path + ".partial"
            with open(staged["__manifest__"], "w") as fh:
                json.dump(manifest, fh, indent=2)
            # Renames are atomic one by one, not as a set; the manifest goes
            # last, so it never describes outputs that are not yet in place.
            for kind, tmp in staged.items():
                if kind != "__manifest__":
                    os.replace(tmp, targets[kind])
            os.replace(staged["__manifest__"], man_path)
            staged.clear()
            log(f"\nwrote {len(targets)} output(s) and ingest_manifest.json to {out_sym}")
    finally:
        for tmp in staged.values():
            if os.path.exists(tmp):
                os.remove(tmp)
    return manifest


def _flag_record_overlaps(entry: dict, log) -> None:
    """Files whose headers don't overlap can still hold records that do
    (a file's header range and its records' ts_event range differ). That is
    not an error at this stage, but piece B must know where to look for
    duplicates across the file boundary."""
    srcs = [s for s in entry["source_files"] if s.get("ts_event_min")]
    overlaps = []
    for a, b in zip(srcs, srcs[1:]):
        if b["ts_event_min"] <= a["ts_event_max"]:
            overlaps.append({"earlier": a["path"], "later": b["path"],
                             "earlier_max": a["ts_event_max"],
                             "later_min": b["ts_event_min"]})
            log(f"  NOTE: records of {b['path']} start at {b['ts_event_min']}, "
                f"before {a['path']} ends at {a['ts_event_max']}")
    entry["record_time_overlaps"] = overlaps


def check_existing(symbol: str, out_dir: str, log=print) -> None:
    """Re-run verify_output on every parquet already in data/<SYMBOL>/."""
    out_sym = os.path.join(os.path.abspath(out_dir), symbol.upper())
    found = sorted(n for n in os.listdir(out_sym) if n.endswith(".parquet")) \
        if os.path.isdir(out_sym) else []
    if not found:
        raise IngestError(f"no parquet files in {out_sym}")
    for name in found:
        kind = name[: -len(".parquet")]
        for line in verify_output(os.path.join(out_sym, name), kind):
            log(f"  {name}: {line}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbol", required=True, help="contract root, e.g. NG")
    ap.add_argument("--raw-dir", default=os.path.join(_ROOT, "data", "raw"),
                    help="folder holding the Databento downloads (searched recursively)")
    ap.add_argument("--out-dir", default=os.path.join(_ROOT, "data"),
                    help="writes <out-dir>/<SYMBOL>/... (default data/)")
    ap.add_argument("--force", action="store_true",
                    help="replace outputs that already exist")
    ap.add_argument("--dry-run", action="store_true",
                    help="identify and check the raw files, write nothing")
    ap.add_argument("--check", action="store_true",
                    help="only verify outputs already in <out-dir>/<SYMBOL>/")
    args = ap.parse_args(argv)
    try:
        if args.check:
            check_existing(args.symbol, args.out_dir)
        else:
            m = ingest(args.symbol, args.raw_dir, args.out_dir,
                       force=args.force, dry_run=args.dry_run)
            if args.dry_run:
                print("\n" + json.dumps(m, indent=2))
    except IngestError as e:
        print(f"\nINGEST FAILED: {e}", file=sys.stderr)
        return 1
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
