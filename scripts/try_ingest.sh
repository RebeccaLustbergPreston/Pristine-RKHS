#!/usr/bin/env bash
# Try ingestion on raw Databento downloads that live on your own machine.
# Nothing is uploaded anywhere: it reads the folder you point it at and
# writes parquet files under data/<TICKER>/ in this repo (gitignored).
#
#   scripts/try_ingest.sh NG ~/Downloads/box-data
#   FORCE=1 scripts/try_ingest.sh NG ~/Downloads/box-data   # replace earlier output
#
# Steps: make sure .venv can run ingest.py → list every raw file by its
# header → dry run for the ticker → real run → re-check the outputs.
# (Windows: see docs/ingestion_format.md for the equivalent commands.)
set -euo pipefail

usage="usage: [FORCE=1] scripts/try_ingest.sh TICKER RAW_DIR"
TICKER="${1:?$usage}"
RAW_DIR="${2:?$usage}"
[ -d "$RAW_DIR" ] || { echo "no such folder: $RAW_DIR" >&2; exit 1; }
RAW_DIR="$(CDPATH= cd -- "$RAW_DIR" && pwd)"          # absolute, before we cd away
cd "$(dirname "$0")/.."

FORCE_FLAG=""
if [ "${FORCE:-0}" = "1" ]; then FORCE_FLAG="--force"; fi

# pandas 3 needs Python 3.11+; macOS's built-in python3 is often 3.9.
PY_SYS="${PYTHON:-python3}"
if ! "$PY_SYS" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
  echo "Need Python 3.11 or newer (found: $("$PY_SYS" --version 2>&1))." >&2
  echo "Install one (e.g. 'brew install python@3.13') and re-run with PYTHON=python3.13." >&2
  exit 1
fi

PY=.venv/bin/python
if ! [ -x "$PY" ] || ! "$PY" -c 'import databento, pandas, pyarrow, zstandard' 2>/dev/null; then
  echo "== setting up .venv for ingest.py"
  [ -x "$PY" ] || "$PY_SYS" -m venv .venv
  # Only what ingest.py needs, at the versions pinned in requirements.txt.
  pins=$(grep -E '^(databento|databento-dbn|zstandard|pandas|pyarrow|numpy)==' requirements.txt) \
    || { echo "could not find the pins in requirements.txt" >&2; exit 1; }
  # shellcheck disable=SC2086
  "$PY" -m pip install -q $pins
fi

echo; echo "== 1/4 what is in $RAW_DIR (read from each file's header)"
"$PY" ingest.py --list --raw-dir "$RAW_DIR"

PLAN="data/ingest_plan_${TICKER}.json"
mkdir -p data
echo; echo "== 2/4 dry run for $TICKER (writes nothing but the plan)"
"$PY" ingest.py --symbol "$TICKER" --raw-dir "$RAW_DIR" --dry-run $FORCE_FLAG > "$PLAN.tmp"
mv "$PLAN.tmp" "$PLAN"
echo "plan saved to $PLAN"

echo; echo "== 3/4 ingest $TICKER → data/$TICKER/"
"$PY" ingest.py --symbol "$TICKER" --raw-dir "$RAW_DIR" $FORCE_FLAG

echo; echo "== 4/4 re-check data/$TICKER/"
"$PY" ingest.py --symbol "$TICKER" --check

echo; echo "All done. Send back data/$TICKER/ingest_manifest.json (small, no market data in it)."
