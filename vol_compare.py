"""
Do the kernels forecast volatility better than trailing volatility does?

    DATA_SOURCE=cleaned .venv/bin/python vol_compare.py --symbols all --reuse
    DATA_SOURCE=cleaned .venv/bin/python vol_compare.py --target relvol --reuse

Runs the pipeline in its volatility mode (TARGET_MODE=vol: the kernels are
trained on log forward realised volatility) for each contract and seed, with
the real target and with the target shuffled. --target relvol trains on
forward volatility relative to trailing volatility instead (decision log
entry 17); it is scored the same way. Each run saves every kernel's
own out-of-sample forecast, scored by scoring.daily_vol_ic against what
followed each session's close: next-day |return| and next-5-day realised vol.

Volatility is persistent, so trailing 20-day volatility alone forecasts both
well; that is the baseline row. The number that matters is "beyond": rank
correlation with the outcome after trailing volatility is removed from both.
A kernel earns its place only if "beyond" is positive and clear of the
shuffled-target null.

Pooled across contracts per kernel, weighted by scored days. Finished runs
go to data/vol_compare_runs.jsonl with their commit; --reuse resumes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

import numpy as np

from validate_pipeline import BASE_ENV, _PY, _ROOT

MODES = ("none", "shuffle_target")
ALL_SYMBOLS = ("CL", "NG", "HG", "GC", "ZC", "ZT", "ES", "BTC")
RUN_LOG = os.path.join(_ROOT, "data", "vol_compare_runs.jsonl")
COLS = ("ic_abs1", "ic_rv5", "partial_abs1", "partial_rv5",
        "baseline_ic_abs1", "baseline_ic_rv5", "n_days")


def _commit():
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT,
                          capture_output=True, text=True).stdout.strip()


def _source():
    return os.environ.get("DATA_SOURCE", "legacy").lower()


def _logged(commit, target):
    done = {}
    if os.path.exists(RUN_LOG):
        with open(RUN_LOG) as fh:
            for line in fh:
                r = json.loads(line)
                if (r["commit"] == commit and r["source"] == _source()
                        and r.get("target", "vol") == target):
                    done[(r["symbol"], r["mode"], r["seed"])] = r
    return done


def run_one(symbol, mode, seed, commit, done, target="vol"):
    key = (symbol, mode, seed)
    tag = f"{symbol:<4} mode={mode:<14} seed={seed}"
    if key in done:
        print(f"  [cached] {tag}")
        return done[key]
    env = dict(os.environ)
    env.update(BASE_ENV)
    env.update({"SYMBOL": symbol, "TARGET_MODE": target, "NULL_MODE": mode,
                "SEED": str(seed)})
    print(f"  [run]    {tag} ... ", end="", flush=True)
    p = subprocess.run([_PY, os.path.join(_ROOT, "run_backtest.py")], env=env,
                       cwd=_ROOT, capture_output=True, text=True)
    m = re.search(r"Results saved to (\S+\.npz)", p.stdout)
    if p.returncode != 0 or not m:
        print(f"FAILED (exit {p.returncode})")
        print("\n".join(p.stdout.strip().splitlines()[-15:]))
        return None
    d = np.load(os.path.join(_ROOT, m.group(1)), allow_pickle=True)
    scores = {str(n): dict(zip(COLS, map(float, row)))
              for n, row in zip(d["vol_score_names"], d["vol_scores"])}
    r = {"symbol": symbol, "mode": mode, "seed": seed, "target": target, "scores": scores,
         "commit": commit, "source": _source()}
    with open(RUN_LOG, "a") as fh:
        fh.write(json.dumps(r) + "\n")
    c = scores.get("combined", {})
    print(f"ok  (combined beyond-rv5 {c.get('partial_rv5', float('nan')):+.3f})")
    return r


def pooled(rows, mode, seed, name, col):
    vals, w = [], []
    for r in rows:
        if r["mode"] == mode and r["seed"] == seed and name in r["scores"]:
            s = r["scores"][name]
            if np.isfinite(s[col]) and s["n_days"] > 0:
                vals.append(s[col]); w.append(s["n_days"])
    if not vals:
        return float("nan"), 0
    return float(np.average(vals, weights=w)), int(sum(w))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--symbols", default="all")
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--target", choices=("vol", "relvol"), default="vol")
    a = ap.parse_args(argv)
    symbols = ALL_SYMBOLS if a.symbols == "all" else tuple(
        s.strip().upper() for s in a.symbols.split(","))
    commit = _commit()
    done = _logged(commit, a.target) if a.reuse else {}
    print(f"Volatility forecasts vs trailing volatility — target={a.target}, data={_source()}, "
          f"contracts={','.join(symbols)}, {a.seeds} seed(s), commit {commit}\n")
    rows = [r for sym in symbols for m in MODES for s in range(a.seeds)
            if (r := run_one(sym, m, s, commit, done, a.target)) is not None]
    names = sorted({n for r in rows for n in r["scores"]},
                   key=lambda n: (n == "combined", n))

    for col, label in (("rv5", "next-5-day realised vol"), ("abs1", "next-day |return|")):
        print("\n" + "=" * 78)
        print(f"  {label}: pooled rank IC, mean ± sd over seeds")
        print("=" * 78)
        print(f"  {'forecast':<12}{'IC':>16}{'beyond trailing':>20}{'null beyond':>18}{'gap/s.e.':>10}")
        base = [pooled(rows, "none", s, names[0], f"baseline_ic_{col}")[0]
                for s in range(a.seeds)]
        for n in names:
            ic = np.array([pooled(rows, "none", s, n, f"ic_{col}")[0] for s in range(a.seeds)])
            be = np.array([pooled(rows, "none", s, n, f"partial_{col}")[0]
                           for s in range(a.seeds)])
            nu = np.array([pooled(rows, "shuffle_target", s, n, f"partial_{col}")[0]
                           for s in range(a.seeds)])
            days = max(pooled(rows, "none", s, n, f"partial_{col}")[1] for s in range(a.seeds))
            be, nu = be[np.isfinite(be)], nu[np.isfinite(nu)]
            if not len(be):
                continue
            se = np.sqrt(np.var(be) / len(be) + (np.var(nu) / len(nu) if len(nu) else 0)
                         + 2.0 / max(days, 1))
            gap = be.mean() - (nu.mean() if len(nu) else 0.0)
            print(f"  {n:<12}{np.nanmean(ic):>+9.3f} ±{np.nanstd(ic):.3f}"
                  f"{be.mean():>+13.3f} ±{be.std():.3f}"
                  f"{(nu.mean() if len(nu) else float('nan')):>+12.3f}{gap / se:>13.2f}")
        print(f"  {'trailing vol':<12}{np.nanmean(base):>+9.3f}   (the baseline itself)")
    print("\n  A kernel adds volatility information only if 'beyond trailing' is positive")
    print("  and at least 2 s.e. above its shuffled-target null.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
