# data/ — what the Chokepoint Desk page reads

The page (`/index.html` + `/app.js`) reads **only** these JSON files. Dates are ISO 8601, UTC.
`<strait>` is `hormuz` or `bab_el_mandeb`.

| File | Owner | Panel |
|---|---|---|
| `transits/<strait>.json` | data pipeline (#2) | A. Transit counter |
| `forecast/<strait>.json` | forecast model (#3) | Next 7 days |
| `rules.json` | analyst, by hand | B. What settles it |
| `ledger.json` | analyst, by hand | C. Claimed vs confirmed |

If a transit file's `source` or a forecast's `method_note` contains the word `MOCK`, the
page shows a "mock data" banner. (Real pipeline files from #2 / #3 replaced the mocks on
17 Nov 2026.)

Optional fields the page also uses when present: `forecast.baseline`
(`{mean_per_day, label}`) becomes the reference line for straits without fixed lines
(Bab el-Mandeb); `backtest.naive_kind` (`naive_ma7` | `naive_last`) names the simple guess
in the backtest text.

Backtest verdict (QA #10, D6). The verdict is computed by the backend (#3) only; the page
applies no rule of its own. It reads:
- `backtest.outcome`: headline. `model_clearly_better` = "Model clearly better",
  `no_meaningful_difference` = "No meaningful difference", `naive_better` /
  `naive_clearly_better` = "Simple guess did better". Any other code is shown as words.
- `backtest.method_used` (else top-level `method`): `model` | `naive_baseline`, which says whose
  range the chart shows.
- `mae_model`, `mae_naive`, `coverage_80`, `window`: the test-window numbers on the card.
- `backtest.verdict`: shown as is, as a small "Detail." line under the numbers.
- `naive_kind`: names the simple guess in the "Range from:" line under the issue date.
The backend must keep these three consistent (no "No meaningful difference" with
`method_used: model`); the page shows them exactly as given.

Stale data (D7): if `last_data_date` is more than 7 days before the viewer's clock, the page
shows a red "Stale data" notice under the data stamp.

---

## transits/&lt;strait&gt;.json

```json
{
  "strait": "hormuz",
  "source": "IMF PortWatch, Daily Chokepoints Data",
  "fetched_at": "2026-11-17T06:00:00Z",
  "last_data_date": "2026-11-15",
  "daily": [
    { "date": "2026-11-15", "total": 63,
      "by_type": { "container": 12, "dry_bulk": 15, "general_cargo": 8, "roro": 2, "tanker": 26 },
      "ma7": 63.1 }
  ]
}
```

- `offline` (bool, from #9): `true` when the file was rebuilt from data/raw without querying
  PortWatch; `fetched_at` is then the original fetch time. The page shows a
  "PortWatch was unavailable" notice with that date.
- Daily array under `daily` (what #2 writes); `rows` or `data` are accepted too. Any order; the page sorts by date.
- `ma7` = mean of `total` over the 7 days ending on `date`; `null` while fewer than 7 days exist.
  If `ma7` is missing the page computes it.

## forecast/&lt;strait&gt;.json

```json
{
  "issued_at": "2026-11-17T07:00:00Z",
  "regime": "open | contested | closed",
  "regime_reasons": ["7-day average is 26% below the 90-day average"],
  "horizon_days": 7,
  "daily": [{ "date": "2026-11-16", "p10": 52.1, "p50": 63.4, "p90": 74.7 }],
  "weekly_total": { "p10": 377, "p50": 444, "p90": 511 },
  "by_type": { "tanker": { "p10": 150, "p50": 185, "p90": 222 } },
  "backtest": { "mae_model": 6.8, "mae_naive": 9.4, "coverage_80": 0.78,
                "window": { "start": "2026-05-01", "end": "2026-11-08" } },
  "method_note": "One or two plain sentences on how the forecast is made."
}
```

- `by_type` values are **7-day totals** per ship type (same quantiles as `weekly_total`).
- `mae_*` are mean absolute daily errors, in transits/day. `coverage_80` is the share
  (0–1) of backtest days where the actual count fell inside p10–p90.
- `backtest.window` may also be a plain string, e.g. `"2026-05-01 to 2026-11-08"`.
- **Per-type figures do not add up to the total.** Each ship type is forecast on its
  own (and may use a different method), so the `by_type` p50s need not sum to
  `weekly_total.p50`, and p10/p90 never add up (ranges do not sum). The file repeats
  this in `by_type_note`.

**One verdict, one source.** `scripts/forecast.py` is the only place the model-vs-naive
choice is made (rule: the model is shown only if the whole 95% CI of its MAE gain over
naive is above 0; no extra minimum-gain threshold). The page applies no rule of its own:
it renders `method`, `backtest.outcome` and `backtest.verdict`. `daily`, `weekly_total`
and the ranges always come from the method named in `method`.

Extra fields written by `scripts/forecast.py` (#3), all optional for the page:

| Field | Meaning |
|---|---|
| `method` | `model` or `naive_baseline`: what the forecast numbers come from |
| `backtest.outcome` | `model_clearly_better` \| `no_meaningful_difference` \| `naive_clearly_better` |
| `backtest.verdict` | One plain-English sentence saying which method is shown and why |
| `backtest.gain`, `gain_ci95` | Naive MAE minus model MAE (transits/day) and its 95% CI; the model is shown only if the whole CI is above 0 |
| `backtest.naive_kind` | `naive_ma7` (last 7-day mean) or `naive_last` (yesterday) |
| `backtest.out_of_sample_check` | The same choice rule run one quarter earlier and scored on `backtest.window`, which it never saw |
| `by_type.<type>.method`, `.daily`, `.backtest` | Same as above, per ship type |
| `regime_history` | `[{date, regime}]`, last 30 days, each computed from data up to that day |
| `baseline` | `{mean_per_day, period, label}`: the pre-crisis reference the regime uses |
| `last_data_date` | Last settled PortWatch day; forecast dates start the day after |

Since #27 (18 Nov 2026) "the model" is the strait-level **count model** in
`scripts/strait_model.py`: negative binomial, or Poisson when a dispersion test finds no
overdispersion, on recent level, trend, weekday, regime and independently reported
incidents; per-type forecasts are the total times a smoothed type share. The choice
rule and the existing fields are unchanged, so the page needs no change. When the model
is shown, `daily[].p10/p50/p90` can differ from day to day (weekday effect). New optional
fields:

| Field | Meaning |
|---|---|
| `target_note` | Plain sentence: the forecast is of PortWatch's AIS-based count (the settlement series), not true traffic |
| `backtest.model_kind` | `count_glm` |
| `backtest.half_life_days` | Now the count model's training-weight half-life (days) |
| `backtest.crps_model`, `crps_naive` | Mean CRPS on `backtest.window` (proper score for the whole predictive distribution; lower is better, ships/day) |
| `backtest.crps_gain`, `crps_gain_ci95` | Naive CRPS minus model CRPS and its block-bootstrap 95% CI. For the **total** the decision stays on MAE (fixed beforehand). **Per type** (decision 18 Nov) the model is shown only if both the MAE and the CRPS gain CIs are entirely above 0 **and** the out-of-sample check confirms it; `by_type.<type>.backtest.rule` says which rule applied |
| `backtest.log_score_model` | Mean negative log predictive probability of the model (lower is better) |
| `backtest.coverage_80_model`, `coverage_80_naive` | Share of outcomes inside p10-p90, for each method |
| `backtest.count_model` | `{family, alpha, dispersion_z, train_rows, train_weight_half_life_days, coefficients}` as fitted at the issue date (`share` added per type) |
| `backtest.models_compared` | MAE / coverage (/ CRPS) of `count_model`, `naive_ma7`, `naive_last` and the earlier `ewma` model, all on `backtest.window` |
| `by_type.<type>.backtest.outcome` | Also `not_confirmed_out_of_sample` (decision 18 Nov): the model won on the latest window but the out-of-sample check (tune on TUNE, choose on SELECT, score on REPORT) did not confirm it, so the baseline is shown |

## rules.json  (hand-curated)

One row per market family. **Replace the `example: true` row**; rows with `example: true`
are drawn with an EXAMPLE tag.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Unique id |
| `example` | bool | `true` only for the template row |
| `strait` | `hormuz` \| `bab_el_mandeb` | Which strait the rule is about |
| `venue` | string | Where the rule is published |
| `market_family` | string | Short name of the market family |
| `settlement_source` | string | Data series the rule settles on |
| `field` | string | Exact field / column used |
| `aggregation` | string | How daily values are combined (single day, weekly average, sum…) |
| `comparison` | `">="` \| `">"` \| `"<="` \| `"<"` | Comparison as worded in the rule |
| `threshold` | number \| null | Ship count in the rule |
| `strikes` | number[] | Optional, for ladder markets: one question per number. Use with `threshold: null`; shown as e.g. "more than (>) each of 15, 20, 25" |
| `window` | string | Counting window, in words |
| `window_start`, `window_end` | date | Optional, for the current window |
| `earliest_settlement` | date | Earliest date it can settle |
| `url` | string | Link to the rule text |
| `read_on` | date | **Required.** Day the rule text was read |
| `notes` | string | Optional caveats or verbatim rule text; over 240 characters it is folded behind "Show full text" |

## ledger.json  (hand-curated)

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Unique id |
| `example` | bool | `true` only for the template row |
| `strait` | `hormuz` \| `bab_el_mandeb` | |
| `date` | date | Date of the incident (UTC) |
| `place` | string | Where, in words |
| `summary` | string | One line: what is said to have happened |
| `claimed_by` | string | Who made the claim (name them) |
| `independent_source` | string \| null | UKMTO, JMIC, CENTCOM, a major outlet… `null` if none |
| `status` | `claimed_only` \| `third_party_report` \| `confirmed` | |
| `links` | `[{label, url}]` | At least one |
| `verified_on` | date | Day the analyst checked the row against its sources |

Top level: `updated_at` (date), `window_days` (default 14; the page shows rows within
this many days of `updated_at`, newest first).
