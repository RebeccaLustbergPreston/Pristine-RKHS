# What ingestion hands to cleaning (piece A → piece B)

`ingest.py` turns raw Databento downloads into one file per schema per
contract. This note is the contract between ingestion (A) and cleaning (B)
— proposed by A, to be agreed with B in week one.

```bash
python ingest.py --symbol NG                       # reads data/raw/, writes data/NG/
python ingest.py --symbol NG --raw-dir ~/dl --dry-run   # what would happen, writes nothing
python ingest.py --symbol NG --check               # re-verify data/NG/*.parquet
```

## Input

Any folder tree of `*.dbn.zst` / `*.dbn` files (default `data/raw/`). How it
is organised does not matter. Each file is identified **only from its DBN
header**: dataset, schema, symbols, symbology and the instrument mappings.
File and folder names are never read, so a file called `CL_december` that
holds NG trades is ingested as NG trades.

## Output

| File | From schema | Notes |
|---|---|---|
| `data/<SYM>/trades.parquet` | `trades` | |
| `data/<SYM>/ohlcv1m.parquet` | `ohlcv-1m` | |
| `data/<SYM>/definitions.parquet` | `definition` | usually pulled as `<SYM>.FUT`, so includes spreads |
| `data/<SYM>/statistics.parquet` | `statistics` | |
| `data/<SYM>/<schema>.parquet` | anything else | e.g. `mbp10.parquet` for depth (piece D) |
| `data/<SYM>/ingest_manifest.json` | — | every source file, its sha256, header range, actual `ts_event` range, row count, gaps, overlaps, checks passed |

Columns are exactly what `databento.DBNStore.to_df()` produces — the same
reader crude's files came from — with two representational changes:

1. `ts_recv` is a column, not the index. Every timestamp is a column,
   `datetime64[ns, UTC]`.
2. In statistics, Databento's "undefined quantity" sentinel (INT64_MAX) is
   a real missing value (`Int64` NA). Prices are already NaN when undefined.

Files in older DBN versions (v1/v2) are upgraded to v3 before decoding, so
every output has one column layout whatever year it was downloaded in, and
the old 32-bit "undefined" sentinels can't survive as numbers.

Prices are float, in contract units, not scaled. Native Databento dtypes are
kept (`instrument_id` is `uint32`, `size` is `uint32`).

## What ingestion does *not* do — all of it is cleaning

No row is dropped, sorted, de-duplicated, adjusted or filled. Rows appear in
the order the files' headers start, and within a file in the order Databento
wrote them. `session_date` is not added (rule f). Casting to the standard's
dtypes (`instrument_id` → int32, `size` → float64) is left to B so the
cleaning report can count anything that would not survive the cast.

## When ingestion stops instead of guessing

- Two *different* files for one output cover overlapping header periods
  (byte-identical copies are skipped and recorded instead).
- Files for one output come from different datasets, or pull different
  series for the root (`NG.v.0` mixed with `NG.c.0`).
- A row's `instrument_id` has no mapping in its file's own metadata on
  that date — it cannot be attributed to a contract.
- A file's header cannot be read, a file is truncated (an interrupted
  download — Databento itself only warns), or two files decode to
  incompatible column layouts.
- A file bundling several roots has rows for a symbol with no single root
  (a user-defined or inter-commodity spread). Dropping them would make the
  output depend on how the download was bundled; pull that root on its own.
- `data/<SYM>/` holds a parquet from an earlier run that this run would not
  rewrite. It is not deleted; move it yourself.
- A written file fails `verify_output` (no rows, missing required column,
  non-UTC timestamp, missing `instrument_id` / `symbol` / `ts_event`).

All outputs are staged as `.partial` and only renamed into place once every
one of them has passed, manifest last, so a failed run leaves the previous
outputs intact.

Options are filed under their own root: CL's options (`LO.OPT`) go to
`data/LO/`, not `data/CL/`. Linking a future to its options root is a
per-contract setting for piece C.

## Things B should look at in the manifest

- `coverage_gaps` — periods no file covers. Recorded, never filled.
- `record_time_overlaps` — adjacent files whose `ts_event` ranges overlap
  (a trade at 23:59:59.9 received after midnight lands in the next month's
  file). Rule c's de-duplication should look at these boundaries.

## Open questions for B

1. Does cleaning write back to `data/<SYM>/trades.parquet` in place, or to a
   separate path? In place is fine as long as the raw `.dbn.zst` files stay
   the source of truth — re-running ingestion restores the pre-clean file.
2. `DataLoader.ohlcv()` currently expects `ts_event` as the *index*; the
   standard (and this output) has it as a column. That is piece C's to
   reconcile.
