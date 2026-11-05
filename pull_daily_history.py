"""
Long daily history for F1: daily bars and settlement statistics, front and
second contract, for the eight contracts, June 2010 – December 2024.

    PY=~/Desktop/TradersAtSMU/.venv_databento/bin/python
    $PY pull_daily_history.py              # quote only
    $PY pull_daily_history.py --buy        # pull the bars, submit the statistics jobs
    $PY pull_daily_history.py --download   # fetch finished statistics jobs (rerun until done)

Needs the databento package (kept out of the project venv) and the API key
in ~/.databento_key. Quotes the total first and refuses to buy above
--max-cost. Daily bars are small and stream directly; statistics stream
too slowly (minutes per megabyte), so they go through batch jobs, billed
once and downloadable for 30 days. Files land in
data/raw/<ROOT>/daily_history/, each with a manifest (query, cost, records,
sha256). Same roll rule per contract as the tick data (CL/NG/HG by volume,
GC/ZC/ZT/ES by open interest, BTC by calendar). GLBX.MDP3 starts 6 June
2010; BTC futures from December 2017.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.abspath(__file__))
DATASET = "GLBX.MDP3"
ROLL = {"CL": "v", "NG": "v", "HG": "v", "GC": "n", "ZC": "n", "ZT": "n", "ES": "n", "BTC": "c"}
SCHEMAS = ("ohlcv-1d", "statistics")
START, END = "2010-06-06", "2025-01-01"
JOBS = Path(_ROOT, "data", "raw", "daily_history_jobs.json")


def _dir(root):
    d = Path(_ROOT, "data", "raw", root, "daily_history")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _query(sym, schema):
    return dict(dataset=DATASET, symbols=[sym], schema=schema, start=START, end=END,
                stype_in="continuous")


def _record(path, kw, cost, job_id=None):
    import databento as db
    n = len(db.DBNStore.from_file(path).to_ndarray())
    m = {"query": kw, "quoted_cost": cost, "records": n,
         "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
         "pulled": datetime.now(timezone.utc).isoformat()}
    if job_id:
        m["job_id"] = job_id
    Path(str(path) + ".manifest.json").write_text(json.dumps(m, indent=2))
    return n


def buy(c, quotes):
    jobs = json.loads(JOBS.read_text()) if JOBS.exists() else {}
    for root, sym, schema, kw, cost in quotes:
        path = _dir(root) / f"{sym}.{schema}.dbn.zst"
        if path.exists() or f"{sym}.{schema}" in jobs:
            print(f"  have {path.name}", flush=True)
            continue
        if schema == "ohlcv-1d":
            tmp = path.with_suffix(".part")
            c.timeseries.get_range(**kw, path=str(tmp))
            tmp.rename(path)
            print(f"  {path.name}: {_record(path, kw, cost):,} records", flush=True)
        else:
            job = c.batch.submit_job(**kw, encoding="dbn", compression="zstd",
                                     split_duration="none", delivery="download")
            jobs[f"{sym}.{schema}"] = {"job_id": job["id"], "root": root, "query": kw,
                                       "quoted_cost": cost}
            JOBS.write_text(json.dumps(jobs, indent=2))
            print(f"  submitted {sym} {schema}: {job['id']}", flush=True)


def download(c):
    jobs = json.loads(JOBS.read_text())
    state = {j["id"]: j["state"] for j in c.batch.list_jobs(states="queued,processing,done")}
    waiting = 0
    for name, info in jobs.items():
        path = _dir(info["root"]) / f"{name}.dbn.zst"
        if path.exists():
            continue
        if state.get(info["job_id"]) != "done":
            waiting += 1
            continue
        tmpdir = path.parent / f".{name}.tmp"
        files = c.batch.download(job_id=info["job_id"], output_dir=tmpdir)
        data = [f for f in files if f.name.endswith(".dbn.zst")]
        if len(data) != 1:
            raise RuntimeError(f"{name}: expected one data file, got {data}")
        data[0].rename(path)
        shutil.rmtree(tmpdir)
        print(f"  {path.name}: {_record(path, info['query'], info['quoted_cost'], info['job_id']):,} records",
              flush=True)
    print(f"{waiting} job(s) still processing" if waiting else "all statistics downloaded")
    return waiting


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--buy", action="store_true")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--max-cost", type=float, default=5.0)
    a = ap.parse_args(argv)
    import databento as db
    c = db.Historical((Path.home() / ".databento_key").read_text().strip())
    if a.download:
        return 0 if download(c) == 0 else 2
    quotes = []
    for root, r in ROLL.items():
        for rank in (0, 1):
            for schema in SCHEMAS:
                sym = f"{root}.{r}.{rank}"
                kw = _query(sym, schema)
                quotes.append((root, sym, schema, kw, c.metadata.get_cost(**kw)))
    total = sum(q[-1] for q in quotes)
    print(f"{len(quotes)} files, quoted total ${total:.2f}", flush=True)
    if not a.buy:
        return 0
    if total > a.max_cost:
        print(f"Refusing: above --max-cost ${a.max_cost:.2f}")
        return 1
    buy(c, quotes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
