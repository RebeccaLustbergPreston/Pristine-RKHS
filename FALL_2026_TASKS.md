# Fall 2026 — practical task list

Companion to `TODO.md` (which explains *why*) and `HANDOFF.md` (state of
play). This one is the *what*, in order, with a way to tell when each
piece is finished.

**Team shape:** five sector owners, aiming for about ten contracts in
total, with CL already built. Ten is the right number for effort as much
as data cost, and enough markets that the cross-asset thesis is testable.

**Rule for every task below:** it is not done until someone other than the
author can run it from a clean clone and get the same answer.

---

## A. Decide the data source — blocking, do in week 1

Everything downstream depends on this and nothing else should start until
it is answered.

**A1. Establish exactly what SMU's WRDS subscription covers for CME
futures.** Ask the library's WRDS contact directly. Specific questions:

1. Is there **trade-by-trade (tick) data** for CME futures, and for which
   product families? Not daily settlement — individual prints.
2. Is there **any order-book data**? Full depth is ideal, but even
   top-of-book bid/ask/size would carry most of what we need.
3. What is the **history window** and the **delivery format** (web query,
   SAS, cloud, direct DB)?
4. Are there **query size or export limits** that would bite on ten
   contracts of tick data?
5. Do we have **OptionMetrics (Ivy DB)**? It carries computed Greeks
   including gamma, which is the piece Gamma Exposure has been waiting on.

*Acceptance:* a one-page memo circulated to the group answering all five,
with the WRDS contact's name and date.

**A2. Decide, in writing, based on A1.** Three outcomes:

- **Tick + some book depth available.** Proceed. All kernels survive.
- **Tick trades but no book.** LOB kernel dies; VPIN, Kyle's Lambda and
  Hawkes all survive since they only need the trade tape. Acceptable —
  note it and move on.
- **Daily settlement only.** The entire fast layer is unusable and this
  stops being the same project. At that point the choice is to pay for
  tick data on a smaller subset, or rebuild around daily-frequency
  kernels. Escalate to Rebecca and Anders rather than deciding alone.

*Acceptance:* the decision and its reasoning are committed to the repo.

**A3. Confirm what CL currently is, so the new source is comparable.**
Our CL tape is Databento's `CL.c.0` — a *continuous front-month series
with rolls already spliced in by the vendor*. Whatever WRDS gives us will
almost certainly use a different roll convention. Establish which one, or
prices will not be comparable across vendors.

Databento's splice does not adjust prices, which puts 18 fake returns into
our CL target — see D0. Whatever the new source does at a roll, check it
for the same problem before building on it.

---

## B. Pick the ten contracts — week 1 to 2

**B1. Screen candidates on four things:** average daily volume, whether
the chosen data source actually covers them, contract size (can a paper
book hold one lot), and pairwise correlation against the rest of the set.

The thesis is that signal comes from how markets interact, so a set of ten
correlated contracts is worth much less than ten spread out. Aim for low
average pairwise correlation.

**B2. Sectors and owners:**

| Sector | Owner | Contracts |
|--------|-------|-----------|
| Energy | — | CL, already built and run end to end |
| Interest rates | Nolan | chosen in B1 |
| Equities | Demetrios | chosen in B1 — see note |
| Metals | Connor | chosen in B1 |
| Crypto | Arjan | chosen in B1 |
| Indexes | Junaid | chosen in B1 — see note |

*Note:* in futures, equities and indexes overlap — both mean stock-index
contracts such as ES and NQ. Demetrios and Junaid are to agree how to
split it; nothing here assumes an answer.

*Acceptance:* a committed table of the final contracts with sector owner,
data availability confirmed per contract, and the measured correlation
matrix.

**B3. Settle the equities / indexes split** and record it in the B2 table.

---

## C. Pull and cache the data — weeks 2 to 5

**C1. Write one puller that takes a symbol** and produces the same file
layout we already use, so `data_loader.py` needs no special cases:

```
data/<SYMBOL>/trades.parquet          tick tape
data/<SYMBOL>/ohlcv1m.parquet         1-minute bars
data/<SYMBOL>/definitions.parquet     expiration, tick size, multiplier
data/<SYMBOL>/LOB/                    book features, if A2 allows
```

*Acceptance:* `pull.py --symbol NG` runs start to finish and the output
passes the same validation as CL.

**C2. Storage decision, made before the bulk pull, not after.** CL alone
is 2.8 GB for 18 months. Ten contracts of raw tick data is 8 to 30 GB
depending on liquidity, and ES will be the heaviest by some margin.
Computed features are ~55 MB per contract, so the whole set of ten fits in
under a gigabyte.

The plan should be: **pull raw → compute features → keep the features →
archive the raw somewhere cheap.** Do not delete raw, since recomputing a
feature differently later would mean re-pulling.

*Acceptance:* a written answer to "where does raw live, where do features
live, and how does a new team member get both".

**C3. Per-contract cost and tick size.** The backtest currently hardcodes
one cost number (0.00015, roughly a tick on CL at $70). That is wrong for
nine of the ten. The definitions file already carries
`min_price_increment` and `min_price_increment_amount` per contract — wire
them in.

*Acceptance:* cost per trade is derived per symbol, not a constant.

---

## D. Make the pipeline run per symbol — weeks 2 to 5

**D0. Remove the fake contract-switch returns from CL — do first.** Our
CL tape joins 19 contracts without adjusting prices, so the 18 switches
put 18 fake returns into the forward-return target (median 0.95%, largest
2.14%, 14 of 18 downward, against a typical bar move of about 0.5%). Every
trade carries `instrument_id`, so drop any target return that spans a
change of contract, and check whether VPIN, Kyle's Lambda and Hawkes see
the jumps too. Full reasoning in `TODO.md` item 1.

*Acceptance:* a test that no target return spans a contract switch, and
`validate_pipeline.py` still passes. This becomes the baseline D1 is
checked against.

**D1. Extract `build_features_for_symbol(symbol)` out of `main()`.**
Each kernel must be **optional per symbol**: the pipeline currently builds LOB
features at stage 2 and registers LOB in the fast-kernel set unconditionally,
so a symbol without order-book depth fails rather than running the kernels it
does have. Depth exists for CL, NG, HG and ZC only. Never pad a missing
kernel's features with zeros or with another contract's values — a silent
placeholder is what disabled the VRP kernel in bug 4.

`run_backtest.py` is one long function with CL assumptions threaded
through it. The master-schedule design already supports this: each symbol
gets its own dollar-bar schedule, and cross-asset work happens later on
the daily axis where calendars line up.

*Acceptance:* CL run through the refactored path produces results
identical to the post-D0 baseline, and `validate_pipeline.py` still passes.

**D2. Per-symbol config.** Dollar-bar threshold cannot stay at $1M for
every contract — that is tuned to CL's notional. Set it per symbol so bar
counts land in a comparable range.

*Acceptance:* every symbol produces roughly the same number of bars per
day, documented.

---

## E. Build a score we can trust — weeks 2 to 4, blocks section G

**E1. Score the prediction, not the profit.** Compute the correlation
between the model's prediction and what the price actually did next, fold
by fold. Profit has to pass through the Kalman filter, the entry and exit
thresholds and the position sizing, and lands on fewer than 50 days where
we held anything — few enough that a handful of lucky trades sets the
number.

**E2. Average over several random starting points.** Today one seed gives
one number, and the spread across seeds is larger than any effect we would
be looking for.

**E0. Check whether the kernels are doing the work at all — cheap, do first.**
The position rule is: enter when the kernel volatility forecast says volatility
is compressed, take direction from 20-day momentum, exit when volatility
expands, size by inverse predicted volatility. The kernels therefore gate and
size; they never choose direction. So replace the whole kernel volatility
forecast with a trivial one — trailing realised volatility — and change nothing
else. If performance is unchanged, ten kernels are an elaborate way to compute
something one line already provides.

*Acceptance:* a side-by-side of the two configurations across seeds, reported as
the spread rather than a single number.

Note that pinning the prediction to zero does **not** work as a control: the
volatility score would never cross the entry threshold, so no trades would be
taken at all.

**E3. Validate the metric itself.** It must score the real pipeline
differently from `NULL_MODE=shuffle_target`. If it cannot tell those
apart, it is not a metric and E1 needs another pass.

*Acceptance:* `validate_pipeline.py` gains a check that the new metric
separates real from shuffled, and it passes.

---

## F. Change the question being asked — weeks 5 to 8

**F1. Cross-sectional target.** Stop predicting "will CL go up" and start
predicting "which of the eight looks best right now". That is what an RKHS
is actually built for, and it is what the interaction thesis needs.

**Run it on the features every contract shares** — the trade-based kernels
(VPIN, Kyle, Hawkes) and the daily ones (VRP, MacroMotion, the gates). Order-book
depth exists for only four of the eight, and a ranking where half the contracts
carry an extra feature partly reflects which data we bought rather than what the
markets did. The LOB question gets its own controlled test in G8.

**F2. Split direction from sizing.** Direction from a plain trend signal
per market — this is a commodity and nobody has an edge in it. Size from
the kernels, ranking markets by predicted volatility regime and how clean
the order flow looks.

**F3. Wire Gamma Exposure** once the Greek arrives from Anders, or from
OptionMetrics if A1 turns it up.

---

## G. Ablation — weeks 8 to 10, after E

Run each configuration under the metric from section E, across seeds, and
report the spread rather than a single number:

1. Trend signal alone, no RKHS — this is the floor everything must beat
2. Single kernel alone, one row per kernel
3. Fast layer only
4. Fast + slow, additive
5. Full model with the cross-level term
6. Full model plus the event gate
7. Full model with position-sizing gates removed
8. **Does order-book depth add anything?** Same model, same dates, run with and
   without the LOB features on the four contracts that have depth (CL, NG, HG,
   ZC). This is the question the LOB kernel has never been asked, and the reason
   the depth data was bought. A null result retires the kernel honestly; a
   positive one is the evidence needed to ask for depth on the rest.

*Acceptance:* a table showing which kernels earn their place. If nothing
beats row 1, that is a real result and should be written up as one.

---

## H. Roll handling now; live trading deferred

**Deferred to Spring 2027 (decided 2026-09-27): H1, H4, H5, H6, H7, H8, H9.**
The semester was over-scoped, and the reason for cutting this block rather
than research work is stated in the section itself: a paper trade cannot
validate an edge until section E lands, so building the execution stack now
would prove the plumbing while telling us nothing about the strategy. The
task descriptions stay below as the brief for whoever picks it up.

**Still in scope: H2 and H3.** Neither is execution work — they are backtest
correctness. H2 (which contract were we holding on a given date) is
prerequisite to fixing the contract-switch returns in D0, and H3 (roll
costs) removes an overstatement from every number we report.

There is **no execution code in this repo at all** today. This is a
from-scratch build, which is why it was scheduled from week 6 rather than
week 11. `analytics/trade_simulator.py` is not a starting point — it
simulates *options* exits (premiums, take-profit, expiry) and came from a
different project.

**H1. Broker and account.** *(deferred to Spring 2027)* Interactive Brokers paper account is the
realistic choice for futures; `ib_async` is the standard Python client.
*Acceptance:* pull a live quote and place, then cancel, one paper order.

**H2. Contract selection and roll calendar.** *(in scope — needed for D0)* Live you trade CLZ6, not
`CL.c.0`. Write the rule — front month, roll N days before expiry or on
volume crossover — and a function that answers "which contract do we hold
today" for every symbol. The `expiration` field is already in the
definitions file.
*Acceptance:* given any date in the backtest window, the function names
the contract that was actually front month.

**H3. Roll cost in the backtest.** *(in scope)* Our continuous series splices rolls for
free. A real book pays the calendar spread every roll, roughly monthly on
CL. That cost is currently nowhere in our numbers. (The splice also puts
fake returns into the target; that is D0, a separate fix.)
*Acceptance:* backtest P&L includes an explicit roll cost line.

**H4. Signal service — no order placement.** *(deferred)* A scheduled job that loads
the latest data, computes features, and writes target positions per symbol
to a file with a timestamp. Runs on a schedule for weeks in dry-run before
anything touches a broker.
*Acceptance:* runs unattended for a full week and the output is
inspectable each morning.

**H5. Execution service — separate process.** *(deferred)* Reads target positions,
compares against what the broker actually holds, and emits only the
difference. Keeping this apart from H4 is what lets H4 run safely forever.
*Acceptance:* given a target file and a mocked broker state, it emits the
correct order list and nothing else.

**H6. Safety rails, before the first live order.** *(deferred)* All of these:

- Client order IDs so a retry cannot double-fill
- Max position per symbol, max total gross, max daily loss
- Refuse to trade on data older than N minutes
- A kill-switch file that halts new orders when present
- Explicit market-closed and holiday handling
- Bounded order size — reject anything above a sanity threshold

*Acceptance:* each rail has a test that proves it fires.

**H7. State and reconciliation.** *(deferred)* On restart, read actual positions from
the broker rather than assuming. Log intended versus filled for every
order, and produce a daily reconciliation.
*Acceptance:* kill the process mid-session, restart it, and it recovers
the correct position without manual help.

**H8. Failure drills.** *(deferred)* Deliberately break it and confirm each is handled:
feed goes down mid-session, API disconnects between submit and confirm,
stale data file, duplicate signal, partial fill, roll day.
*Acceptance:* each drill is a test in the suite, not a one-off.

**H9. Entry and exit rules, written in plain English first.** *(deferred)* We have
thresholds in code but no document saying when a decision is made, what
order type is used, or what happens on a partial fill. Write that page,
then make the code match it.

---

## I. Writeup and handoff — weeks 14 to 15

**I1.** Results deck: the metric from E, drawdown, turnover, the ablation
table from G, and honest limitations.

**I2.** Update `POSTMORTEM.md`, `HANDOFF.md` and `TODO.md` rather than
starting new documents.

**I3.** Handoff doc so Spring 2027 starts from a known state — including
what did not work, which is the part that saves the next team the most
time.

---

## Sequencing at a glance

```
Week  1     A (data source)  ·  B (pick assets)
Week  2-4   C (pull)  ·  D (per-symbol)  ·  E (metric)
Week  5-8   F (cross-sectional)          ·  H2-H3 (rolls, roll costs)
Week  8-10  G (ablation, needs E)
Week 11-13  E and G follow-ups, more seeds
Week 14-15  I (writeup)
```

A blocks everything. E blocks G. The live-trading build (H1, H4-H9) is
deferred to Spring 2027 — it was the longest pole, and it cannot validate
an edge before E lands. H2 and H3 stay because they are backtest
correctness, not execution.
