"""
Next bar or next day? Train on each, score both on the same outcome.

    DATA_SOURCE=cleaned .venv/bin/python compare_targets.py                 # CL, 3 seeds
    DATA_SOURCE=cleaned .venv/bin/python compare_targets.py --symbols all   # pooled, 8
    DATA_SOURCE=cleaned .venv/bin/python compare_targets.py --symbols all --reuse

For each training target — next-bar return (TARGET_CLOCK=bar) and next-day
return (TARGET_CLOCK=day) — runs the pipeline with the real target and with
the target shuffled, for each contract and seed, in the validate_pipeline.py
configuration. Every run is scored by daily IC (scoring.py): the
end-of-session prediction against the next session's close-to-close return,
the return the end-of-day position earns. So the two targets are judged on
the same thing.

Across contracts the daily ICs are pooled, weighted by scored days. "Better"
means further above its own shuffled-target null, not a larger raw number.
The no-skill standard error of a daily IC is about 1/sqrt(days): ±0.077 on
one contract's ~170 scored days, about ±0.027 pooled over eight.

Each finished run is appended to data/compare_targets_runs.jsonl with the
commit it ran on; --reuse skips runs already there for the current commit,
so an interrupted sweep resumes where it stopped.
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

TARGETS = ("bar", "day")
MODES = ("none", "shuffle_target")
ALL_SYMBOLS = ("CL", "NG", "HG", "GC", "ZC", "ZT", "ES", "BTC")
RUN_LOG = os.path.join(_ROOT, "data", "compare_targets_runs.jsonl")


def _commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT,
                          capture_output=True, text=True).stdout.strip()


def _logged(commit: str) -> dict:
    done = {}
    if os.path.exists(RUN_LOG):
        with open(RUN_LOG) as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("commit") == commit and r.get("source") == _source():
                    done[(r["symbol"], r["target"], r["mode"], r["seed"])] = r
    return done


def _source() -> str:
    return os.environ.get("DATA_SOURCE", "legacy").lower()


def run_one(symbol, target, mode, seed, commit, done):
    key = (symbol, target, mode, seed)
    tag = f"{symbol:<4} target={target:<4} mode={mode:<14} seed={seed}"
    if key in done:
        print(f"  [cached] {tag}")
        return done[key]
    env = dict(os.environ)
    env.update(BASE_ENV)
    env.update({"SYMBOL": symbol, "TARGET_CLOCK": target, "NULL_MODE": mode,
                "SEED": str(seed)})
    print(f"  [run]    {tag} ... ", end="", flush=True)
    proc = subprocess.run([_PY, os.path.join(_ROOT, "run_backtest.py")],
                          env=env, cwd=_ROOT, capture_output=True, text=True)
    m = re.search(r"Results saved to (\S+\.npz)", proc.stdout)
    if proc.returncode != 0 or not m:
        print(f"FAILED (exit {proc.returncode})")
        print("\n".join(proc.stdout.strip().splitlines()[-15:]))
        return None
    d = np.load(os.path.join(_ROOT, m.group(1)), allow_pickle=True)
    r = {"symbol": symbol, "target": target, "mode": mode, "seed": seed,
         "ic": float(d["daily_ic"]), "n_days": int(d["daily_ic_n_days"]),
         "sharpe": float(d["sharpe"]), "commit": commit, "source": _source()}
    with open(RUN_LOG, "a") as fh:
        fh.write(json.dumps(r) + "\n")
    print(f"ok  (IC {r['ic']:+.3f})")
    return r


def pooled(rows, target, mode, seed):
    """Days-weighted mean daily IC across contracts for one target/mode/seed."""
    sel = [r for r in rows if r["target"] == target and r["mode"] == mode
           and r["seed"] == seed and np.isfinite(r["ic"])]
    if not sel:
        return float("nan"), 0
    w = np.array([r["n_days"] for r in sel], dtype=float)
    return float(np.average([r["ic"] for r in sel], weights=w)), int(w.sum())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--symbols", default=os.environ.get("SYMBOL", "CL"),
                    help="comma-separated contract roots, or 'all'")
    ap.add_argument("--reuse", action="store_true")
    a = ap.parse_args(argv)
    symbols = ALL_SYMBOLS if a.symbols == "all" else tuple(
        s.strip().upper() for s in a.symbols.split(","))
    commit = _commit()
    done = _logged(commit) if a.reuse else {}
    print(f"Next bar vs next day — data={_source()}, contracts={','.join(symbols)}, "
          f"{a.seeds} seed(s), commit {commit}\n")
    rows = [r for sym in symbols for t in TARGETS for m in MODES for s in range(a.seeds)
            if (r := run_one(sym, t, m, s, commit, done)) is not None]

    print("\n" + "=" * 76)
    print("  Daily IC by contract (mean over seeds): real − shuffled-target")
    print("=" * 76)
    print(f"  {'':<5}" + "".join(f"{t + ' real':>12}{t + ' null':>12}" for t in TARGETS))
    for sym in symbols:
        line = f"  {sym:<5}"
        for t in TARGETS:
            for m in MODES:
                v = [r["ic"] for r in rows if r["symbol"] == sym and r["target"] == t
                     and r["mode"] == m and np.isfinite(r["ic"])]
                line += f"{np.mean(v):>+12.3f}" if v else f"{'—':>12}"
        print(line)

    print("\n" + "=" * 76)
    print(f"  Pooled over {len(symbols)} contract(s), weighted by scored days")
    print("=" * 76)
    print(f"  {'target':<6}{'real':>18}{'shuffled target':>22}{'gap':>9}{'gap / s.e.':>12}")
    verdicts, n_days = {}, 0
    for t in TARGETS:
        real = np.array([pooled(rows, t, "none", s)[0] for s in range(a.seeds)])
        null = np.array([pooled(rows, t, "shuffle_target", s)[0] for s in range(a.seeds)])
        real, null = real[np.isfinite(real)], null[np.isfinite(null)]
        if not len(real) or not len(null):
            continue
        n_days = max(pooled(rows, t, "none", s)[1] for s in range(a.seeds))
        # Seed-to-seed spread, plus the sampling error of an IC on n_days days
        # (shared by every seed, so it does not average out).
        se = np.sqrt(np.var(real) / len(real) + np.var(null) / len(null) + 2.0 / n_days)
        gap = real.mean() - null.mean()
        verdicts[t] = gap / se
        print(f"  {t:<6}{real.mean():>+10.4f} ±{real.std():.4f}"
              f"{null.mean():>+14.4f} ±{null.std():.4f}{gap:>+9.4f}{gap / se:>12.2f}")
    if verdicts:
        print(f"\n  {n_days} scored contract-days; no-skill s.e. of one pooled daily IC "
              f"≈ ±{1 / np.sqrt(n_days):.3f}.")
        if not any(abs(z) >= 2 for z in verdicts.values()):
            print("  Neither target is distinguishable from its shuffled null (|gap/s.e.| < 2).")
            print("  The choice cannot be made from results; decide on design.")
        else:
            for t, z in verdicts.items():
                print(f"  {t}: {'separates' if abs(z) >= 2 else 'does not separate'} "
                      f"from its null ({z:+.2f} s.e.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
