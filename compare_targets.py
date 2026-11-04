"""
Next bar or next day? Train on each, score both on the same outcome.

    .venv/bin/python compare_targets.py                    # 3 seeds
    DATA_SOURCE=cleaned .venv/bin/python compare_targets.py --seeds 5
    .venv/bin/python compare_targets.py --reuse            # skip finished runs

For each training target — next-bar return (TARGET_CLOCK=bar) and next-day
return (TARGET_CLOCK=day) — runs the pipeline with the real target and with
the target shuffled, across seeds, in the validate_pipeline.py configuration.
Every run is scored by daily IC (scoring.py): the end-of-session prediction
against the next session's close-to-close return, the return the end-of-day
position earns. So the two targets are judged on the same thing.

"Better" means further above its own shuffled-target null, not a larger raw
number. With n scored days the no-skill standard error of a daily IC is about
1/sqrt(n): roughly ±0.056 on one contract's ~318 days, so a single contract
will usually not separate the two. Pooled across contracts it can.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

import numpy as np

from validate_pipeline import BASE_ENV, _PY, _ROOT

TARGETS = ("bar", "day")
MODES = ("none", "shuffle_target")


def results_path(target: str, mode: str, seed: int) -> str:
    label = "2fast_3slow_twostage_v2" + ("_tday" if target == "day" else "") + "_kalman_wu60"
    source = os.environ.get("DATA_SOURCE", "legacy").lower()
    if source != "legacy":
        label += f"_src-{source}"
    if os.environ.get("SWITCH_GAPS", "remove").lower() == "keep":
        label += "_gaps-kept"
    lag = int(os.environ.get("SLOW_FEATURE_LAG", "1"))
    if lag != 1:
        label += f"_slowlag{lag}"
    if os.environ.get("GATE_FIT", "walkforward").lower() == "fullsample":
        label += "_gatefull"
    if mode != "none":
        label += f"_null-{mode}"
    if seed != 0:
        label += f"_seed{seed}"
    return os.path.join(_ROOT, f"backtest_results_{label}.npz")


def run_one(target: str, mode: str, seed: int, reuse: bool):
    path = results_path(target, mode, seed)
    tag = f"target={target:<4} mode={mode:<14} seed={seed}"
    if not (reuse and os.path.exists(path)):
        env = dict(os.environ)
        env.update(BASE_ENV)
        env.update({"TARGET_CLOCK": target, "NULL_MODE": mode, "SEED": str(seed)})
        print(f"  [run]    {tag} ... ", end="", flush=True)
        proc = subprocess.run([_PY, os.path.join(_ROOT, "run_backtest.py")],
                              env=env, cwd=_ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"FAILED (exit {proc.returncode})")
            print("\n".join(proc.stdout.strip().splitlines()[-15:]))
            return None
        print("ok")
        m = re.search(r"Results saved to (\S+\.npz)", proc.stdout)
        if m:
            path = os.path.join(_ROOT, m.group(1))
    else:
        print(f"  [cached] {tag}")
    d = np.load(path, allow_pickle=True)
    if "daily_ic" not in d.files:
        print(f"    {os.path.basename(path)} predates daily IC; re-run without --reuse")
        return None
    return {"target": target, "mode": mode, "seed": seed, "ic": float(d["daily_ic"]),
            "n_days": int(d["daily_ic_n_days"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--reuse", action="store_true")
    a = ap.parse_args(argv)
    print(f"Next bar vs next day — data={os.environ.get('DATA_SOURCE', 'legacy')}, "
          f"{a.seeds} seed(s)\n")
    rows = [r for t in TARGETS for m in MODES for s in range(a.seeds)
            if (r := run_one(t, m, s, a.reuse)) is not None]

    print("\n" + "=" * 72)
    print("  Daily IC: end-of-session prediction vs next session's return")
    print("=" * 72)
    print(f"  {'target':<6}{'real':>18}{'shuffled target':>22}{'gap':>9}{'gap / s.e.':>12}")
    verdicts = {}
    for t in TARGETS:
        real = np.array([r["ic"] for r in rows if r["target"] == t and r["mode"] == "none"])
        null = np.array([r["ic"] for r in rows if r["target"] == t and r["mode"] != "none"])
        if not len(real) or not len(null):
            continue
        n_days = max(r["n_days"] for r in rows if r["target"] == t)
        # Two sources of noise: seed-to-seed spread, and the sampling error of
        # an IC on n_days days (identical across seeds, so it does not average out).
        se = np.sqrt(np.var(real, ddof=0) / len(real) + np.var(null, ddof=0) / len(null)
                     + 2.0 / n_days)
        gap = real.mean() - null.mean()
        verdicts[t] = gap / se
        print(f"  {t:<6}{real.mean():>+10.4f} ±{real.std():.4f}"
              f"{null.mean():>+14.4f} ±{null.std():.4f}{gap:>+9.4f}{gap / se:>12.2f}")
    if verdicts:
        print(f"\n  {n_days} scored days; no-skill s.e. of one daily IC ≈ ±{1/np.sqrt(n_days):.3f}.")
        sep = {t: abs(z) >= 2 for t, z in verdicts.items()}
        if not any(sep.values()):
            print("  Neither target is distinguishable from its shuffled null (|gap/s.e.| < 2).")
            print("  On this data the choice cannot be made from results; decide on design.")
        else:
            for t, z in verdicts.items():
                print(f"  {t}: {'separates' if sep[t] else 'does not separate'} "
                      f"from its null ({z:+.2f} s.e.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
