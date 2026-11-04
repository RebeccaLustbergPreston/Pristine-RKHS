# Architecture decisions

A running log of the choices that shape the data and the pipeline: what was
decided, what it changes downstream, and why — with the evidence or the
published practice behind it. Newest last. When a decision is reversed, add
a new entry that supersedes the old one; don't edit history.

Each entry: **Decision**, **Status**, **Context**, **Alternatives**,
**Why**, **Downstream impact**.

---

## 1. One vendor, Databento, for every contract

*3 November 2026 (settled in September) · Status: in force*

**Decision.** All eight contracts — CL, NG, HG, GC, ZC, ZT, ES, BTC — come
from Databento's CME Globex feed (`GLBX.MDP3`), 2 July 2023 to 31 December
2024: trades, 1-minute bars, statistics and definitions for all, order-book
depth (`mbp-10`) for CL, NG, HG, ZC.

**Context.** `FALL_2026_TASKS.md` section A asked whether SMU's WRDS covered
tick data. It does not for CME futures; the fast kernels need trade-by-trade
data.

**Alternatives.** WRDS daily settlement only (kills the fast layer); a mix
of vendors (roll conventions and timestamps would not be comparable).

**Why.** One feed means one timestamp convention, one symbology and one roll
definition across the book.

**Downstream impact.** Every contract's continuous series is a Databento
roll (`.v.0` volume, `.n.0` open interest, `.c.0` calendar), recorded in each
file's header. Depth exists for four contracts only, so any comparison that
uses book features must be run on those four (G8).

---

## 2. Narrow mismatched trade sources to one series using Databento's own roll

*3 November 2026 · Status: in force · Code: `ingest_csv.py`*

**Decision.** Where a contract's trades are not one continuous DBN series —
NG and HG (CSV, parent pull), ES (half `ES.v.0`, half `ES.FUT` parent), CL and
GC (CSV exports) — keep, for each date, only the contract that Databento's
roll for the bar series names on that date. The roll is read from the
headers of the continuous minute-bar files, not estimated.

**Alternatives.** Re-pull as DBN ($35.52 for NG and HG, more for the rest);
estimate the front month from daily volume.

**Why.** Free, and exact: minute bars rebuilt from the narrowed trades equal
Databento's own bars bar for bar for every contract (HG 512,262 of 512,262;
NG 469,616; ES 531,442; CL and GC likewise). An estimated roll could not
have been checked this way.

**Downstream impact.** Each contract's trades are the same series as its
bars, so bars and trades can be mixed without contract mismatches. ES is
labelled `ES.n.0` throughout; Arjan's `ES.v.0` half was accepted only because
its roll agrees with `ES.n.0` on every date (checked at ingestion).

---

## 3. Keep every trade CME reports, even repeats (cleaning rule c)

*3 November 2026 · Status: in force; the written standard still says
otherwise · Code: `clean.py`*

**Decision.** Remove only true re-deliveries (the same record twice from
overlapping files — ingestion refuses those). Do not de-duplicate on
`(ts_event, sequence)`, and do not drop rows that are identical in every
field.

**Context.** `docs/data_cleaning_standard.md` rule c says de-duplicate on
`(ts_event, sequence)`.

**Evidence.** On CME, one aggressive order that fills against resting orders
at several prices is reported as several trades in one packet, sharing
timestamp and sequence number. Tested against Databento's minute-bar volume
on HG:

| De-duplication | HG rows | minutes whose volume matches Databento |
|---|---|---|
| none | 12,419,012 | 512,262 of 512,262 |
| rows identical in every field | 12,398,405 | 500,463 |
| `(ts_event, sequence)` | 12,138,117 | 393,888 |

Connor's CL, cleaned to the standard as written, is short exactly the
767,956 trades (1.9%) that share a `(ts_event, sequence)` — nothing else
differs.

**Downstream impact.** Volume, VPIN, Kyle's lambda and Hawkes counts are
computed on the full tape. Anything cleaned to the old rule undercounts
volume in busy minutes. The standard needs amending (`TODO.md`).

---

## 4. Session date is the exchange's trade date (cleaning rule f)

*3 November 2026 · Status: in force · Code: `clean.py`*

**Decision.** A trade after 17:00 Chicago time belongs to the next day's
session; a session that would fall on a day the exchange did not settle
(holidays, weekends) rolls to the next day it did. Trading days come from
each contract's own settlement prices (statistics, `stat_type` 3, `ts_ref`).

**Alternatives.** The clock rule alone (Connor's file), which labels holiday
evenings as sessions that never existed.

**Why.** It matches the exchange: settlement dates are the exchange's own
record of which days traded.

**Downstream impact.** 378 sessions in the window for every contract, not
468 calendar dates. Daily bars, the slow kernels and the external VIX/SPY/XLE
join all sit on these 378 days. Anything set in days (`WARMUP_DAYS`, fold
sizes, embargo) was set against 468 and should be checked. 418,516 CL trades
on ten holiday evenings carry a different date from Connor's file.

---

## 5. Cleaned data in its own folder, and a switch to choose the source

*3 November 2026 · Status: in force · Code: `data_loader.py`,
`run_backtest.py`*

**Decision.** Cleaned files live in `data/cleaned-master/<SYM>/` (and the same
path in Box). The pipeline reads them with `DATA_SOURCE=cleaned`; the default
stays `legacy` (the original `CL.c.0` files) so every earlier result still
reproduces. Cleaned runs are tagged `_src-cleaned` in their results file.

**Why.** Ingestion writes uncleaned files to `data/<SYM>/`; writing cleaned
output there would overwrite its own input. Keeping the old source runnable
lets a change of data be measured separately from a change of method.

**Downstream impact.** CL under `cleaned` is `CL.v.0`, 40.6M trades, not the
36.7M-trade `CL.c.0`. No number in `POSTMORTEM.md` was produced on it.

---

## 6. Tick value computed from contract size, not read from definitions

*3 November 2026 · Status: in force · Code: `data_loader.py`*

**Decision.** Tick value = `min_price_increment × unit_of_measure_qty`,
× 0.01 where the price is quoted in cents (ZC) or percent of par (ZT).

**Evidence.** The definitions field `min_price_increment_amount` is not the
dollar value of a tick in this data — CL shows 0.1 for a $10 tick — and the
error is not one factor across contracts. Computed values match the
exchange's contract specs: CL, NG, GC $10; HG, ZC, ES $12.50; ZT $7.8125;
BTC $25.

**Downstream impact.** Per-contract trading cost (C3) and any dollar
position sizing must use `DataLoader.tick_value()` / `cost_per_trade()`.
One tick on CL at $70 is 0.000143 of price, close to the flat 0.00015 the
pipeline has used.

---

## 7. Bar size per contract set by rule, never by score

*3 November 2026 · Status: agreed in principle; target bars per session waits
on the hours-or-days decision (`TODO.md` item 3)*

**Decision.** Every contract gets the same number of bars per session, from
a threshold computed only from past sessions (e.g. trailing 20-session median
of price × size ÷ target).

**Evidence.** The "$1M dollar bar" is price × contracts with no multiplier.
At $1M: NG and HG 0.2 bars a day, ES about 7,000; BTC's count rises fourfold
over the window as its price rises.

**Why.** Choosing bar size by which one scores best is the tuning-on-noise
that made Runs 1–25 meaningless. A backward-looking threshold also keeps
bars per session stable through price drift without lookahead.

**Downstream impact.** CL's bar schedule changes when this lands, so CL is
re-baselined at that step (D2), after D1 is shown to reproduce the fixed-size
results exactly.

---

## 8. Remove contract-switch gaps by chaining returns forward (D0)

*3 November 2026 · Status: in force · Code: `roll_gaps.py`, `data_loader.py`
(`switch_gaps`), `SWITCH_GAPS` in `run_backtest.py`*

**Decision.** At each change of contract in the continuous series, measure
the gap between the old contract's last price and the new contract's first,
and take it out of every *price change* from that point on (ratio
adjustment, applied forward so only past switches are ever used). Keep the
actual traded price wherever the *level* matters: building dollar bars
(price × size), cost per trade (tick ÷ price), position sizing.

**Context.** The vendor's continuous series splices contracts without
adjusting, so each switch is a price move nobody traded — 18 on CL, typically
about 1%, two to four times a normal bar move. They were in the target, in
daily P&L (positions are held overnight, through the switch), in momentum and
realised volatility, and inside VPIN's bar volatility and Kyle's price change.

**Alternatives.** Drop every target return that spans a switch (D0's original
wording). Removes 18 of ~5,100 training labels but leaves the inputs and the
daily P&L contaminated: a 20-day momentum window keeps "remembering" a move
that never happened.

**Why — published practice.** Chaining each contract's own returns across
the roll is the standard way to build a continuous futures series for
backtesting: vendors sell back-adjusted series for this, and López de Prado
(*Advances in Financial Machine Learning*, 2018, ch. 2, "the roll") builds a
"rolled" series by subtracting cumulative roll gaps, with rolled prices for
returns and P&L and raw prices for sizing. Ratio rather than difference
adjustment keeps returns exact and prices positive. Dropping roll-spanning
observations is mainly an academic-returns practice, where the question is
what prices did rather than what a held position earned. Classic
*back*-adjustment rewrites past price levels using rolls that had not
happened yet — harmless to returns but a lookahead for any level-based
feature — so the adjustment here runs forward.

**Downstream impact.** Target, daily P&L, momentum, realised volatility, the
VRP and MacroMotion inputs, VPIN's bar volatility and tick rule, and Kyle's
price changes all see contract-consistent moves. Dollar-bar boundaries and
costs are unchanged. Roll *costs* — the spread paid to roll a real position —
are still not modelled; that is H3, a separate line.

**Measured.** On cleaned CL the 18 gaps sum to −11.6% over the window, 16 of
them downward: the unadjusted series showed crude losing about 12% that no
held position lost — a built-in downward drift under a momentum-directed
strategy. Trades and 1-minute bars find the same 18 switches with identical
gaps. Falsification suite, cleaned CL, Run 25 configuration, three seeds
(Sharpe mean ± sd):

| mode | gaps in | gaps removed |
|---|---|---|
| real features | 0.37 ± 0.33 | 0.36 ± 1.10 |
| pure noise | −1.23 ± 0.69 | −0.31 ± 0.62 |
| shuffled in time | 0.11 ± 0.73 | −0.71 ± 1.82 |
| shuffled target | −0.46 ± 1.29 | −0.10 ± 1.95 |
| leakage check (< 0.05) | 0.0468 | 0.0132 |

The leakage margin is now comfortable. Before the fix, real features looked
separable from noise (0.37 vs −1.23); after it they are not (0.36 vs −0.31).
The noise run's strongly negative Sharpe was most likely the fake downward
moves, not something the kernels added — so the one sign of kernel skill on
the cleaned data was an artifact of the gaps. Stage-2 out-of-sample
correlation is about zero in every mode either way. This is what step 7
(a score that separates real from shuffled) is for; nothing should be tuned
before it. Logs: `data/validate_cleaned_pre_D0.log`,
`data/validate_cleaned_post_D0.log`.
