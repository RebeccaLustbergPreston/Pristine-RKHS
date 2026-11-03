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
two different files that could hold the same records for the same period,
a download that is truncated or fails its job manifest's hash, files for one
output from different datasets or different series (``NG.v.0`` mixed with
``NG.c.0``), mismatched column layouts, or a written file that fails the
checks in ``verify_output``.

Trades that are not one continuous DBN series — a parent pull (NG, HG as CSV;
half of ES as DBN), or a CSV export of a continuous pull (CL, GC) — are handled
by ``ingest_csv.py``: narrowed to the continuous series of the contract's minute
bars by Databento's own roll, and checked against those bars. Continuous DBN
trades of one series take the ordinary path and win over any CSV.
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
_RE_STRATEGY = re.compile(r"^([A-Z0-9]+):[A-Z0-9]{2}\s")

_CHUNK_ROWS = 2_000_000
# Definitions rows are ~70 columns with many strings; decode them in smaller
# chunks so a whole-range parent pull doesn't need several GB at once.
_CHUNK_ROWS_BY_SCHEMA = {"definition": 200_000, "mbp-10": 250_000, "mbo": 1_000_000}

# Record layouts that changed between DBN v1/v2 and v3. Only these need to be
# rewritten as v3 before decoding; for every other schema the bytes are the
# same and an upgraded copy would only cost disk.
_SCHEMAS_CHANGED_SINCE_V1 = {"definition", "statistics"}
_SCRATCH_PREFIX = ".ingest-"


class IngestError(RuntimeError):
    """Inputs do not line up. Raised instead of patching around the problem."""


# ── Identifying a file from its own metadata ─────────────────────────────────

def root_of(symbol: str) -> Optional[str]:
    """
    Contract root of a Databento symbol, or None if it cannot be told.

        NG.v.0 → NG      (continuous)       NG.FUT → NG   (parent)
        NGZ3   → NG      (outright)         NGZ3-NGF4 → NG (calendar spread)
        LOZ3 C4800 → LO  (option)           4242 → None   (instrument id)
        NG:BF H4-J4-K4 → NG  (CME exchange-defined strategy)
        UD:1V: VT 0102999 → None  (user-defined spread, may mix roots)
    """
    s = str(symbol).strip().upper()
    for rx in (_RE_CONTINUOUS, _RE_PARENT):
        m = rx.match(s)
        if m:
            return m.group(1)
    m = _RE_STRATEGY.match(s)
    if m and m.group(1) != "UD":
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
    instrument_ids: Tuple[int, ...] = ()   # from the header's symbol mappings

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
    """SHA-256 of the file, and — for .zst files — proof that every zstd frame
    in it is complete. A download cut short at a compressed block boundary
    decodes cleanly into fewer records, and Databento's reader does not notice
    (python-zstandard returns the whole blocks it has), so check here."""
    import zstandard

    h = hashlib.sha256()
    with open(path, "rb") as f:                 # by content, not by extension
        magic = f.read(4)
    is_zst = magic == b"\x28\xb5\x2f\xfd" or (                  # zstd frame
        len(magic) == 4 and magic[1:] == b"\x2a\x4d\x18" and 0x50 <= magic[0] <= 0x5f)
    dctx = zstandard.ZstdDecompressor()
    obj, fed = dctx.decompressobj(), False
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
            if not is_zst:
                continue
            # Feed small slices so a highly compressed block can't balloon memory.
            for i in range(0, len(block), 64 << 10):
                piece = block[i:i + (64 << 10)]
                while piece:
                    try:
                        obj.decompress(piece)
                    except zstandard.ZstdError as e:
                        raise IngestError(
                            f"{path}: corrupt zstd data ({e}) — re-download it") from e
                    fed = True
                    if obj.eof:                  # frame finished; more may follow
                        piece = obj.unused_data
                        obj, fed = dctx.decompressobj(), False
                    else:
                        piece = b""
    if is_zst and fed and not obj.eof:
        raise IngestError(f"{path}: the zstd stream stops part-way through a frame — "
                          "the download is truncated; re-download it")
    return h.hexdigest()


def load_batch_manifests(raw_dir: str) -> Dict[str, dict]:
    """
    Databento batch jobs ship a ``manifest.json`` listing each file with its
    sha256. Map every listed file (absolute path) to its job and expected hash.
    Used to catch corrupt downloads and to recognise the parts of one job.
    """
    out: Dict[str, dict] = {}
    for dirpath, dirnames, files in os.walk(raw_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(_SCRATCH_PREFIX)]
        if "manifest.json" not in files:
            continue
        mpath = os.path.join(dirpath, "manifest.json")
        try:
            with open(mpath) as fh:
                entries = json.load(fh).get("files", [])
        except (OSError, ValueError, AttributeError):
            continue                       # not a Databento job manifest
        for pos, e in enumerate(entries if isinstance(entries, list) else []):
            if not isinstance(e, dict) or not isinstance(e.get("filename"), str):
                continue                   # not shaped like a Databento entry
            name, digest = e["filename"], str(e.get("hash", ""))
            out[os.path.abspath(os.path.join(dirpath, name))] = {
                "job": mpath,
                "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
                "position": pos,
            }
    return out


def _enum_str(v) -> str:
    return str(getattr(v, "value", v))


def read_header(path: str) -> RawFile:
    """Identify a raw file from its DBN header alone — never from its name."""
    import databento as db

    md = db.DBNStore.from_file(path).metadata
    if md.schema is None:
        raise IngestError("mixed-schema file (live recording); not supported")
    if md.ts_out:
        raise IngestError("file carries ts_out (live recording); not supported")
    ids = set()
    for intervals in (md.mappings or {}).values():
        for iv in intervals:
            if str(iv["symbol"]).isdigit():
                ids.add(int(iv["symbol"]))
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
        instrument_ids=tuple(sorted(ids)),
    )


def find_raw_files(raw_dir: str) -> List[str]:
    out = []
    for dirpath, dirnames, files in os.walk(raw_dir):
        # Skip ingest's own scratch folders, in case --raw-dir covers data/.
        dirnames[:] = [d for d in dirnames if not d.startswith(_SCRATCH_PREFIX)]
        for name in files:
            if name.endswith(".dbn.zst") or name.endswith(".dbn"):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


def _read_headers(paths: Sequence[str]) -> Tuple[List[RawFile], List[dict]]:
    files, unreadable = [], []
    for p in paths:
        try:
            files.append(read_header(p))
        except ImportError:
            raise                          # a broken environment, not a bad file
        except Exception as e:  # noqa: BLE001 — reported by the caller
            unreadable.append({"path": p, "error": f"{type(e).__name__}: {e}"})
    return files, unreadable


def _name_tokens(path: str, raw_dir: str) -> set:
    rel = os.path.relpath(path, raw_dir)
    return {t.upper() for t in re.split(r"[^A-Za-z0-9]+", rel) if t}


def name_mismatches(files: Sequence[RawFile], raw_dir: str) -> List[dict]:
    """
    Files whose name or folder mentions a ticker their header does not hold.

    The header still decides what a file is — this only makes a misleading
    name visible, so whoever organised the download can fix it. Only tickers
    that some header in this folder actually contains count as tickers, so
    words like "trades" or "dec" never trigger it.
    """
    known = {r for f in files for r in f.roots}
    out = []
    for f in files:
        named = sorted(_name_tokens(f.path, raw_dir) & known)
        if named and not set(named) & set(f.roots):
            out.append({"path": os.path.relpath(f.path, raw_dir),
                        "name_says": named, "header_says": list(f.roots)})
    return out


def list_raw(raw_dir: str, log=print) -> List[RawFile]:
    """Print what every raw file holds, according to its header. Writes nothing."""
    raw_dir = os.path.abspath(raw_dir)
    paths = find_raw_files(raw_dir)
    if not paths:
        raise IngestError(f"no .dbn.zst / .dbn files under {raw_dir}")
    files, unreadable = _read_headers(paths)
    log(f"{len(paths)} raw file(s) under {raw_dir}\n")
    log(f"{'ticker':<8}{'schema':<12}{'series':<16}{'start':<12}{'end':<12}"
        f"{'dbn':<5}{'MB':>9}  file")
    for f in sorted(files, key=lambda x: (x.roots, x.schema, x.start_ns)):
        series = ",".join(f.symbols)[:15]
        end = _ns_to_iso(f.end_ns)[:10] if f.end_ns is not None else "?"
        log(f"{','.join(f.roots) or '??':<8}{f.schema:<12}{series:<16}"
            f"{_ns_to_iso(f.start_ns)[:10]:<12}{end:<12}v{f.dbn_version:<4}"
            f"{f.n_bytes / 1e6:>9.1f}  {os.path.relpath(f.path, raw_dir)}")
    for u in unreadable:
        log(f"{'UNREADABLE':<69}{os.path.relpath(u['path'], raw_dir)}  ({u['error']})")
    for mm in name_mismatches(files, raw_dir):
        log(f"\nWARNING: {mm['path']} is named for {mm['name_says']} but holds "
            f"{mm['header_says']}")
    from ingest_csv import find_csv_jobs
    for j in find_csv_jobs(raw_dir):
        log(f"{','.join(j.roots) or '??':<8}{j.schema:<12}{','.join(j.symbols)[:15]:<16}"
            f"{_ns_to_iso(j.start_ns)[:10]:<12}"
            f"{_ns_to_iso(j.end_ns)[:10] if j.end_ns else '?':<12}csv  "
            f"{len(j.files):>5} files  {os.path.relpath(j.folder, raw_dir)}/")
    return files


# ── Grouping and the alignment checks ────────────────────────────────────────

@dataclass
class OutputPlan:
    kind: str
    schema: str
    files: List[RawFile] = field(default_factory=list)
    duplicates_skipped: List[dict] = field(default_factory=list)
    coverage_gaps: List[dict] = field(default_factory=list)


def plan_outputs(files: Sequence[RawFile], symbol: str,
                 batch: Optional[Dict[str, dict]] = None) -> Dict[str, OutputPlan]:
    """
    Group the files for ``symbol`` by output and check that they line up.

    Raises IngestError when files for one output disagree on dataset, on the
    series pulled for this root, or cover overlapping periods with different
    contents. Byte-identical copies are skipped and recorded.

    Two files may cover the same period when they cannot hold the same
    records: their header mappings name disjoint instrument ids (a batch job
    split by symbol, or one download per contract), or they are listed in the
    same Databento job ``manifest.json`` (a job split by size).
    """
    batch = batch or {}
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
            want = batch.get(f.path, {}).get("sha256")
            if want and want != f.sha256:
                raise IngestError(
                    f"{f.path}: sha256 {f.sha256[:12]}… does not match the "
                    f"{want[:12]}… in its job's manifest.json — the download is "
                    "incomplete or corrupt; re-download it")
        seen: Dict[str, RawFile] = {}
        unique = []
        # Same start (size-split parts): the job manifest's order, then a
        # natural sort so part 2 comes before part 10.
        for f in sorted(plan.files, key=lambda x: (
                x.start_ns, batch.get(x.path, {}).get("position", 0), _natural(x.path))):
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
        series = {(_stype_label(f), _series_for(f, symbol)) for f in plan.files}
        if len(series) > 1:
            raise IngestError(
                f"{plan.kind}: files pull different series for {symbol} "
                f"{sorted(series)} — e.g. a volume roll mixed with a calendar "
                "roll. Pick one and remove the others; " + _list_paths(plan.files))

        # 3. No two files that could hold the same records may cover the
        #    same period. Files are sorted by start, so compare each with every
        #    earlier file still open at its start.
        inf = np.iinfo(np.int64).max
        for i, b in enumerate(plan.files):
            for a in plan.files[:i]:
                a_end = a.end_ns if a.end_ns is not None else inf
                if b.start_ns >= a_end or _may_coexist(a, b, batch):
                    continue
                raise IngestError(
                    f"{plan.kind}: {a.path} covers {_ns_to_iso(a.start_ns)} → "
                    f"{_ns_to_iso(a.end_ns)} and {b.path} covers "
                    f"{_ns_to_iso(b.start_ns)} → {_ns_to_iso(b.end_ns)}, for the "
                    "same instruments. They would duplicate or contradict each "
                    "other's records. If they are parts of one Databento batch job, "
                    "put the job's manifest.json next to them; if one is a "
                    "re-download, keep only one copy.")
        # Gaps: periods no file covers, measured against the union so far.
        covered_to = None
        prev = None
        for f in plan.files:
            if covered_to is not None and f.start_ns > covered_to:
                plan.coverage_gaps.append(
                    {"from": _ns_to_iso(covered_to), "to": _ns_to_iso(f.start_ns),
                     "after": prev.path, "before": f.path})
            f_end = f.end_ns if f.end_ns is not None else inf
            if covered_to is None or f_end > covered_to:
                covered_to, prev = f_end, f
    return plans


def _natural(path: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", path)]


def _may_coexist(a: RawFile, b: RawFile, batch: Dict[str, dict]) -> bool:
    """Can two files covering the same period both be kept?"""
    ja, jb = batch.get(a.path, {}).get("job"), batch.get(b.path, {}).get("job")
    if ja is not None and ja == jb:
        return True                         # size-split parts of one job
    if a.instrument_ids and b.instrument_ids:
        return not set(a.instrument_ids) & set(b.instrument_ids)
    return False


def _series_for(f: RawFile, symbol: str) -> Tuple[str, ...]:
    """Header symbols that belong to ``symbol`` (other roots in a shared file
    don't count). Outright contracts collapse to the root, so pulling NGZ3
    in one month and NGF4 in the next is still one series."""
    own = [s for s in f.symbols if root_of(s) == symbol]
    if f.stype_in == "raw_symbol" and "ALL_SYMBOLS" not in f.symbols:
        return (f"{symbol} outrights",)
    return tuple(sorted(own)) or (f"{symbol} via mappings",)


def _stype_label(f: RawFile) -> str:
    """Old DBN v1 files say ``smart`` for what is now ``continuous`` or
    ``parent``; read it off the symbols so the same series compares equal."""
    if f.stype_in != "smart":
        return f.stype_in
    if any(_RE_CONTINUOUS.match(s.upper()) for s in f.symbols):
        return "continuous"
    if any(_RE_PARENT.match(s.upper()) for s in f.symbols):
        return "parent"
    return "smart"


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
    upgraded = None
    if f.dbn_version < 3 and f.schema in _SCHEMAS_CHANGED_SINCE_V1:
        upgraded = path = os.path.join(scratch_dir, f"{f.sha256}.v3.dbn")
        _upgrade_to_v3(f.path, path)
    store = db.DBNStore.from_file(path)
    if upgraded and int(store.metadata.version) != 3:
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
    if upgraded:
        del store                           # release the handle (Windows can't delete open files)
        os.remove(upgraded)                 # free the disk before the next file


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
    scratch = tempfile.TemporaryDirectory(prefix=_SCRATCH_PREFIX, dir=os.path.dirname(tmp))
    try:
        for f in plan.files:
            n, t0, t1 = 0, None, None
            # Databento only *warns* when a file is cut short; an interrupted
            # download would otherwise be ingested as if it were complete.
            with warnings.catch_warnings():
                warnings.simplefilter("error", BentoWarning)
                try:
                    for df in _frames(f, symbol, chunk_rows, scratch.name):
                        if len(df) == 0:
                            continue        # an empty chunk must not fix the schema
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
                except EOFError as e:   # Python 3.14's zstd reader on a cut stream
                    raise IngestError(f"{f.path}: compressed stream ends early "
                                      f"({e}) — re-download it") from e
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

    These are the properties every loader and cleaning step relies on, and
    that crude's files are expected to have (not yet confirmed against the
    real crude parquets): the file opens, has rows, carries the required
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
           log=print, skip_schemas: Sequence[str] = ()) -> dict:
    symbol = symbol.upper()
    raw_dir = os.path.abspath(raw_dir)
    if not os.path.isdir(raw_dir):
        raise IngestError(f"raw directory {raw_dir} does not exist")
    paths = find_raw_files(raw_dir)
    if not paths:
        raise IngestError(f"no .dbn.zst / .dbn files under {raw_dir}")

    log(f"Reading headers of {len(paths)} raw files under {raw_dir} ...")
    files, unreadable = _read_headers(paths)
    if unreadable:
        raise IngestError("could not read the DBN header of: "
                          + "; ".join(f"{u['path']} ({u['error']})" for u in unreadable))

    skipped = [f for f in files if f.schema in skip_schemas]
    if skipped:
        log(f"  skipping {len(skipped)} file(s) of schema {sorted(set(skip_schemas))}")
        files = [f for f in files if f.schema not in skip_schemas]
    unidentified = [f for f in files if not f.roots]
    for f in unidentified:
        log(f"  WARNING: cannot tell which contract {f.path} holds "
            f"(symbols {list(f.unidentified_symbols or f.symbols)[:5]}); skipped")
    other_roots = sorted({r for f in files for r in f.roots} - {symbol})
    mismatches = name_mismatches(files, raw_dir)
    for mm in mismatches:
        log(f"  WARNING: {mm['path']} is named for {mm['name_says']} but its "
            f"header says {mm['header_says']}; going by the header")

    batch = load_batch_manifests(raw_dir)
    from ingest_csv import (check_against_bars, finish_plan, select_trades_sources,
                            write_narrowed_trades)
    files, narrowed, trades_not_used = select_trades_sources(files, raw_dir, symbol,
                                                             log=log)
    plans = plan_outputs(files, symbol, batch=batch)
    if narrowed is not None:
        narrowed = finish_plan(narrowed, plans, symbol, batch)
    if not plans and narrowed is None:
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
        "name_mismatches": mismatches,
        "schemas_skipped": sorted(set(skip_schemas)),
        "trades_sources_not_used": trades_not_used,
    }

    targets = {kind: os.path.join(out_sym, f"{kind}.parquet") for kind in plans}
    if narrowed is not None:
        targets[narrowed.kind] = os.path.join(out_sym, f"{narrowed.kind}.parquet")
    # A dry run applies the same checks, so it predicts the real run.
    if not force:
        exists = [p for p in targets.values() if os.path.exists(p)]
        if exists:
            raise IngestError(f"already exist: {exists}; pass --force (FORCE=1 with scripts/try_ingest.sh) to replace them")
    if os.path.isdir(out_sym):
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
                "source_files": [
                    {**f.describe(raw_dir),
                     "batch_job": (os.path.relpath(batch[f.path]["job"], raw_dir)
                                   if f.path in batch else None)}
                    for f in plan.files],
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

        if narrowed is not None:
            cal = narrowed.calendar
            out_path = targets["trades"]
            log(f"\ntrades: {len(narrowed.sources)} source(s) narrowed to {cal.series} "
                f"by Databento's roll → {out_path}")
            entry = {
                "path": os.path.relpath(out_path, os.path.abspath(out_dir)),
                "schema": "trades",
                "dataset": cal.dataset,
                "stype_in": "continuous",
                "series": [cal.series],
                "narrowed": {"sources": [x.describe(raw_dir) for x in narrowed.sources],
                             "roll": cal.describe(raw_dir),
                             "rows_kept_if": "instrument_id is the one the roll maps "
                                             "for the UTC date of ts_recv"},
                "source_files": [],
            }
            manifest["outputs"]["trades"] = entry
            if not dry_run:
                os.makedirs(out_sym, exist_ok=True)
                tmp = out_path + ".partial"
                staged["trades"] = tmp
                per_file, counts = write_narrowed_trades(narrowed, tmp, symbol,
                                                         chunk_rows=chunk_rows)
                entry["source_files"] = [{**p, "path": os.path.relpath(p["path"], raw_dir)}
                                         for p in per_file]
                _flag_record_overlaps(entry, log)
                entry.update(counts)
                entry["rows"] = counts["rows_kept"]
                checks = verify_output(tmp, "trades", expected_rows=entry["rows"])
                if "ohlcv1m" in staged:
                    checks.append(check_against_bars(tmp, staged["ohlcv1m"], cal.series))
                else:
                    log(f"  WARNING: no {symbol} minute bars in this run, so the "
                        "narrowed trades were not checked against Databento's bars")
                entry["checks_passed"] = checks
                log(f"  kept {entry['rows']:,} of {counts['rows_read']:,} rows; "
                    "checks passed")

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
    ap.add_argument("--symbol", help="contract root, e.g. NG")
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
    ap.add_argument("--skip-schema", action="append", default=[], metavar="SCHEMA",
                    help="leave out files of this schema, e.g. mbp-10 (depth is "
                         "streamed into features, never written as one table)")
    ap.add_argument("--list", action="store_true",
                    help="print what every raw file holds (any ticker); write nothing")
    args = ap.parse_args(argv)
    if not args.list and not args.symbol:
        ap.error("--symbol is required (or use --list)")
    try:
        if args.list:
            files = list_raw(args.raw_dir)
            n_bad = len(find_raw_files(os.path.abspath(args.raw_dir))) - len(files)
            if n_bad:
                raise IngestError(f"{n_bad} raw file(s) could not be read (see above)")
        elif args.check:
            check_existing(args.symbol, args.out_dir)
        else:
            if args.dry_run:
                # Progress goes to stderr so stdout is the plan, as clean JSON.
                m = ingest(args.symbol, args.raw_dir, args.out_dir, force=args.force,
                           dry_run=True, log=lambda *a: print(*a, file=sys.stderr),
                           skip_schemas=args.skip_schema)
                print(json.dumps(m, indent=2))
                return 0
            ingest(args.symbol, args.raw_dir, args.out_dir, force=args.force,
                   skip_schemas=args.skip_schema)
    except IngestError as e:
        print(f"\nINGEST FAILED: {e}", file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"\nINGEST FAILED: {e}. Install the requirements: "
              "pip install -r requirements.txt", file=sys.stderr)
        return 1
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
