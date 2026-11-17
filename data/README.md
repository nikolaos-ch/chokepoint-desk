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
(Bab el-Mandeb); `backtest.model_beats_naive` and `backtest.naive_kind`
(`naive_ma7` | `naive_last`) drive the plain-words backtest text.

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
| `threshold` | number | Ship count in the rule |
| `window` | string | Counting window, in words |
| `window_start`, `window_end` | date | Optional, for the current window |
| `earliest_settlement` | date | Earliest date it can settle |
| `url` | string | Link to the rule text |
| `read_on` | date | **Required.** Day the rule text was read |
| `notes` | string | Optional caveats |

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
