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

---

## 9. Bars belong to the session they trade in, by session start time

*3 November 2026 · Status: in force for `DATA_SOURCE=cleaned` · Code:
`run_backtest.py` (daily timestamps passed to `MultiResolutionAligner`)*

**Decision.** A bar belongs to the last session that *started* at or before
it, with each session starting at 17:00 Chicago time on the previous
session's date — not to the last *midnight UTC*.

**Context.** `MultiResolutionAligner` assigns each bar to the last daily
timestamp at or before it. With the legacy data a "day" was a UTC calendar
date, so midnight was the right boundary. With exchange sessions (entry 4) a
session closes at about 21:00–22:00 UTC and the next opens an hour later, so
midnight-UTC timestamps put the first two hours of session d+1 into day d.

**Why.** The end-of-day position is the position at a day's last bar, and it
earns the close-to-close return from d to d+1. Under the midnight mapping
that "last bar" was two hours into session d+1 — after part of the return it
is credited with had already happened. A lookahead in the P&L, created by
moving to session days and caught before any result was quoted on it.

**Downstream impact.** End-of-day decisions are made at the session's own
last bar. The test `test_evening_bar_belongs_to_the_next_session` pins it.

---

## 10. Daily features reach a bar from the last completed day

*3 November 2026 · Status: in force (`SLOW_FEATURE_LAG=1`, the default) ·
Code: `MultiResolutionAligner(feature_lag=)`, `get_feature_day_indices`*

**Decision.** A bar during day d sees daily features from day d−1, never
from d. Applies to the slow kernels (VRP, MacroMotion, Sentiment) in both
the combiner and the stage-1 loop, the momentum regime gate, and the
momentum direction signal. Which day a bar *belongs to* (for P&L and
scoring) is unchanged.

**Evidence.** Several daily features are computed from the day's own close:
VRP's `mom_5` and `mom_21` (`close[i] / close[i-5]`), VRP's `log_vix` (that
day's VIX close), the 20-day momentum and the momentum gate. Mapped to the
bars of the same day, a bar at 10:00 knew where crude and VIX would close at
16:00. MacroMotion was already safe (it uses returns up to d−1); the event
gate is safe (scheduled events are known in advance) and is left unlagged.
This was in the legacy pipeline too, and nothing in the falsification suite
catches it — the shuffled-target control tests for leaks into the target,
not for features that see the future of their own day.

**Alternatives.** Rewrite each daily feature to stop at d−1. More places to
get wrong, and every future daily feature would need the same care; one lag
at the point where daily meets intraday covers all of them.

**Downstream impact.** Every result before this entry used same-day daily
features. `SLOW_FEATURE_LAG=0` reproduces them (results tagged `_slowlag0`).
Falsification suite re-run on the new default.

**Measured** (cleaned CL, gaps removed, Run 25 configuration, three seeds;
Sharpe mean ± sd):

| mode | same-day features (lag 0) | last completed day (lag 1) |
|---|---|---|
| real features | 0.36 ± 1.10 | 0.76 ± 0.16 |
| pure noise | −0.31 ± 0.62 | −0.88 ± 0.96 |
| shuffled in time | −0.71 ± 1.82 | −1.01 ± 0.58 |
| shuffled target | −0.10 ± 1.95 | −0.75 ± 1.18 |
| leakage check (< 0.05) | 0.0132 | 0.0179 |

With the lag, Sharpe separates real from shuffled and from noise for the
first time on cleaned data — by the suite's own rule, on three seeds. Read it
with entry 11: the predictions themselves have no daily skill (IC about zero
or negative), so the separation is coming from something other than the
kernels forecasting next-day returns — most plausibly the momentum direction
and the volatility filter. That is the question E0 asks, and three seeds is
too few to lean on; `TODO.md` item 7 asks for ten. Logs:
`data/validate_cleaned_lag1.log`.

---

## 11. Choosing between next-bar and next-day targets by test, not by Sharpe

*3 November 2026, pooled 4 November · Status: tested — the data cannot choose; decide on design · Code:
`TARGET_CLOCK`, `scoring.py`, `compare_targets.py`*

**Decision.** Add a true next-day target (`TARGET_CLOCK=day`: the return 24
hours ahead on the bar clock — for a session's last bar, close to next
close) beside the existing next-bar one, and choose between them with
`compare_targets.py`: train on each, score both by the daily IC of the
end-of-session prediction against the next session's return, and compare
each with its own shuffled-target null across seeds.

**Context.** `FORWARD_HORIZON=1` was printed as "1-day forward returns" but
computed the next *bar's* return (~1.5 hours at 16 bars a session). Every
run so far, Run 25 included, predicted the next bar while being graded on
overnight positions (`TODO.md` item 3). The label is fixed; the default is
unchanged so earlier runs reproduce.

**Why this test.** Sharpe cannot separate real from shuffled here (entry 8),
so it cannot choose either. Scoring both targets on the same outcome — the
return the end-of-day position earns — makes them comparable; a rank
correlation keeps a few large days from carrying it; the shuffled null per
target says whether either has any skill at all.

**Power.** The no-skill standard error of a daily IC is about 1/sqrt(n). On
cleaned CL the out-of-sample window scores 168 days (±0.077), so only an IC
above ~0.15 would separate from noise — far above typical daily-return ICs.
Pooled across eight contracts (D1) it falls to roughly ±0.03. CL alone is a
dry run of the method; the binding decision waits for the pooled run.

**Dry run on cleaned CL** (gaps removed, lag 1, three seeds, 168 scored
days, no-skill s.e. ±0.077):

| training target | daily IC, real | daily IC, shuffled target | gap ÷ s.e. |
|---|---|---|---|
| next bar | −0.040 ± 0.063 | +0.022 ± 0.042 | −0.52 |
| next day | −0.018 ± 0.045 | −0.031 ± 0.045 | +0.12 |

Neither separates from its null — as the power calculation predicted. On CL
alone the choice cannot be made from results. In several folds the combiner
gives every kernel zero weight, so the prediction is constant there and the
fold has no IC. Log: `data/compare_targets_CL.log`.

**Pooled over all eight contracts** (commit `ebcb539`, purged, three seeds,
96 runs, 1,356 scored contract-days, no-skill s.e. ±0.027):

| training target | daily IC, real | daily IC, shuffled target | gap ÷ s.e. |
|---|---|---|---|
| next bar | −0.044 ± 0.012 | +0.005 ± 0.010 | −1.24 |
| next day | −0.044 ± 0.019 | +0.016 ± 0.032 | −1.35 |

Neither target separates from its null, and the two are the same to three
decimals. Both sit slightly *below* their nulls; at about 1.3 s.e. that is
not evidence of anything. No contract shows a consistent positive IC under
both targets. The results cannot choose the target, so it is chosen on
design: the position is held overnight and graded on the next session's
return, which is what the next-day target trains on. Log:
`data/compare_targets_pooled.log`; runs in `data/compare_targets_runs.jsonl`.

---

## 12. The momentum regime gate is fitted walk-forward on causal features

*3 November 2026 · Status: in force (`GATE_FIT=walkforward`, the default) ·
Code: `MomentumGate.gate_values_walk_forward`, `momentum_features(causal=)`*

**Decision.** Each day's gate comes from a model fitted only on returns
already known that day; its three momentum features use only returns up to
the row's own day and are standardised by expanding z-score; the gate
reaches out-of-sample bars by date (last completed day), not by row
position.

**Context.** The gate (a kernel ridge regression from momentum features to
next-day return, squashed to (0, 1)) sets the entry and exit thresholds —
trending or choppy — and attenuates the slow kernels in the combiner. Four
things were wrong with it, all present in the legacy pipeline:

1. **Fitted once on every day** — including every out-of-sample day — so
   each day's gate was an in-sample forecast of its own next return.
2. **The target was inside a feature.** `ema_cross` read the EMAs at
   `log_r[t]`, the return the gate is fitted to predict.
3. **Whole-sample standardisation** of the features (`feats.mean(axis=0)`),
   which is bug 5 in `POSTMORTEM.md`.
4. **Stretched by row position**: in position generation the daily gate was
   spread over the out-of-sample bars with `np.linspace`, breaking invariant
   1 in `CLAUDE.md` and pairing bars with the wrong days.

**Evidence.** Rank correlation of the gate with the next day's CL return on
cleaned data: **0.349** fitted on the full sample (it called "trending" on
50% of up days and 28% of down days), **0.017** fitted walk-forward. Without
the leak the gate barely moves (0.477–0.512 on cleaned CL). None of this was
visible to the falsification suite: the gate is always fitted on the real
returns, so shuffling the target does not touch it.

**Downstream impact.** Every result before this entry had a regime gate
that knew tomorrow's direction. `GATE_FIT=fullsample` reproduces those runs
(original features and fit, results tagged `_gatefull`).
`test_momentum_gate.py` pins that changing future prices cannot change an
earlier gate.

---

## 13. E0 tests the configuration we actually run, and the one it was written for

*3 November 2026 · Status: done on CL · Code: `PRED_SOURCE`, `run_e0.py`*

**Decision.** Run E0 in both of the pipeline's position paths.

**Context.** E0 was written from a reading of the volatility path
(`TARGET_MODE=vol`): kernels forecast volatility, direction from 20-day
momentum. The configuration every reported number and the falsification
suite use — Run 25 — is the *returns* path: the Kalman-filtered kernel
return predictions set direction and timing, the regime gate sets
thresholds, and size comes from trailing realised volatility, not from the
kernels. So "kernels gate and size, never choose direction" (`HANDOFF.md`,
`FALL_2026_TASKS.md` E0) is true of the volatility path only.

**The controls.** Replace the kernels' out-of-sample predictions and change
nothing else: in the returns path, by the trailing 28-calendar-day return
(`PRED_SOURCE=trend` — the "trend alone" floor of ablation row G1); in the
volatility path, by trailing 20-session realised volatility
(`PRED_SOURCE=trailing_vol` — E0 as written).

**Result** (cleaned CL, all fixes in, Run 25 configuration, three seeds;
mean ± sd; the rule controls are deterministic, so their spread is zero):

| path | prediction | Sharpe | daily IC | active days |
|---|---|---|---|---|
| returns (Run 25) | kernels | 0.76 ± 0.16 | −0.040 ± 0.063 | 29 |
| returns (Run 25) | trend rule | −0.03 | −0.136 | 63 |
| volatility | kernels | 0.51 ± 0.57 | +0.047 ± 0.021 | 25 |
| volatility | trailing vol | 0.39 | +0.033 | 50 |

What it means, read with the stage-2 weights saved in each run:

- **Returns path: the Sharpe is not coming from the kernels.** The
  ElasticNet combiner gives every kernel a weight of exactly zero on seed 0
  and at most 0.04 on seeds 1 and 2, so the "prediction" is mostly the
  combiner's fitted intercept — 38 distinct values over 2,505 bars on seed 0,
  a slow step function of past mean returns. Seed 0 earns Sharpe 0.56 with
  every kernel switched off. The trend rule scores worse, but it is a
  different signal, not the kernels-off version of this one. The predictions
  have no daily skill (IC about zero or negative).
- **The regime gate does not move positions in this configuration.** Walk-
  forward and full-sample gates classify 61% and 50% of bars as trending but
  give identical positions and identical results, which is why fixing its
  lookahead (entry 12) left the falsification numbers unchanged.
- **Volatility path: the kernels are not distinguishable from trailing
  volatility.** 0.51 ± 0.57 against 0.39, daily IC 0.047 against 0.033. Here
  OrderFlow does get weight (0.18–0.21 on every seed) — the only kernel that
  does anywhere.

**Downstream impact.** On CL, no kernel has shown measurable value in either
path. That is what the project's thesis predicts for one contract
(`HANDOFF.md`: the value should be in ranking across markets), and one
contract cannot settle it statistically. The next test of the idea is D1
(all eight contracts) and F1 (the cross-sectional target). A permanent
"kernels off" control — predictions replaced by the combiner's intercept —
would make this check routine in `validate_pipeline.py`. Logs:
`data/validate_cleaned_gatewf.log`, `data/e0_CL.log`.

---

## 14. One feature builder per contract; kernels a contract lacks are left out (D1)

*3 November 2026 · Status: in force · Code: `build_features_for_symbol` in
`run_backtest.py`, `SYMBOL`*

**Decision.** Everything from loading data to putting the slow layer on its
daily axis is now `build_features_for_symbol(symbol)`; `main()` calls it for
`SYMBOL` (default CL). A kernel whose data does not exist for a contract is
left out of that contract's run, never padded:

| kernel | which contracts | why |
|---|---|---|
| LOB | CL only for now | precomputed book features exist for CL; NG, HG, ZC have raw depth awaiting piece D |
| VannaCharm | CL only | the Greeks file is from crude's options (LO) |
| Sentiment | CL only | the embeddings are from crude-oil news |
| VPIN, Kyle, Hawkes, VRP, MacroMotion, EventProximity | all eight | built from the trade tape, daily bars and generic external series |

Non-CL results are tagged `_sym-<SYM>`, and their label counts the kernels
they actually used (e.g. `1fast_2slow`).

**Bar size, until D2.** CL keeps its fixed $1M so its results stay
comparable. Every other contract's threshold is the median price × size per
session over its first 20 sessions divided by `BARS_PER_SESSION` (default 16,
about CL's count) — data from inside the warm-up only, which is never scored.
D2 replaces this with a threshold that adapts session by session.

**Evidence that nothing changed for CL.** Run through the refactored path,
CL reproduces the pre-refactor runs from commit 452a01a exactly — all 41
saved arrays identical, on seeds 0 and 1. That was D1's acceptance test.

**First run of all eight** (seed 0 only, Run 25 configuration, one
directional model per contract — not yet the cross-sectional target):

| | bars | kernels with any weight | daily IC (days) | Sharpe |
|---|---|---|---|---|
| CL | 6,207 | none | −0.093 (168) | 0.56 |
| NG | 7,833 | none | −0.058 (167) | 0.59 |
| HG | 6,785 | none | +0.026 (179) | −0.57 |
| ZC | 4,828 | none | +0.116 (121) | −2.67 |
| ZT | 8,879 | OrderFlow 0.03, Macro 0.01 | −0.085 (186) | −3.89 |
| BTC | 14,973 | none | −0.087 (185) | 0.18 |
| GC | 7,890 | none | −0.043 (176) | −0.82 |
| ES | 7,468 | none | −0.124 (174) | −0.35 |

The combiner gives no kernel weight on seven of eight; averaged across the
eight, daily IC is about −0.04 on ~1,350 contract-days (s.e. ≈ 0.027), so
nothing separates from zero. Single-seed Sharpe swings from −3.9 to +0.6 —
another reason not to read it. BTC's 14,973 bars (about 40 a session, not 16)
come from its price tripling after the threshold was set: the drift D2's
adaptive threshold exists to remove. One fix was needed: the order-flow PCA
asked for 15 components from 11 features once LOB was absent; it now takes
at most as many as there are (CL's 61 → 15 unchanged, re-verified identical).

**Downstream impact.** XLE (the energy-sector ETF) is still MacroMotion's
second series for every contract; for non-energy contracts a sector-matched
series would be more meaningful, and VIX is an equity-volatility input to VRP
everywhere. Both are modelling choices for later, recorded here so they are
not mistaken for decisions.

---

## 15. Purge training rows whose label overlaps the test fold

*4 November 2026 · Status: in force (`PURGE_LABELS=1`, the default) · Code:
`PurgedWalkForward.split_indices(label_end=)`, stage 2 in `run_backtest.py`*

**Decision.** Every bar records when its label becomes known (`label_end`:
the bar at whose close it is realised). In both stages of the walk-forward a
training row is used only if its label is known by the first test bar.

**Evidence.** Stage 1 had a fixed 100-bar embargo; stage 2 had no gap at all
— it trained on rows up to the test fold and tested on the next row. That is
harmless for the next-bar target (labels one bar ahead) but not for the
others: the next-day target looks ~16 bars ahead and the 20-day volatility
target ~320, so the last training labels contained returns from inside the
test fold. It showed up as a combined volatility forecast far better than any
of its parts (ZC: +0.28 beyond trailing vol, while each kernel alone was
−0.07 to +0.10). The shuffled-target control cannot see this kind of leak:
shuffling destroys the overlap it relies on.

**Why.** Purging by label horizon is the standard fix (López de Prado,
*Advances in Financial Machine Learning*, 2018, ch. 7); an embargo sized for
one horizon fails silently on a longer one.

**Downstream impact.** Next-bar runs are unchanged — CL reproduces its
reference exactly. Every earlier next-day or volatility-mode number had the
overlap; the first pooled next-bar/next-day sweep was stopped and restarted
for this reason. `PURGE_LABELS=0` reproduces the old behaviour (`_nopurge`).
ElasticNetCV's own 3-fold inner CV is still unpurged; it only picks the
penalty, but it is the same kind of leak and is noted here for later.

---

## 16. Score volatility forecasts against trailing volatility, per kernel

*4 November 2026 · Status: in force · Code: `scoring.daily_vol_ic`,
`vol_compare.py`; every run saves `kernel_preds_oos` and `vol_scores`*

**Decision.** In volatility mode, score each kernel's own out-of-sample
forecast (not only the combination) by rank IC against what follows each
session's close — next-day |return| and next-5-day realised volatility — and
by the same IC after trailing 20-day volatility is removed from forecast and
outcome ("beyond trailing"). Compare with the shuffled-target null, pooled
across contracts.

**Why.** Volatility is persistent: trailing volatility alone forecasts the
next five days well (pooled rank IC +0.19 here). Any forecast that tracks it scores
well, so the plain IC cannot say whether a kernel knows anything. "Beyond
trailing" can. Short, non-overlapping outcome windows keep the score from
looking more certain than it is.

**Context.** The project documents describe the kernels as volatility
forecasters, but every reported run used the returns target (entry 13).
This is the first test of the kernels on the job they were built for.

**Pooled result** (commit `ebcb539` with the no-trade print fix, all eight
contracts, three seeds, 48 runs; Sentiment and VannaCharm exist on CL only):

| forecast | IC, next-5-day vol | beyond trailing | shuffled-null beyond | gap ÷ s.e. |
|---|---|---|---|---|
| Macro | +0.016 | +0.017 | +0.013 | 0.08 |
| OrderFlow | +0.016 | −0.012 | −0.002 | −0.30 |
| VRP | −0.019 | −0.023 | −0.023 | 0.01 |
| Sentiment (CL) | −0.107 | −0.050 | −0.033 | −0.14 |
| VannaCharm (CL) | −0.082 | −0.090 | −0.080 | −0.09 |
| combined | −0.095 | −0.130 | +0.061 | −4.35 |
| trailing 20-day vol | +0.188 | — | — | — |

No kernel adds volatility information beyond trailing volatility: every
"beyond trailing" score is within a fraction of a standard error of its
null. Next-day |return| tells the same story. Plain trailing volatility
beats every kernel and the combination.

The combination's −4.35 is real but is not the kernels. The stage-2
ElasticNet gives every kernel zero or near-zero weight on most contracts, so
the combined forecast is mostly its intercept: the mean realised volatility
of each fold's training window. That is a stale volatility level that steps
once per fold. Over 18 months in which volatility regimes drift, the old
level points the wrong way once trailing volatility is removed. With a
shuffled target the intercept is the same everywhere, so the null sits near
zero. The finding is that the intercept is a bad forecast, not that the
kernels forecast backwards.

**Downstream.** The volatility framing does not rescue the kernels on this
data. Any later volatility model must beat trailing volatility, not zero,
and should forecast relative to it (for example, the log of future
volatility over trailing volatility), so a stale intercept cannot score.
Log: `data/vol_compare_pooled.log`; runs in `data/vol_compare_runs.jsonl`.

---

## 17. Forecast volatility relative to trailing volatility (relvol)

*4 November 2026 · Status: tested — no kernel separates · Code:
`TARGET_MODE=relvol`, `vol_compare.py --target relvol`*

**Decision.** Add a volatility target that trains on log(forward 20-day
volatility ÷ trailing 20-day volatility), the trailing window being the 20
daily returns up to the session's close (the baseline `daily_vol_ic`
removes). Score it exactly as entry 16.

**Why.** In entry 16 the combined forecast scored −4.35 s.e. below its null
because it was mostly a stale intercept: the training window's mean
volatility level. A relative target has no level to go stale, so a forecast
can score only by what trailing volatility misses. It is the fair version of
the question the kernels were described as answering.

**Result** (commit `9322a19`, all eight contracts, three seeds, 48 runs, no
failures; Sentiment and VannaCharm on CL only):

| forecast | IC, next-5-day vol | beyond trailing | shuffled-null beyond | gap ÷ s.e. |
|---|---|---|---|---|
| Macro | +0.006 | +0.004 | −0.016 | 0.49 |
| OrderFlow | −0.020 | −0.018 | −0.003 | −0.39 |
| VRP | +0.030 | +0.029 | +0.005 | 0.60 |
| Sentiment (CL) | −0.055 | −0.052 | +0.005 | −0.51 |
| VannaCharm (CL) | +0.152 | −0.034 | −0.030 | −0.03 |
| combined | +0.036 | +0.007 | −0.007 | 0.24 |
| trailing 20-day vol | +0.188 | — | — | — |

The stale-intercept artifact is gone (combined −0.130 → +0.007), and with it
any sign of skill: every kernel is within 0.6 s.e. of its null, on
next-5-day volatility and on next-day |return| alike. VannaCharm's plain IC
of +0.15 is all trailing volatility; beyond it, nothing.

**Downstream.** On this data the kernels forecast neither direction (entry
11) nor volatility, absolute or relative. Further tuning of the kernel stack
against these targets is not justified. The next step is F1 — a
cross-sectional target across the eight contracts on established premia
(momentum, carry), with the kernels tested only as an overlay against plain
trailing-volatility sizing. Log: `data/relvol_compare_pooled.log`; runs in
`data/vol_compare_runs.jsonl` (`"target": "relvol"`).

---

## 18. Ridge on the same features: the features, not the model

*4 November 2026 · Status: tested — no skill · Code: `ridge_check.py`,
`test_ridge_check.py`*

**Question.** The kernel stack fits a flexible model on ~300 training
sessions and finds nothing (entries 11, 16, 17). Is it overfitting, or is
there nothing to find? Ask the same questions with the least flexible model.

**Method.** One row per session: OrderFlow at the session's last bar, VRP
and Macro from the last completed day (the features all eight contracts
share, at the pipeline's lag). RidgeCV, expanding window from 40% of the
sample, refit every 20 sessions, label-overlap purge, features standardised
on training rows only. Same scores as before: daily rank IC for the
next-day return; "beyond trailing" next-5-day vol for the relative-vol
target. Null: 50 retrains on the target shuffled across sessions per
contract, scored against the real outcomes. (RidgeCV's own leave-one-out
choice of penalty is not purged; with 1-day labels that is immaterial, with
20-day labels it can only flatter the real model.)

**Result** (all eight contracts, cleaned data):

| target | ridge, pooled | shuffled null, pooled | z | contracts above null's 95th pct |
|---|---|---|---|---|
| next-day return | −0.032 | −0.000 ± 0.025 | −1.24 | 0 of 8 |
| relative vol, beyond trailing | −0.082 | +0.004 ± 0.060 | −1.42 | 0 of 8 |

Ridge does no better than the kernels. The strongest single contract (CL
relative vol, +0.20) beats 88% of its nulls — about what one contract in
eight does by chance. Log: `data/ridge_check.log`.

**Conclusion.** The model is not the problem: the shared features carry no
detectable information about next-day direction or relative volatility on
this data. Tuning model capacity cannot fix that. F1 (section F of
`FALL_2026_TASKS.md`) starts from known premia for this reason; the features
are tested there only as an overlay.

---

## 19. Order-book signal exists at seconds, gone by 30 minutes, not tradeable as a taker

*5 November 2026 · Status: measured · Code: `depth_seconds.py` (runs in the
databento venv), `horizon_check.py`, `test_horizon_check.py`*

**Question.** Entries 11–18 found no daily signal. Do the order-flow and
book features carry information at all, just at a shorter horizon than we
asked about?

**Method.** Three sample months (Aug 2023, Feb 2024, Aug 2024) of `mbp-10`
depth for the four contracts that have it (CL, NG, HG, ZC), streamed into
one row per second (best quotes, sizes, top-five depth, Cont–Kukanov–Stoikov
order-flow imbalance, signed trade flow). At each horizon h from 1 s to 30
min, non-overlapping origins; features from (t−h, t] only; outcome the
**mid**-price move over (t, t+h] in ticks (trade prices bounce between bid
and ask and manufacture predictability). No window crosses a gap over 60 s
or a contract switch. Per-session Spearman IC, t-stat across sessions.
Tests include a look-ahead check: flow that moves the price only in the same
second must score ~0 as a forecast. The CL depth is the `CL.c.0` roll, not
`CL.v.0`; irrelevant within a session.

**Result** (book imbalance = (bid size − ask size) / (bid size + ask size) at
the best quotes; IC with the next-h mid move, t-stat in brackets):

| h | CL | NG | HG | ZC |
|---|---|---|---|---|
| 1 s | +0.18 (26) | +0.12 (49) | +0.18 (41) | +0.12 (74) |
| 10 s | +0.12 (18) | +0.13 (41) | +0.15 (27) | +0.22 (91) |
| 1 min | +0.05 (6) | +0.08 (15) | +0.08 (16) | +0.22 (43) |
| 5 min | +0.03 (4) | +0.02 (2) | +0.05 (4) | +0.14 (16) |
| 30 min | +0.04 (2) | +0.01 (0) | +0.03 (2) | +0.02 (1) |

- **Book imbalance predicts the mid, strongly, from 1 s to ~1 min** on all
  four contracts, and decays to nothing by 30 min. Top-five depth imbalance
  shows the same at about half the size.
- **Past order-flow imbalance and trade flow barely predict** (|IC| ≤ 0.05).
  They explain the move *as it happens* (same-window IC 0.6–0.9, the
  plumbing check) — the textbook result — but not the next one.
- **Short-term mid reversal**: the last h seconds' move predicts a small
  opposite move (IC −0.03 to −0.10).
- **Not tradeable by crossing the spread.** Trading the top/bottom 10% of
  book imbalance earns 0.1–0.4 ticks of mid move on average; the median
  spread is 1 tick, so a taker loses ~0.6–0.9 ticks per round trip before
  fees. This is the information market makers use to decide which side to
  quote and when to pull a quote, not a directional strategy.

**Conclusion.** The features are not empty — they are informative at a
horizon of seconds and decay well before the daily horizon the pipeline
trades, which is why entries 11–18 found nothing. For the club: (1) the
daily RKHS strategy should not expect order-flow features to help; (2) the
honest use of the depth data is execution — timing entries of a daily
strategy (piece D could test whether waiting for favourable book imbalance
saves part of the spread); (3) the slow kernels' daily nulls stand on their
own. Log: `data/horizon_check.log`; table `data/horizon_check.csv`.

---

## 20. F1 rule baselines: momentum has the right sign, the sample is too short

*5 November 2026 · Status: measured; no forecast passes the fixed rule ·
Code: `f1.py`, `scoring.cross_sectional_ic`, `test_f1.py`*

**Method.** Each session, rank the eight contracts by a causal forecast;
the outcome is each contract's next-session return over its trailing
20-session volatility (so the volatile contracts do not own the extreme
ranks). Score: Spearman IC across contracts per session, averaged, t-stat
across sessions (378 shared sessions; the within-session shuffled null has
mean zero). Rule fixed in advance: a forecast counts only at t ≥ 2.
Forecasts: vol-scaled 21/63/126-session momentum, one-day reversal, carry
from the switch gaps, and the expanding average of each ("static": a fixed
tilt rather than timing). The rank-weighted long–short Sharpe is gross of
costs and only describes the IC.

**Result.**

| forecast | mean IC | t | L/S Sharpe (gross) |
|---|---|---|---|
| mom21 | +0.037 | 1.78 | 1.61 |
| mom63 | +0.024 | 1.06 | 1.17 |
| mom126 | +0.032 | 1.24 | 0.72 |
| rev1 | +0.008 | 0.37 | 0.07 |
| carry (switch gaps) | −0.015 | −0.66 | 0.12 |
| static mom126 | +0.059 | 2.18 | 1.53 |

- Momentum is positive at every lookback, at the size the literature
  reports (cross-sectional ICs of a few hundredths), but no single lookback
  reaches t = 2. With ~378 sessions the resolvable IC is about ±0.04: the
  sample is too short to confirm an effect of the known size.
- Static mom126 crosses t = 2, but it is one of ten forecasts (a
  multiple-testing bar is about t = 2.8), and a fixed tilt toward the
  contracts that rose over this particular 18 months (bitcoin, gold,
  equities in 2023–24) is a description of the sample, not a forecast. It
  does not count.
- Carry from switch gaps shows nothing, and is a weak measurement: 31 of the
  85 switches happen at a session's first trade, so their gap includes the
  overnight move as well as the term spread.

**Downstream.**
1. The binding constraint for F1 is history, not modelling. Daily
   settlement prices for the eight contracts over 15–20 years would resolve
   ICs of ±0.01 and are cheap (Databento `ohlcv-1d` or `statistics` on the
   continuous symbols) — a purchase, so Rebecca's call. Those runs need no
   ticks.
2. Proper carry needs the second contract's price at the same moment
   (settlements for `.v.1`/`.n.1`); the same purchase could include it.
3. **D0 caveat found here:** when a switch happens at a session's first
   trade (31 of 85), the gap D0 removes includes a real overnight move, so
   one genuine session return per such switch is removed with the roll.
   Fixing it also needs the second contract's price at the old contract's
   last trade. Logged in `TODO.md`.
4. The kernels, as an F1 overlay, would be tested against mom21 — but with
   the base effect itself unresolved on this sample, that test has no power
   either. Wait for longer history.
Log: `data/f1_baselines.log`; table `data/f1_baselines.csv`.

---

## 21. Execution timing: waiting out an adverse book saves a few percent of the spread

*5 November 2026 · Status: measured; small, real, free · Code:
`exec_timing.py`, `test_exec_timing.py`*

**Question.** Entry 19's book-imbalance signal is far below the spread, so
it cannot pay for a trade. Can it make a trade we were doing anyway — a
daily strategy's rebalance — cheaper?

**Method.** Same per-second book (four depth contracts, three sample
months). A decision every 5 minutes where a quote exists and the spread is
at most 2 ticks (normal markets; pre-open and thin moments, with spreads up
to 87 ticks, are not when a daily strategy trades). At each, price a buy and
a sell of one contract — drift cancels between them — against the mid at
the decision. Policies: cross now; wait up to W seconds while book
imbalance leans against us (≤ −θ for a buy, ≥ +θ for a sell), then cross;
cross at a random second within W (control). Taker orders only — a resting
order's fill depends on queue position, which this data cannot show.

**Result** (θ = 0.3, wait up to 60 s; saving vs crossing at once, in ticks
per contract, t across sessions):

| | cost of crossing now | book policy saves | random wait saves | typical wait |
|---|---|---|---|---|
| CL | 0.61 | +0.035 (t 2.5) | −0.014 | 1 s |
| NG | 0.69 | +0.033 (t 11) | −0.002 | 4 s |
| HG | 0.70 | +0.030 (t 15) | +0.000 | 1 s |
| ZC | 0.53 | +0.056 (t 35) | +0.001 | 10 s |

- The book policy saves on every contract, every window (10 s–5 min) and
  both thresholds; waiting at random saves nothing. So the saving is the
  book's information, not the act of waiting.
- It is small: about 5% of the half-spread on CL, NG and HG, and about 10%
  on corn (more with a 5-minute window: 0.075 ticks, 14%). In dollars,
  roughly $0.30–0.95 per contract per trade.
- The wait is short — a few seconds on average — so it barely delays the
  daily strategy.

**Downstream.** Worth having as a free execution rule once there is a daily
strategy to execute; not a reason to keep the order-flow kernels in the
prediction model. Piece D, re-aimed at execution, has its first result. A
fuller version would test resting limit orders, which needs queue position
(`mbo` data). Log: `data/exec_timing.log`; table `data/exec_timing.csv`.

---

## 22. F1 on 14 years of settlements: no timing signal survives; drift tilts are allocations

*5 November 2026 · Status: measured · Code: `pull_daily_history.py`,
`daily_settlements.py` (both in the databento venv), `f1_long.py`,
`test_f1.py`*

**Data.** Daily bars and settlement statistics for the front and second
contract of all eight, June 2010 – December 2024 (bitcoin from November
2017), same roll rules as the tick data; files and manifests in
`data/raw/<ROOT>/daily_history/`. Statistics went through batch jobs
because streaming them ran at minutes per megabyte. Settlement = stat_type
3, last update per contract per trading date.

**Better measurement than entry 20.** Returns are roll-free: each date's
return is its front contract's settlement over *the same contract's*
settlement the date before (from the second-contract file when it was not
yet front), so no switch gap is ever in a return and no overnight move is
removed with one. Carry is measured: log(front ÷ second settlement) on the
same date, annualised by the contract's listing cycle. 3,674 dates with at
least five contracts; the resolvable IC falls from ±0.04 to about ±0.013.

**Result** (same forecasts, outcome, score and rule as entry 20; halves
2010–17 and 2018–24):

| forecast | IC | t | IC 2010–17 (t) | IC 2018–24 (t) |
|---|---|---|---|---|
| mom21 | +0.004 | 0.6 | −0.009 (−1.0) | +0.018 (1.9) |
| mom63 | +0.013 | 1.8 | −0.000 (0.0) | +0.027 (2.8) |
| mom126 | +0.017 | 2.4 | +0.009 (0.9) | +0.025 (2.5) |
| rev1 | +0.031 | 4.5 | +0.042 (4.2) | +0.019 (2.0) |
| carry | +0.004 | 0.7 | +0.023 (2.4) | −0.015 (−1.7) |
| static mom21 | +0.025 | 3.9 | +0.032 (3.4) | +0.018 (2.0) |
| static carry | +0.015 | 2.4 | +0.027 (2.9) | +0.003 (0.3) |

**Checks on the two that pass.**
- *One-day reversal is an artefact of settlement times.* The eight settle
  hours apart (copper ~12:00 Chicago, gold 12:30, corn 13:15, crude and gas
  13:30, Treasuries 14:00, ES and bitcoin 15:00). News between two
  settlements reaches one contract today and another tomorrow, which looks
  exactly like cross-sectional reversal. Among the four that settle within
  45 minutes (CL, NG, ZC, ZT) it shrinks to +0.016 (t 1.65); among the four
  spread across the afternoon it is +0.042 (t 3.0); skipping a day it is
  +0.005 (t 0.7). Not tradeable.
- *Static tilts are allocations, not forecasts.* The expanding average of
  momentum or carry is a near-fixed ranking of the contracts by their past
  drift — equities and gold rose, natural gas futures decayed (contango).
  It holds in both halves, but among the four synchronous settlers it is
  +0.004 (t 0.5): it is a few contracts' persistent drift, i.e. known risk
  premia (equity premium, the cost of carrying contango), captured by
  holding, not by timing.

**Conclusion.** With 14 years the test has power, and no *time-varying*
forecast passes: momentum is weak and only in 2018–24, carry flips sign
between halves, reversal is a timing artefact. What survives is static
drift — a buy-and-hold tilt toward known premia. That is a legitimate
portfolio but not a forecasting result, and gives the kernels nothing to
time. Any future cross-sectional test must use prices sampled at one common
moment (e.g. 1-minute bars at 12:00 Chicago), not settlements.
Log: `data/f1_long.log`; table `data/f1_long.csv`.

---

## 23. At 1–5 minutes the features forecast volatility beyond the standard benchmark; direction stays below cost

*5 November 2026 · Status: measured · Code: `minute_check.py`,
`test_minute_check.py`*

**Question.** Put the pipeline's kind of features on a short clock. Do they
forecast the next 1–5 minutes' direction or volatility, and does a flexible
(kernel) model beat a linear one?

**Method.** 1-minute bars from the per-second book (entry 19; CL, NG, HG,
ZC; three sample months, 75–86 sessions, 80–90k bars each). Fifteen causal
features: book and top-five depth imbalance, order-flow imbalance and signed
trade flow over 1 and 5 minutes, past mid moves, a VPIN-style flow
imbalance and a Kyle's-lambda-style price impact over 30 minutes, a
Hawkes-style trade-intensity burst, realised variance over 1, 5 and 30
minutes, the spread. Walk-forward by session (expanding from 30% of
sessions, refit every 5, standardised on training rows). Models: RidgeCV,
and random Fourier features of an RBF kernel + RidgeCV (the pipeline's kind
of kernel). Volatility benchmark: HAR (Corsi 2009) — ridge on 1/5/30-minute
realised variance — plus hour of day, which every volatility model also
gets, so "beyond HAR" isolates the order-flow features. Scores: per-session
Spearman IC on non-overlapping origins, t across sessions; "beyond" is the
rank partial correlation after removing the benchmark from forecast and
outcome. Tests include a causality check and a planted signal.

**Direction** (IC, t; edge = ticks per round trip on each session's
top/bottom 10%, against a ~1-tick cost):

| | book alone, 1 min | ridge, 1 min | kernel, 1 min | book alone, 5 min | ridge, 5 min |
|---|---|---|---|---|---|
| CL | +0.084 (8), 0.27 | +0.061 (11), 0.21 | +0.080 (7), 0.32 | +0.028 (2), 0.14 | +0.015 (1), −0.22 |
| NG | +0.067 (12), 0.19 | +0.066 (12), 0.19 | +0.065 (12), 0.18 | +0.016 (1), 0.28 | +0.032 (3), 0.19 |
| HG | +0.089 (16), 0.32 | +0.089 (18), 0.31 | +0.078 (15), 0.26 | +0.040 (4), 0.37 | +0.030 (3), 0.17 |
| ZC | +0.248 (41), 0.27 | +0.248 (40), 0.27 | +0.232 (33), 0.25 | +0.164 (15), 0.33 | +0.154 (14), 0.33 |

Fifteen features and a kernel add nothing over book imbalance alone, and
every edge is a third of the cost or less.

**Volatility** (next-h log realised variance; IC, then rank partial beyond
the HAR benchmark with t):

| | HAR, 1 min | ridge, 1 min (beyond HAR) | kernel, 1 min (beyond HAR) | HAR, 5 min | ridge, 5 min (beyond HAR) | kernel, 5 min (beyond HAR) |
|---|---|---|---|---|---|---|
| CL | 0.780 | 0.782 (+0.089, t 6) | 0.697 (+0.078, t 15) | 0.883 | 0.886 (+0.169, t 18) | 0.795 (+0.132, t 13) |
| NG | 0.700 | 0.706 (+0.128, t 20) | 0.629 (+0.057, t 9) | 0.801 | 0.810 (+0.202, t 18) | 0.731 (+0.122, t 9) |
| HG | 0.767 | 0.767 (+0.110, t 13) | 0.575 (+0.041, t 6) | 0.854 | 0.862 (+0.217, t 20) | 0.677 (+0.041, t 4) |
| ZC | 0.458 | 0.472 (+0.131, t 30) | 0.420 (+0.095, t 15) | 0.669 | 0.673 (+0.095, t 8) | 0.612 (+0.121, t 9) |

- Short-term volatility is very forecastable, and the HAR benchmark already
  gets most of it (IC 0.46–0.88).
- The order-flow features carry real information beyond HAR on all four
  contracts at both horizons (partial +0.09 to +0.22, t 6–30). In plain IC
  the gain is small (+0.00 to +0.01) — they refine, they do not transform.
- The kernel model is worse than ridge in plain IC on every row and adds
  less beyond HAR on most: with these sample sizes the flexibility costs
  more than it finds.

**Conclusion.** This is the first place in the project where the features
earn their keep: they improve a standard volatility forecast at 1–5
minutes. It is a market-making or options-hedging input (sizing quotes,
hedging gamma), not a directional strategy, and the linear model is enough
— the kernel is not justified even here. Together with entries 19–22: the
features' information is real, short-horizon and about volatility; nothing
in this data supports a daily directional strategy. Log:
`data/minute_check.log`; table `data/minute_check.csv`.

---

## 24. Volatility ripples: the clock, the echo, and scheduled reports

*7 November 2026 · Status: measured · Code: `vol_ripples.py`*

**What.** Three descriptions of 1-minute realised variance on the four depth
contracts (three sample months): its average by half-hour of the Chicago
day; its autocorrelation at lags of 1 minute to 4 hours, with and without
each minute-of-day's mean removed; and crude's variance minute by minute
around the Wednesday 9:30 Chicago EIA inventory release, against the same
minute on other weekdays.

**Expiry days excluded.** CL's depth follows the calendar roll (`CL.c.0`),
so it holds the expiring contract through its last session, when the book
empties (spread up to $5.72). Three such afternoons made the 13:00 slot read
17× average; each contract's final session before a switch is now dropped
(it reads 2×). Earlier book-based results (entries 19, 21, 23) include those
few sessions; their rank-based, per-session scores are not driven by them,
but this is the cleaner rule for future work.

**Result.**
- *Clock:* variance runs 2–5× average from 7:30 to 10:00 Chicago; gas has a
  second peak at its Thursday 9:30 storage report, corn at 8:30 (open) and
  11:00 (USDA reports), crude and gas about 2× in the settlement window.
  Copper also peaks in the London morning and the Asian evening.
- *Echo:* with the clock removed, 1-minute autocorrelation is 0.21–0.39,
  falling fast over five minutes, with a tail of about 0.17 at four hours on
  copper and corn (crude 0.07, gas 0.02). Without removing the clock the
  echo looks about twice as strong.
- *EIA:* the release minute is 9.6× normal, the next two about 3×, then
  1.5–2× for 15–30 minutes; the half-hour before runs below normal.
  Thirteen Wednesdays, so the multiples are rough.

**Downstream.** Explains why HAR forecasts minutes so well (fast drop, long
tail) and supports an execution rule of not trading in the minutes after a
scheduled release. Outputs: `data/vol_ripples_{clock,echo,event}.csv`.


---

## 25. Volatility spills over between markets, with equities as the hub; gas and corn stand apart

*7 November 2026 · Status: measured · Code: `spillover.py`, `test_spillover.py`*

**Question.** Does one market's recent volatility help forecast another's
over the next 1 and 5 minutes, beyond the target's own history?

**Method.** Cleaned 1-minute bars for all eight contracts, 8:30–13:15
Chicago (the hours all eight trade), on the 305 dates all eight trade.
Minute variance is the Parkinson range estimator. For each target B,
walk-forward by date: ridge on B's own 1/5/30-minute log variance and the
half-hour of day (the HAR benchmark), against the same plus the other seven
contracts' 1- and 5-minute variance. Score: per-date rank partial
correlation of the cross forecast with the outcome beyond the own forecast,
t across dates; then one leader at a time. Expiry sessions dropped (entry
24). The floor added before taking logs comes from the first five
(never-scored) dates only — a full-sample median was caught by the
causality test.

**Result** (beyond own HAR; t):

| | 1 min, all others | strongest leader | 5 min, all others | strongest leader |
|---|---|---|---|---|
| CL | +0.051 (12.4) | ES | +0.094 (9.5) | ES |
| NG | +0.007 (1.4) | ZT (t 2.3) | +0.025 (2.4) | GC (t 2.9) |
| HG | +0.064 (13.2) | ES | +0.118 (12.3) | GC |
| GC | +0.068 (15.6) | ES | +0.109 (11.4) | ES |
| ZC | +0.010 (1.7) | NG (t 1.8) | +0.013 (1.4) | ZT (t 2.4) |
| ZT | +0.038 (6.4) | ES | +0.076 (8.7) | ES |
| ES | +0.043 (9.9) | ZT | +0.056 (5.6) | HG |
| BTC | +0.024 (5.7) | GC | +0.039 (4.3) | GC |

- Volatility does travel between markets: six of eight contracts get real
  information from the others (t 4–16), and the S&P future is the most
  frequent leader.
- Natural gas and corn are largely their own markets: neither passes at
  1 minute, and at 5 minutes each has one weak leader just past t = 2.

**Caveat on size.** Partial correlations show that new information exists;
they overstate how much a forecast improves (entry 28 measured both on the
kernel: partial +0.10 to +0.18, plain IC +0.002 to +0.014). Read the
numbers above as "present and ordered", not as forecast gains.

**Downstream.** For a multi-market volatility model, ES and GC are the
inputs worth adding to metals, crude and rates; gas and corn need their own
drivers (weather, storage, USDA). Outputs: `data/spillover_{all,pairs}.csv`,
`data/spillover.log`.

---

## 26. Implied volatility beats our order flow at the daily horizon; order flow still adds at five days

*7 November 2026 · Status: measured · Code: `implied_vol.py`,
`vol_vs_implied.py`*

**Question.** At the daily horizon, where entries 11–18 found nothing
beyond trailing volatility, does the options market already know what our
features know?

**Method.** Crude's daily at-the-money implied volatility from LO option
settlements (Databento statistics): forward by put–call parity, Black-76
on the ATM straddle, nearest option month with at least 20 days left.
Option expiry is computed from CME's rules (futures last trade 3 business
days before the 25th, options 3 business days before that); this was
first written as 1 day and corrected after checking all 18 option months
in the file. 379 dates, median 29.3%. Target: crude's log realised
variance over the next 1 and next 5 sessions (Parkinson, from cleaned
1-minute bars, expiry sessions dropped). Walk-forward ridge, label-horizon
purge: HAR; HAR + implied; HAR + implied + end-of-session order-flow
summaries from the per-second book.

**Result** (360 sessions; no-skill s.e. ≈ 0.068):

| | next session IC | beyond the previous model | next 5 sessions IC | beyond the previous model |
|---|---|---|---|---|
| HAR | 0.786 | | 0.714 | |
| + implied | 0.801 | +0.249 | 0.744 | +0.301 |
| + order flow | 0.799 | +0.081 (≈1.2 s.e.) | 0.757 | +0.211 (≈3.1 s.e.) |
| implied alone | 0.733 | | 0.723 | |

**Conclusion.** Implied volatility is the strongest single addition to HAR
at both horizons. Order flow adds nothing measurable for the next session
once implied is in, but does add for the next five sessions (plain IC
0.744 → 0.757). One market, 18 months: suggestive, not settled. It fits
entries 23 and 29 — what order flow knows about volatility is mostly
short-lived — and suggests that a daily volatility product should start
from implied volatility.

---

## 27. The 1–5 minute results hold on 18 months

*7 November 2026 · Status: measured · Code: `minute_check.py --step 20`*

**What changed.** Entry 23 used three sample months. With the full
18 months of depth (72 files, entry 19), the same test was run on 360k–530k
minute bars per contract. Refit interval raised from 5 to 20 sessions: at
5, each refit retrains on all earlier sessions, so cost grows with the
square of the sample (a first attempt ran two hours without finishing).
The models are unchanged, only refreshed less often.

**Result.**
- *Direction:* book imbalance alone matches ridge and kernel. 1-minute IC
  +0.065 to +0.068 on CL, NG, HG (t 17–30), +0.26 on ZC (t 89); 5-minute
  +0.02 to +0.03, ZC +0.17. Edge 0.1–0.3 ticks per round trip against a
  ~1-tick cost — unchanged.
- *Volatility:* ridge on the features beats HAR on every contract at both
  horizons, partial +0.07 to +0.21 (t 17–59), the same range as on three
  months.
- *Untuned kernel:* still below ridge in plain IC on every row.

**Conclusion.** Entry 23's findings were not an artifact of a small sample.
Outputs: `data/minute_check_full.{log,csv}`.

---

## 28. Kernel retest: a tuned kernel helps, but "beyond ridge" overstates by far

*7 November 2026 · Status: measured · Code: `kernel_retest.py`,
`kernel_paired.py` (+ tests)*

**Question.** With six times the data, does a kernel model beat ridge?
Rule fixed before running: a kernel model earns a place for a target only
if it adds beyond ridge with t ≥ 2 on at least 3 of 4 contracts.

**Method.** Same features and walk-forward as entry 27 (refit every 20
sessions). Models: ridge; untuned RBF kernel (400 random features); tuned
kernel (1,000 features; γ and penalty chosen on the last 20% of each
training window); hybrid (ridge plus a tuned kernel fitted to ridge's
residuals). Kernels train on at most 120,000 evenly spaced rows.

**Result by the rule.** Tuned and hybrid pass on 1- and 5-minute volatility
(4/4 contracts each, partial +0.10 to +0.18, t 20–54) and on 1-minute
direction (3/4, partial +0.005 to +0.02). Nothing passes on 5-minute
direction.

**Why that is not the end.** The *untuned* kernel also "passed" on
volatility (t 12–33) although its IC is far below ridge's everywhere
(e.g. copper 0.59 vs 0.79), and the hybrid's IC was only 0.000–0.014 above
ridge's. So a second check, written before looking further:
- *paired:* per session, IC(model) − IC(ridge) on the same minutes; t
  across sessions. This asks whether the forecast is better.
- *placebo:* a second ridge fitted on the kernels' thinned rows. It holds
  no new information; its partial-beyond-ridge was −0.002 to +0.009, so
  the metric does not reward a copy of ridge.

Paired IC gain of the hybrid over ridge: CL +0.003 (5 min; 1 min −0.002,
n.s.), NG +0.003/+0.004, HG +0.003/+0.002, ZC +0.014/+0.006 — significant
on 7 of 8 rows (t 6.7–19).

**Conclusion.** The tuned kernel finds something ridge misses, but the gain
is roughly 1/30 of what the partial correlation implies. A forecast that
differs from ridge in shape gets credit from the partial correlation for
the shape, not just for new information. **Rule for future work:** a
model's improvement over a baseline is reported as the paired IC
difference; partial correlations may show that information exists but are
not quoted as its size. Tuning chose the smoothest γ on offer almost every
time, which led to entry 29. Outputs: `data/kernel_retest.{log,csv}`,
`data/kernel_paired.{log,csv}`.

---

## 29. A fairly set-up kernel improves short-term volatility forecasts on every contract; gains level off after about a year

*7 November 2026 · Status: measured; supersedes the kernel conclusion of
entry 23 · Code: `kernel_fair.py`, `test_kernel_fair.py`*

**Context.** Entry 28's kernel kept choosing the smoothest setting. Three
flaws in its setup could have forced that:
1. *Hour of day.* 24 standardised one-hot columns (about ±4.8 each) outweighed
   all 15 order-flow features in the kernel's distances, so only a very
   smooth, near-linear kernel avoided treating each hour as separate.
2. *Too few random features* (1,000), which make sharp kernels look noisy
   in tuning.
3. *Heavy-tailed features,* which let a few extreme minutes dominate RBF
   distances.

**Decision.** Kernel inputs are the 15 features, clipped at the training
window's 0.5/99.5 percentiles and standardised, plus sin/cos of time of
day; the hour's one-hot columns stay in ridge only. Ridge gets the same
clipped features, so the comparison stays fair. 4,000 random features
(2,000 while tuning), computed in chunks so every training row is used;
γ grid extended ten times smoother. Hyperparameters tuned once per refit
(every 40 sessions) on the most recent 60,000 rows of ridge residuals.
Learning curve: ridge and hybrid each trained on the last 60, 120, 240
sessions and all history, scored on the same rows by paired IC gain.

**Result** (hybrid − ridge, paired per-session IC; t):

| | 1 min: 60 / 120 / 240 / all | 5 min: 60 / 120 / 240 / all |
|---|---|---|
| CL | +0.0015 / +0.0020 / +0.0028 / **+0.0029** (9.9) | +0.0029 / +0.0034 / +0.0037 / **+0.0037** (10.5) |
| NG | +0.0027 / +0.0037 / +0.0042 / **+0.0043** (18.9) | +0.0031 / +0.0037 / +0.0040 / **+0.0041** (12.8) |
| HG | +0.0035 / +0.0044 / +0.0049 / **+0.0049** (12.8) | +0.0017 / +0.0023 / +0.0029 / **+0.0029** (13.1) |
| ZC | +0.0178 / +0.0197 / +0.0189 / **+0.0190** (25.1) | +0.0041 / +0.0056 / +0.0059 / **+0.0072** (9.8) |

- The hybrid beats ridge on all 8 rows (t 9.8–25.1); crude at 1 minute,
  negative in entry 28, is now positive. Gains are 20–60% larger than
  entry 28's.
- Tuning now mostly chooses medium sharpness (γ × 0.3–1), rarely the new
  smoothest values. The flattening was the setup, and there is real
  curvature in how order flow relates to the next minutes' volatility.
- Gains rise from 3 to about 12 months of history and then level off;
  only corn at 5 minutes is still rising. Caveat: early in the test there
  is less than 240 sessions of history, so "240" and "all" coincide there.

**Conclusion.** The kernels have a measured use: layered on the linear
model, a properly set-up RBF kernel improves 1–5 minute volatility
forecasts on all four depth contracts. The gain is small (IC +0.003 to
+0.005; corn +0.007 to +0.019) and statistically solid. On this evidence a
longer history would add little for CL, NG and HG; corn might still
benefit. The kernels still add nothing to direction (entry 28), and
direction remains below cost (entry 27). Output: `data/kernel_fair.{log,csv}`.

## 30. A market maker would use the volatility forecast to stand aside; it removes 70–85% of the loss but does not make quoting pay

*8 November 2026 · Status: measured · Code: `mm_backtest.py`,
`test_mm_backtest.py`, `mm_suite_check.py`, `mm_scorecard.py`,
`test_mm_scorecard.py` (run of `mm_backtest.py` with sha256 8182568…,
logged in `data/mm_backtest.log`)*

**Correction (same day).** The first version of this entry (commit
45240c3) booked cash flows to the session in which each fill happened. A
few segments run one second past the 22:00 UTC session boundary, so a
position opened in one session and closed in that last second split its
buy and sell across two sessions, leaving one-second "sessions" of about
±$71,000. Totals were unaffected, but every per-session statistic (t,
Sharpe, bootstrap intervals) was distorted. Positions are now closed at
every session boundary, sessions with less than an hour of test data are
not scored, and a test covers it. All numbers below are from the
corrected run; the verdict did not change.

**Context.** Entries 27–29 show the 1-minute volatility forecast is real but
that no directional use of the features clears a 1-tick cost. A market
maker does not pay the spread; its cost is adverse selection, being filled
just before the price runs through its quote. The forecast sorts minutes by
exactly that risk (in crude's calmest tenth of minutes the mid moves 0.8
ticks; in the loudest, 4.7).

**Decision.** Simulate one-lot quoting at the best bid and ask on the
per-second book over the test period of the kernel_paired forecasts
(260–273 sessions per contract). Position limit ±1 lot; a position held
120 s, or open at a segment's end or a session boundary, is closed by
crossing the spread; fees $1 per contract per side. Without our queue
position, two fill rules bracket the truth: *through* (filled only when
the price trades through our level: certain, pessimistic) and *queue*
(also filled when the displayed size at our level has shrunk by more than
the size ahead of us when we joined: optimistic, since it counts every
cancellation as ahead of us). Policies: always quote; random pulls (as
often as the gate); **vol** (quote only in minutes whose ridge forecast is
below the median of its previous 20,000 forecasts, causal); vol with the
ridge + kernel forecast; book imbalance side-skipping (|imbalance| > 0.5);
vol plus book. Seconds with one side of the book empty (0.2–2.7%) are
dropped and split their segment. **Rule, fixed and logged before running:**
the gate earns a place if vol beats both always and random (paired t ≥ 2)
on ≥ 3 of 4 contracts under both fill rules; a policy goes on to the risk
layers only if its own net profit is positive with t ≥ 2 under the through
rule.

**Result** (net dollars per session per lot, through / queue; profit per
fill before fees in ticks):

| | always | vol gate | vol − always (t) | vol − random (t) | kernel gate − ridge gate (t) |
|---|---|---|---|---|---|
| CL | −39,962 / −32,509 (−0.33 / −0.17 t) | −6,450 / −3,116 (−0.18 / +0.004 t) | +33,512 (27.2) | +14,769 (23.8) | +158 (5.2) |
| NG | −16,463 / −13,497 (−0.37 / −0.21 t) | −2,546 / −1,425 (−0.24 / −0.05 t) | +13,917 (25.0) | +6,324 (22.2) | +26 (3.2) |
| HG | −28,116 / −16,346 (−0.19 / −0.04 t) | −6,058 / −858 (−0.13 / +0.06 t) | +22,059 (22.6) | +9,274 (18.2) | +192 (6.6) |
| ZC | −2,192 / **+682** (−0.25 / +0.13 t) | −626 / +51 (−0.25 / +0.09 t) | +1,566 (32.6) | +612 (25.0) | +14 (4.8) |

(Comparison columns under the through rule; the queue rule agrees except
for corn, below.)

- **The gate earns a place by the rule:** 4/4 contracts under through, 3/4
  under queue. It quotes half the minutes but takes a quarter of the
  fills, and on CL, NG and HG each fill is less toxic (crude's 60-second
  markout −0.18 ticks against −0.33). Random pulls halve the fills but not
  the toxicity, and remove about half the loss; the gate removes 70–85%.
  A stationary block bootstrap of the per-session differences (mean block
  5 sessions; first two warm-up sessions dropped) gives 95% intervals that
  exclude zero for every comparison in the table.
- **No policy makes money under the through rule,** so none goes to the
  Monte Carlo and stress layers. The losses are steady, not occasional:
  quoting crude all the time loses on 95% of sessions, and with the gate
  on 92%; natural gas and corn lose every session. Deflated Sharpe
  probability ≈ 0 for every policy under the through rule (48
  configurations counted as trials). Full scorecard (Sharpe, Sortino,
  drawdown, profitable sessions, win/loss, best and worst session) in
  `data/mm_scorecard.csv`.
- **Corn is the exception, and it is about the queue.** Under the queue
  rule, quoting corn all the time earns +$682 a session on 92% of
  sessions (book side-skipping +$867, annualised Sharpe 22): corn's tick
  is wide for its volatility (two-thirds of minutes the mid does not
  move). There the gate *loses* money by standing aside. Under the through
  rule the same quoting loses every session. So whether corn pays depends
  entirely on queue position, the thing we cannot see and the thing fast
  market makers pay to win.
- **The kernel earns a small practical place.** Gating on the ridge +
  kernel forecast instead of ridge alone saves $14–192 a session on all
  four contracts (t 3.2–7.3 under both rules), 1–3% of the remaining loss.
  Part of it is quoting slightly less often, but on corn under the queue
  rule, where fills are profitable, the kernel gate has the same fill
  count and a better profit per fill (+0.099 against +0.094 ticks).
- **Book imbalance helps only alongside the gate.** On its own, skipping
  the side the book leans against barely helps (crude −$36,032 against
  −$39,962). On top of the gate it saves a further 8–13% of the loss on
  CL, NG and HG and 60% on corn under the through rule (t 20–33); under
  the queue rule it is mixed and costs money on copper.

**Conclusion.** The volatility forecast does what a market maker would want
from it: it tells it when to stand aside, removing 70–85% of the loss from
quoting blind and far more than standing aside at random. It does not make
one-lot quoting at a 1-second reaction time profitable on CL, NG or HG
under either fill rule after fees. Corn might pay a market maker near the
front of the queue. Limits: one snapshot per second (real market makers
react in microseconds, so our quotes are picked off more than theirs); no
queue position; one lot; fees assumed. Output:
`data/mm_backtest.{log,csv}`, `data/mm_sessions_<SYM>.csv`,
`data/mm_suite_check.{log,csv}`, `data/mm_scorecard.{log,csv}`,
`data/mm_equity.csv`.

## 31. Speed, queue position and fees: what the market-making backtest says a firm would need

*8 November 2026 · Status: measured · Code: `mm_latency.py`,
`test_mm_latency.py`, `simulate(..., delay=)` in `mm_backtest.py` (run
logged in `data/mm_latency.log`, mm_latency.py sha256 e80f8b5…)*

**Context.** Entry 30: the volatility forecast cuts a passive quoter's
losses by 70–85% but no policy pays after fees. The question for the
final presentation is what a well-funded firm would need to make it pay.

**Decision.** (1) *Speed:* rerun entry 30's quoting (always, and the
forecast gate; pessimistic "through" rule) with our view of the book and
the policy's flags delayed by 0, 1, 2, 5, 10 and 30 seconds, while fills
happen against the real book. Reaction time is 1 s plus the delay. A
stale bid above the real best bid is picked off at its price the next
second; a stale bid at or above the real ask buys at that ask (asks
symmetric). With no delay the simulator is unchanged: checked equal to the
committed version on 40 random books, and covered by tests. The queue rule
is not defined with a delay. (2) *Fees and queue position:* from entry
30's results, the fee per side at which each policy breaks even
(`data/mm_breakeven.csv`), and a bridge from quoting blind to the best case
the data allows: + forecast, + front of the queue (optimistic rule),
+ no fees.

**Result.**

| | gated loss per fill, 1 s → 31 s (ticks) | gated loss per session, 1 s → 2 s | blind loss per fill, 1 s → 31 s | best case (gate, front of queue, no fees) | break-even fee, gate at front of queue |
|---|---|---|---|---|---|
| CL | −0.182 → −0.268 (+47%) | −$6,450 → −$8,950 (+39%) | −0.327 → −0.333 | +$145 | $0.04 |
| NG | −0.237 → −0.269 (+14%) | −$2,546 → −$3,428 (+35%) | −0.370 → −0.378 | −$466 | needs a $0.49 rebate |
| HG | −0.129 → −0.233 (+81%) | −$6,058 → −$9,010 (+49%) | −0.192 → −0.289 | +$2,497 | $0.74 |
| ZC | −0.253 → −0.283 (+12%) | −$626 → −$806 (+29%) | −0.249 → −0.311 | +$350 | $1.17 |

- Slower reaction makes every gated fill more expensive on all four
  contracts, and the forecast's advantage shrinks (crude: 0.145 ticks per
  fill better than blind at 1 s, 0.065 at 31 s). The curve is steepest at
  the fast end: 1 → 2 s costs 29–49% more per session. Below 1 s cannot be
  measured with one snapshot a second.
- Net loss per session falls again at long delays because stale quotes sit
  away from the market and rarely fill; loss per fill is the fair measure.
- Blind quoting barely worsens on CL and NG: under the through rule every
  blind fill is already one where the price runs over us.
- Under the pessimistic rule every policy would need a rebate of $1.57 to
  $3.96 a side to break even.
- Bridge (dollars per session): the forecast closes $1,600–33,500 of the
  loss, the front of the queue adds $700–5,200, removing fees adds
  $300–3,400. Best case per contract traded: about $0.04 on crude and
  $0.74 on copper.

**Conclusion.** The forecast is a real input to a quoting engine, and the
backtest shows what decides whether it pays: speed (steepest at the fast
end, below what we can measure), queue position (corn turns profitable
only at the front), fees (member or market-maker rates), and size and
breadth to make an edge of cents per contract worth running. These are
upper bounds; none shows the architecture would be profitable. Output:
`data/mm_latency.{log,csv}`, `data/mm_breakeven.csv`.

## 32. The v2 position-sizing gate scaled by whole-sample extremes; now causal

*8 November 2026 · Status: fixed and measured · Code: `expanding_minmax` in
`feature_alignment.py`, `run_backtest.py`, `test_feature_alignment.py`
(two new tests) · Logs: `data/validate_pipeline_gatefix.log`*

**Context.** Checking the report's pipeline diagram against Run 25
(`ARCH_VERSION=v2`) showed that v2 scales every position by a gate built
from raw VPIN and Hawkes burst ratio (on by default, `POSITION_GATE=1`):
size × (1 − ½·VPIN scaled) × (1 − ½·burst scaled), so as low as a quarter.
Each series was scaled to [0, 1] with its minimum and maximum over the
**whole sample**, so a bar's position size depended on how extreme order
flow would get later: the same kind of leak as bug 5, in sizing rather than
direction. `TODO.md` had called this gate "the one component that behaved
sensibly in testing".

**Decision.** Scale each series against the minimum and maximum seen so
far (`expanding_minmax`; rows before 20 bars are not scaled down). Tests:
changing the future does not change earlier values, and the last value
equals the full-sample scaling.

**Result.** The effect was small. Run 25 configuration, seed 0, before /
after: combined gate mean 0.694 / 0.665; average size scale on active bars
0.621 / 0.570; positions active 22.0% / 22.0%; trades 17 / 17; Sharpe 0.36
/ 0.34 (Sharpe is not informative here; shown only to say the change is
small). Falsification suite after the fix (4 modes × 3 seeds) passes the
leakage check (|OOS corr| under a shuffled target 0.0475, tolerance 0.05,
close to the limit); real features −0.48 ± 0.89 Sharpe against noise
−0.32 ± 0.12 and shuffled target −0.15 ± 0.79: still not separable, as
before.

**Conclusion.** A 16th leak, found by checking a figure against the code
rather than by a result looking too good. It did not drive Run 25's
results, but the gate's "sensible" behaviour should not be cited without
rerunning it causally. The report's diagram now shows v2 as built: the four
order-flow kernels merged by PCA into one OrderFlow kernel, five stage-1
kernels, and the order-flow sizing gate.

## 33. The short-horizon results hold in every quarter; the forecaster trains in seconds

*12 November 2026 · Status: measured · Code: `stability_check.py`,
`test_stability_check.py`, `timing_check.py` · Logs:
`data/stability_check.log`, `data/timing_check.log`*

**Context.** Two questions the final presentation will face: is "18 months"
one lucky stretch, and could the forecaster run live? Neither needs a new
model, only existing outputs.

**Stability.** Three results scored per calendar quarter, from data already
on disk: book imbalance's per-session IC with the next minute's move (every
session, no model); the walk-forward ridge forecast's per-session IC with
the next minute's volatility (`kernel_paired.py`'s saved forecasts); and
the market-making forecast gate's per-session profit over quoting all the
time and over standing aside at random (entry 30, pessimistic rule).

| per quarter (lowest to highest) | CL | NG | HG | ZC |
|---|---|---|---|---|
| book imbalance IC (6 quarters) | +0.055 to +0.072 | +0.066 to +0.083 | +0.050 to +0.099 | +0.198 to +0.274 |
| volatility forecast IC (5) | 0.81 to 0.84 | 0.68 to 0.74 | 0.76 to 0.81 | 0.37 to 0.48 |
| gate vs random, $/session (5) | +12,897 to +17,479 | +4,807 to +8,999 | +5,877 to +15,297 | +477 to +702 |
| weakest quarter's t | 4.6 | 7.8 | 8.5 | 4.8 |

Every result keeps its sign with t ≥ 4.6 in every quarter on every
contract. The forecast and backtest quarters start in 2023 Q4 (12–16
sessions in it) because the first 30% of sessions trains the model. This
rules out one lucky stretch inside the window; it says nothing about a
different regime.

**Speed** (CL, Apple M3, the model of entry 29; medians of three runs where
repeated): tuning 7.1 s; ridge refit on 240 sessions (270,652 minutes)
0.8 s; kernel refit 9.4 s; one minute's features about 7 µs (batch); one
forecast, ridge plus kernel, about 34 µs. A full nightly or hourly refit is
cheap; a live system's costs are the real-time feed and order-book
processing, and for market making, microsecond reaction, which is
infrastructure rather than model.

**Conclusion.** Both questions now have measured answers in the report
("Do the results hold over time?", "Could it run live?").
