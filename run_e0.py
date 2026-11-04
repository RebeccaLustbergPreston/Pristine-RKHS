"""
E0 — are the kernels doing the work? (FALL_2026_TASKS.md E0)

    DATA_SOURCE=cleaned .venv/bin/python run_e0.py            # 3 seeds
    DATA_SOURCE=cleaned .venv/bin/python run_e0.py --seeds 5

Replaces the kernels' out-of-sample predictions with a one-line rule and
changes nothing else, in both of the pipeline's position paths:

    returns path (Run 25, the configuration validate_pipeline.py checks):
        kernels   vs  trend         — trailing 28-day return as the prediction
    vol path (TARGET_MODE=vol, the mechanism E0 was written for):
        kernels   vs  trailing_vol  — trailing 20-session realised vol

If a rule matches the kernels, the kernels are an elaborate way to compute it.
Results are reported as mean ± sd across seeds, never as one number, with
Sharpe beside daily IC (scoring.py) — Sharpe alone cannot separate real from
shuffled here.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

import numpy as np

from validate_pipeline import BASE_ENV, _PY, _ROOT

CONFIGS = [
    ("returns", "kernels", {}),
    ("returns", "trend", {"PRED_SOURCE": "trend"}),
    ("vol", "kernels", {"TARGET_MODE": "vol"}),
    ("vol", "trailing_vol", {"TARGET_MODE": "vol", "PRED_SOURCE": "trailing_vol"}),
]


def run(path_name, pred, extra, seed):
    env = dict(os.environ)
    env.update(BASE_ENV)
    env.update(extra)
    env.update({"SEED": str(seed), "NULL_MODE": "none"})
    print(f"  [run] path={path_name:<8} prediction={pred:<13} seed={seed} ... ",
          end="", flush=True)
    p = subprocess.run([_PY, os.path.join(_ROOT, "run_backtest.py")], env=env,
                       cwd=_ROOT, capture_output=True, text=True)
    m = re.search(r"Results saved to (\S+\.npz)", p.stdout)
    if p.returncode != 0 or not m:
        print(f"FAILED (exit {p.returncode})")
        print("\n".join(p.stdout.strip().splitlines()[-12:]))
        return None
    print("ok")
    d = np.load(os.path.join(_ROOT, m.group(1)), allow_pickle=True)
    return {"sharpe": float(d["sharpe"]), "ic": float(d["daily_ic"]),
            "active": int(d["n_days_active"]), "file": m.group(1)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args(argv)
    print(f"E0 — data={os.environ.get('DATA_SOURCE', 'legacy')}, {a.seeds} seed(s)\n")
    res = {}
    for path_name, pred, extra in CONFIGS:
        res[(path_name, pred)] = [r for s in range(a.seeds)
                                  if (r := run(path_name, pred, extra, s))]
    print("\n" + "=" * 74)
    print(f"  {'path':<9}{'prediction':<14}{'Sharpe':>18}{'daily IC':>20}{'active d':>11}")
    print("  " + "-" * 70)
    for (path_name, pred), rs in res.items():
        if not rs:
            continue
        sh = np.array([r["sharpe"] for r in rs])
        ic = np.array([r["ic"] for r in rs])
        act = np.mean([r["active"] for r in rs])
        print(f"  {path_name:<9}{pred:<14}{sh.mean():>+10.2f} ±{sh.std():.2f}"
              f"{ic.mean():>+13.4f} ±{ic.std():.4f}{act:>10.0f}")
    print("\n  A rule that scores like the kernels means the kernels are not the source")
    print("  of the result. Three seeds is the minimum; quote the spread, not a value.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
