# Data cleaning standard

Everyone cleans their own contracts. This is the format we all clean *into*,
so the eight contracts are interchangeable downstream.

**The test of a clean contract:** someone else can load it with
`DataLoader(symbol="NG")`, run the same code they run on crude, and get a
result — without reading your cleaning script first.

---

## 1. Where files go

```
data/<SYMBOL>/
├── trades.parquet          tick tape
├── ohlcv1m.parquet         1-minute bars
├── definitions.parquet     tick size, multiplier, expirations
├── statistics.parquet      daily settlement and open interest
├── LOB/                    book features, where depth exists
│   └── lob_features_<SYMBOL>/<YYYYMMDD>_lob_features.npz
└── cleaning_report.json    what you did — see section 4
```

`<SYMBOL>` is the contract root in capitals: `CL`, `NG`, `HG`, `GC`, `ZC`,
`ZT`, `ES`, `BTC`. Not the roll-convention string, and not the micro symbol.

## 2. Required columns

Column names are exact. Extra columns are fine; missing ones fail.

**`trades.parquet`**

| Column | Type | Notes |
|---|---|---|
| `ts_event` | `datetime64[ns, UTC]` | Exchange matching-engine time. Never local time. |
| `ts_recv` | `datetime64[ns, UTC]` | When we received it. Keep it — short-horizon work needs it. |
| `price` | `float64` | Contract price units (dollars per barrel, cents per bushel). Not scaled. |
| `size` | `float64` | Contracts traded. |
| `side` | `string` | `A` = sell aggressor, `B` = buy aggressor, `N` = unknown. Never guess. |
| `instrument_id` | `int32` | **Never drop this.** It is how contract switches are found. |
| `symbol` | `string` | The series you pulled, e.g. `NG.v.0`. |
| `session_date` | `datetime64[ns]` | The exchange trade date, which is *not* the calendar date — see section 3f. |

**`ohlcv1m.parquet`**: `ts_event`, `open`, `high`, `low`, `close`, `volume`,
`instrument_id`, `symbol`, `session_date`.

**`definitions.parquet`**: `symbol`, `raw_symbol`, `instrument_id`,
`expiration` (`datetime64[ns, UTC]`), `min_price_increment`,
`min_price_increment_amount`, `unit_of_measure_qty`.

**`statistics.parquet`**: `ts_event`, `session_date`, `stat_type`, `price`,
`quantity`, `instrument_id`, `symbol`.

**LOB features** — one `.npz` per session date, arrays exactly:
`volume_profile` (n_bars, 20), `book_shape` (n_bars, 20),
`depth_imbalance` (n_bars, 10), `timestamps` (n_bars, UTC ISO strings),
`bar_indices` (n_bars, int).

## 3. The rules, in this order

Apply them in order and count what each one removes.

**a. Keep trades only.** The raw tape contains more than executions. Keep
actual trades; drop book updates and administrative messages.

**b. Drop impossible rows.** Price ≤ 0, size ≤ 0, or a non-finite value in any
required column.

**c. De-duplicate** on `(ts_event, sequence)` where a sequence number exists,
otherwise on the full row.

**d. Sort by `ts_event`, then assert the result is monotonically
non-decreasing.** If it is not, stop and find out why rather than sorting
harder.

**e. Keep `instrument_id` and record every switch.** A change in
`instrument_id` is a contract switch. Record the date and the price gap. Do not
adjust, splice, or smooth it — that is a downstream decision, and silently
hiding it is how 18 invented price moves got into last semester's results.

**f. Tag `session_date`, do not assume the calendar date.** CME's trading day
starts the evening before: a trade at 23:58 UTC on 30 November belongs to the
1 December session. Use the exchange session, not the clock date. Crypto trades
nearly 24/7 and weekend sessions carry the following business day's date — if
your contract is BTC, say in your report how you handled it.

**g. Never fill a gap with a placeholder.** No zeros, no forward-filled prices,
no values borrowed from another contract. A missing day is missing. Filling
VIX with a placeholder is what silently disabled a whole kernel last semester.

**h. Flag outliers, do not delete them.** If a price looks wrong, add a boolean
column saying so and explain it in the report. Deleting real extreme moves is
how a strategy comes to look safer than it is.

**i. Everything stays UTC at nanosecond precision.** No local time, no
rounding to seconds, no timezone conversion anywhere in the file.

## 4. The report — `cleaning_report.json`

Not optional. A cleaned file without one cannot be reviewed.

```json
{
  "symbol": "NG",
  "series": "NG.v.0",
  "roll_convention": "volume",
  "source_files": ["glbx-mdp3-20230702-20230731.trades.dbn.zst", "..."],
  "pulled_on": "2026-09-30",
  "cleaned_by": "your name",
  "cleaned_on": "2026-10-22",
  "date_range": ["2023-07-02", "2024-12-31"],
  "rows_in": 14203991,
  "rows_out": 14198332,
  "removed": {
    "non_trade_messages": 4112,
    "impossible_rows": 36,
    "duplicates": 1511
  },
  "sessions": 468,
  "sessions_with_zero_rows": ["2023-11-24"],
  "contract_switches": [
    {"session_date": "2023-07-20", "from": 42189, "to": 42190, "price_gap_pct": -0.74}
  ],
  "side_unknown_pct": 0.0,
  "outliers_flagged": 7,
  "notes": "Anything you decided that is not covered by the rules above."
}
```

## 5. Before you call it done

- [ ] Every required column present, with the right type and no NaNs
- [ ] `ts_event` sorted and monotonically non-decreasing
- [ ] No duplicate `(ts_event, sequence)` rows
- [ ] `instrument_id` present, and every switch listed in the report
- [ ] `session_date` is the exchange session, verified against one evening trade
- [ ] Price range sanity-checked against a public chart for two random dates
- [ ] Row count per session plotted or printed — zero-row sessions explained
- [ ] `cleaning_report.json` written and committed
- [ ] A second person has read the report and can say what you did

## 6. Things that are not cleaning

Do not do these while cleaning. They are modelling decisions and belong
downstream, where they can be tested and reversed:

- Adjusting prices across contract switches
- Resampling to bars of any kind
- Normalising, standardising or scaling
- Smoothing, winsorising, or removing extreme returns
- Dropping sessions because they look quiet

If you think one of these is needed, write it in `notes` and raise it — do not
bake it into the file.
