"""
Tests for F1: scoring.cross_sectional_ic and the f1.py signals.

Run with: python test_f1.py   (no market data required)
"""

import sys

import numpy as np
import pandas as pd

from f1 import carry_signal, outcome, signals
from scoring import cross_sectional_ic

_failures = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 — test harness
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _failures.append(name)


def _panel(n=300, k=8, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n)
    lr = rng.normal(0, 0.01, (n, k)) * np.linspace(0.5, 3, k)   # very different vols
    close = pd.DataFrame(100 * np.exp(np.cumsum(lr, axis=0)), index=idx,
                         columns=[f"C{i}" for i in range(k)])
    return close


def t_perfect_and_random():
    close = _panel()
    y = outcome(close)
    assert cross_sectional_ic(y, y)["ic"] > 0.999
    noise = pd.DataFrame(np.random.default_rng(1).normal(size=y.shape), index=y.index,
                         columns=y.columns)
    r = cross_sectional_ic(noise, y)
    assert abs(r["t"]) < 3, r["t"]


def t_ranks_are_per_session():
    # Adding a per-session constant to every contract changes nothing.
    close = _panel()
    y = outcome(close)
    f = y + np.arange(len(y))[:, None] * 100.0
    assert abs(cross_sectional_ic(f, y)["ic"] - 1) < 1e-9


def t_min_names():
    close = _panel()
    y = outcome(close)
    f = y.copy()
    f.iloc[:, :4] = np.nan                       # only 4 contracts left
    assert cross_sectional_ic(f, y, min_names=5)["n"] == 0


def t_signals_are_causal():
    # Changing the last price may change only the last row of every signal.
    close = _panel()
    sw = {c: pd.DataFrame({"session_date": close.index[[50, 100, 150]],
                           "log_gap": [0.01, -0.02, 0.01], "at_open": False})
          for c in close.columns}
    a = signals(close, sw)
    c2 = close.copy()
    c2.iloc[-1] *= 1.5
    b = signals(c2, sw)
    for k in a:
        pd.testing.assert_frame_equal(a[k].iloc[:-1], b[k].iloc[:-1], check_exact=False)


def t_outcome_is_next_session():
    close = _panel()
    y = outcome(close)
    lr = np.log(close).diff()
    vol = lr.rolling(20).std()
    d = 100
    assert np.isclose(y.iloc[d, 0], lr.iloc[d + 1, 0] / vol.iloc[d, 0])


def t_carry_from_switches():
    dates = pd.bdate_range("2024-01-01", periods=200)
    sw = pd.DataFrame({"session_date": dates[[10, 60, 110]], "log_gap": [0.0, 0.02, -0.01]})
    c = carry_signal(dates, sw)
    assert c.iloc[:60].isna().all()              # needs two switches for a spacing
    yrs = (dates[60] - dates[10]).days / 365
    assert np.isclose(c.iloc[60], -0.02 / yrs) and np.isclose(c.iloc[109], -0.02 / yrs)
    yrs2 = (dates[110] - dates[60]).days / 365
    assert np.isclose(c.iloc[110], 0.01 / yrs2)


def t_roll_free_returns_and_carry():
    from f1_long import roll_free
    d = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    t = pd.DataFrame({
        "date": [d[0], d[0], d[1], d[1], d[2], d[2]],
        "rank": [0, 1, 0, 1, 0, 1],
        "instrument_id": [1, 2, 1, 2, 2, 3],          # contract 2 becomes front on d[2]
        "settle": [70.0, 71.0, 72.0, 73.0, 74.0, 75.0]})
    rf = roll_free(t)
    assert np.isnan(rf["ret"].iloc[0])
    assert np.isclose(rf["ret"].iloc[1], np.log(72 / 70))
    # Across the roll: contract 2's own move, 73 → 74, not 72 → 74.
    assert np.isclose(rf["ret"].iloc[2], np.log(74 / 73))
    assert np.isclose(rf["front_over_second"].iloc[0], np.log(70 / 71))


for _n, _f in list(globals().items()):
    if _n.startswith("t_"):
        check(_n[2:], _f)
print(f"\n{'ALL PASS' if not _failures else f'{len(_failures)} FAILED'}")
sys.exit(1 if _failures else 0)
