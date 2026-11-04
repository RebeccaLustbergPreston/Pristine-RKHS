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

*3 November 2026 · Status: test built; decision pending pooled data · Code:
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
next five days with a rank IC around 0.5. Any forecast that tracks it scores
well, so the plain IC cannot say whether a kernel knows anything. "Beyond
trailing" can. Short, non-overlapping outcome windows keep the score from
looking more certain than it is.

**Context.** The project documents describe the kernels as volatility
forecasters, but every reported run used the returns target (entry 13).
This is the first test of the kernels on the job they were built for.
