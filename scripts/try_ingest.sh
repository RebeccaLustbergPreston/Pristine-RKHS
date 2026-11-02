#!/usr/bin/env bash
# Try ingestion on raw Databento downloads that live on your own machine.
# Nothing is uploaded anywhere: it reads the folder you point it at and
# writes parquet files under data/<TICKER>/ in this repo (gitignored).
#
#   scripts/try_ingest.sh NG ~/Downloads/box-data
#
# Steps: set up .venv if missing → list every raw file by its header →
# dry run for the ticker → real run → re-check the outputs.
set -euo pipefail

TICKER="${1:?usage: scripts/try_ingest.sh TICKER RAW_DIR}"
RAW_DIR="${2:?usage: scripts/try_ingest.sh TICKER RAW_DIR}"
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "== creating .venv (first run only)"
  python3 -m venv .venv
  .venv/bin/pip install -q databento==0.89.0 pandas==3.0.1 pyarrow==23.0.1 numpy
fi
PY=.venv/bin/python

echo; echo "== 1/4 what is in $RAW_DIR (read from each file's header)"
$PY ingest.py --list --raw-dir "$RAW_DIR"

echo; echo "== 2/4 dry run for $TICKER (writes nothing)"
$PY ingest.py --symbol "$TICKER" --raw-dir "$RAW_DIR" --dry-run > "/tmp/ingest_${TICKER}_plan.json"
echo "plan saved to /tmp/ingest_${TICKER}_plan.json"

echo; echo "== 3/4 ingest $TICKER → data/$TICKER/"
$PY ingest.py --symbol "$TICKER" --raw-dir "$RAW_DIR"

echo; echo "== 4/4 re-check data/$TICKER/"
$PY ingest.py --symbol "$TICKER" --check

echo; echo "All done. Send back data/$TICKER/ingest_manifest.json (small, no market data in it)."
