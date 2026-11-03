# What ingestion hands to cleaning (piece A → piece B)

`ingest.py` turns raw Databento downloads into one file per schema per
contract. This note is the contract between ingestion (A) and cleaning (B)
— proposed by A, to be agreed with B in week one.

```bash
python ingest.py --symbol NG                       # reads data/raw/, writes data/NG/
python ingest.py --symbol NG --raw-dir ~/dl --dry-run   # what would happen, writes nothing
python ingest.py --symbol NG --check               # re-verify data/NG/*.parquet
python ingest.py --list --raw-dir ~/dl             # what every raw file holds, by header
```

To try it on data that lives only on your machine (nothing is uploaded):
`scripts/try_ingest.sh NG ~/path/to/downloads` (macOS/Linux, Python 3.11+).
It sets up `.venv` with just what ingestion needs, lists the files, does a
dry run (plan saved to `data/ingest_plan_NG.json`), ingests, and re-checks.
`FORCE=1` replaces an earlier run's output.

On Windows (PowerShell), the same steps by hand:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install databento==0.89.0 databento-dbn==0.70.0 zstandard==0.25.0 pandas==3.0.1 pyarrow==23.0.1 numpy==2.4.3
.venv\Scripts\python ingest.py --list --raw-dir C:\path\to\downloads
.venv\Scripts\python ingest.py --symbol NG --raw-dir C:\path\to\downloads --dry-run
.venv\Scripts\python ingest.py --symbol NG --raw-dir C:\path\to\downloads
.venv\Scripts\python ingest.py --symbol NG --check
```

## Input

Any folder tree of `*.dbn.zst` / `*.dbn` files (default `data/raw/`). How it
is organised does not matter. Each file is identified **only from its DBN
header**: dataset, schema, symbols, symbology and the instrument mappings.
File and folder names never decide anything, so a file called
`CL_december` that holds NG trades is ingested as NG trades. Names are
cross-checked, though: if a file or folder name mentions a ticker that its
header does not hold, the run prints a warning and lists it under
`name_mismatches` in the manifest, so a mislabelled download gets noticed
and renamed. A name only counts as a ticker if some header in the same
folder holds that ticker, so `CL_december` is flagged when crude is also in
the folder, and words like `trades` or `dec` never are. Every output
carries the ticker in its path (`data/NG/...`) and in the manifest.

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

Definitions and statistics files in older DBN versions (v1/v2) are upgraded
to v3 before decoding, so every output has one column layout whatever year
it was downloaded in, and the old 32-bit "undefined" quantity can't survive
as a number. (Other schemas' records did not change between versions.)

Integer "undefined" values in **definitions** are kept as Databento writes
them, e.g. `contract_multiplier = 2147483647`, `maturity_year = 65535`.
Prices, `expiration` and other timestamps are already NaN/NaT when
undefined. B should treat those integer maxima as missing.

Prices are float, in contract units, not scaled. Native Databento dtypes are
kept (`instrument_id` is `uint32`, `size` is `uint32`).

## CSV trades (NG, HG)

NG and HG trades exist only as a Databento batch job encoded as CSV, pulled
as the parent (`NG.FUT`): every contract month and every spread, in daily
`.trades.csv.zst` files. `ingest_csv.py`, called from `ingest.py`, turns that
into the same `data/<SYM>/trades.parquet` a `NG.v.0` DBN pull would give:

- The job is identified from its `metadata.json` (encoding, schema, parent
  symbol), and every file is hashed against the job's `manifest.json`.
- Which contract to keep on each date comes from the roll Databento records
  in the header of the continuous-series files being ingested alongside (the
  `NG.v.0` minute bars). A row is kept if its `instrument_id` is the one
  mapped for the UTC date of its `ts_recv`; `symbol` becomes `NG.v.0`.
- The result is checked against Databento's own minute bars: bars rebuilt
  from the kept trades, bucketed by `ts_recv`, must equal `ohlcv1m.parquet`
  bar for bar, or the run stops. On the real data: HG 512,262 of 512,262
  bars, NG 469,616 of 469,616.
- The manifest's `trades` entry carries `converted_from_csv` (the job, every
  contract the roll used and from which date) and the rows read vs kept.
- A DBN trades download for the same root always wins; the CSV job is then
  listed under `csv_jobs_not_used`.

Without a continuous-series file for the root in the same raw folder,
ingestion stops rather than estimate the roll.

## What ingestion does *not* do — all of it is cleaning

No row of the requested contract is dropped, sorted, de-duplicated,
adjusted or filled. (A file bundling several contracts contributes only the
requested contract's rows; a file whose header names no recognisable
contract is skipped with a warning and listed under `unidentified_files`.)
Rows appear in the order the files' headers start, and within a file in the order Databento
wrote them. `session_date` is not added (rule f). Casting to the standard's
dtypes (`instrument_id` → int32, `size` → float64) is left to B so the
cleaning report can count anything that would not survive the cast.

## When ingestion stops instead of guessing

- Two *different* files for one output cover overlapping header periods and
  could hold the same records. Byte-identical copies are skipped and
  recorded instead. Overlap is fine when the files' header mappings name
  disjoint instruments (a batch job split by symbol, or one download per
  contract), or when both are listed in the same Databento `manifest.json`
  (a batch job split by size) — so keep each job's `manifest.json` with its
  files.
- A file's sha256 does not match the one in its job's `manifest.json`.
- Files for one output come from different datasets, or pull different
  series for the root (`NG.v.0` mixed with `NG.c.0`).
- A row's `instrument_id` has no mapping in its file's own metadata on
  that date — it cannot be attributed to a contract.
- A file's header cannot be read, a file is truncated (an interrupted
  download: the zstd stream stops mid-frame or the records stop mid-record —
  Databento itself only warns), or two files decode to incompatible column
  layouts. Live recordings (mixed schemas or `ts_out`) are refused.
- A file bundling several roots has rows for a symbol with no single root
  (a user-defined or inter-commodity spread). Dropping them would make the
  output depend on how the download was bundled; pull that root on its own.
- `data/<SYM>/` holds a parquet from an earlier run that this run would not
  rewrite. It is not deleted; move it yourself.
- An output already exists and `--force` was not given.
- A written file fails `verify_output` (no rows, missing required column,
  non-UTC timestamp, missing `instrument_id` / `symbol` / `ts_event`).

`--dry-run` applies all of the checks above that don't need decoding,
including the existing-output ones, so a clean dry run predicts a clean
real run up to decoding.

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
2. `DataLoader` still reads crude's old names (`data/trades_CL_full.parquet`,
   …), so `DataLoader(symbol="NG")` cannot see `data/NG/*` yet, and
   `DataLoader.ohlcv()` expects `ts_event` as the *index* where the standard
   (and this output) has a column. Both are piece C's to reconcile.
3. Not yet shown on real data: that crude's own raw downloads ingest and
   match `data/trades_CL_full.parquet` row for row, and that each file of a
   real split batch job carries its own start/end in its header. Run
   `python ingest.py --list` on one real job to confirm the latter.
