"""
Crude oil's daily at-the-money implied volatility, from option settlements.

    .venv/bin/python implied_vol.py

Reads data/statistics_LO_OPT_full.parquet (CME LO options on CL futures,
Databento statistics), keeps settlement prices (stat_type 3), and for each
trading date and option month:
  1. the forward F by put-call parity at the strike where |call - put| is
     smallest: F = K + (C - P)
  2. the at-the-money straddle at the strike nearest F
  3. the Black-76 volatility that prices that straddle (discounting ignored:
     at 5% rates and a month to expiry it moves the volatility by well under 1%)
The day's implied volatility is from the nearest option month with at least
20 calendar days to expiry.

Expiry is not in this file, so it is computed from CME's rules: CL futures
stop trading 3 business days before the 25th of the month before delivery
(4 if the 25th is not a business day), and LO options stop 3 business days
before that. US federal holidays stand in for the exchange calendar (Good
Friday is missing); checked against the last settlement date of every
option month in the file. With at least 20 days to go, a day's error is at
most 5% in time, about 2.5% in volatility. Writes data/crude_iv_daily.csv.
"""

from __future__ import annotations

import os
import re
import sys
from math import erf, log, sqrt

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(_ROOT, "data", "statistics_LO_OPT_full.parquet")
OUT = os.path.join(_ROOT, "data", "crude_iv_daily.csv")
MONTHS = "FGHJKMNQUVXZ"
SYM = re.compile(r"^LO([FGHJKMNQUVXZ])(\d) ([CP])(\d+)$")
MIN_DAYS = 20


from pandas.tseries.holiday import USFederalHolidayCalendar  # noqa: E402

_HOLIDAYS = set(USFederalHolidayCalendar().holidays("2009-01-01", "2028-12-31"))


def _is_bday(d):
    return d.weekday() < 5 and d not in _HOLIDAYS


def _bday_back(d, n):
    while n:
        d -= pd.Timedelta(days=1)
        if _is_bday(d):
            n -= 1
    return d


def option_expiry(month_code, year_digit, trade_date):
    """LO expiry for a contract month, decade taken nearest the trade date."""
    m = MONTHS.index(month_code) + 1
    y = (trade_date.year // 10) * 10 + int(year_digit)
    if y < trade_date.year - 2:
        y += 10
    prev = pd.Timestamp(year=y, month=m, day=1) - pd.Timedelta(days=1)
    d25 = pd.Timestamp(year=prev.year, month=prev.month, day=25)
    fut_ltd = _bday_back(d25, 3) if _is_bday(d25) else _bday_back(d25, 4)
    return _bday_back(fut_ltd, 3)


def _ncdf(x):
    return 0.5 * (1 + erf(x / sqrt(2)))


def straddle_value(F, K, T, s):
    if s <= 0 or T <= 0:
        return abs(F - K)
    d1 = (log(F / K) + 0.5 * s * s * T) / (s * sqrt(T))
    d2 = d1 - s * sqrt(T)
    call = F * _ncdf(d1) - K * _ncdf(d2)
    put = K * _ncdf(-d2) - F * _ncdf(-d1)
    return call + put


def implied(F, K, T, price):
    """Black-76 volatility of a straddle by bisection; NaN if out of range."""
    lo, hi = 1e-4, 5.0
    if not (straddle_value(F, K, T, lo) <= price <= straddle_value(F, K, T, hi)):
        return float("nan")
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if straddle_value(F, K, T, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def atm_iv(day_series):
    """(F, K, iv) from one date's settlements for one option month."""
    piv = day_series.pivot_table(index="strike", columns="cp", values="price", aggfunc="last")
    if "C" not in piv or "P" not in piv:
        return None
    piv = piv.dropna()
    if len(piv) < 3:
        return None
    k0 = (piv["C"] - piv["P"]).abs().idxmin()
    F = k0 + piv.loc[k0, "C"] - piv.loc[k0, "P"]
    K = min(piv.index, key=lambda k: abs(k - F))
    return F, K, piv.loc[K, "C"] + piv.loc[K, "P"]


def main():
    import pyarrow.dataset as ds
    t = ds.dataset(SRC).to_table(columns=["symbol", "ts_ref", "price", "ts_event"],
                                 filter=ds.field("stat_type") == 3).to_pandas()
    p = t["symbol"].str.extract(SYM)
    t = t.assign(mon=p[0], yr=p[1], cp=p[2], strike=pd.to_numeric(p[3]) / 100.0).dropna(subset=["mon"])
    t["date"] = t["ts_ref"].dt.tz_convert(None).dt.normalize()
    t = t.sort_values("ts_event").groupby(["symbol", "date"]).tail(1)   # final settlement
    rows = []
    for (date, mon, yr), g in t.groupby(["date", "mon", "yr"]):
        exp = option_expiry(mon, yr, date)
        days = (exp - date).days
        if days < MIN_DAYS:
            continue
        r = atm_iv(g)
        if r is None:
            continue
        F, K, straddle = r
        iv = implied(F, K, days / 365.0, straddle)
        rows.append({"date": date, "series": f"{mon}{yr}", "days": days, "F": F, "K": K,
                     "straddle": straddle, "iv": iv})
    a = pd.DataFrame(rows).dropna(subset=["iv"])
    daily = a.sort_values("days").groupby("date").head(1).sort_values("date")
    daily.to_csv(OUT, index=False)
    print(f"{len(daily)} dates, {daily['date'].min():%Y-%m-%d} -> {daily['date'].max():%Y-%m-%d}; "
          f"IV median {daily['iv'].median():.1%}, range {daily['iv'].min():.1%}-{daily['iv'].max():.1%}; "
          f"days to expiry median {daily['days'].median():.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
