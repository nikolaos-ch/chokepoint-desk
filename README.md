# Chokepoint Desk

Independent project tracking ship transits through the Strait of Hormuz and Bab el-Mandeb. Not affiliated with Aureum. Not financial advice.

## Data

Regenerate all transit data with one command (Python 3.9+, standard library only):

```
python scripts/fetch_portwatch.py            # fetch live from IMF PortWatch
python scripts/fetch_portwatch.py --offline  # rebuild the JSON from data/raw if PortWatch is down
```

| File | What it is |
|---|---|
| `data/transits/hormuz.json` | Daily ship transits through the Strait of Hormuz since 2019-01-01: `total`, `by_type` (container, dry_bulk, general_cargo, roro, tanker) and `ma7`, the trailing 7-day average. <br>Header fields `source`, `fetched_at` (UTC) and `last_data_date` say where the numbers come from and how fresh they are. |
| `data/transits/bab_el_mandeb.json` | The same daily series for the Bab el-Mandeb Strait, with the same fields. <br>PortWatch publishes with a lag of a few days, so `last_data_date` is usually 2 to 4 days before today. |
| `data/raw/portwatch_chokepoints.csv` | An untouched copy of every PortWatch field (counts and capacity) for both straits, one row per strait per day. <br>It is the offline backup: `--offline` rebuilds both JSON files from it alone. |

Source: IMF PortWatch "Daily Chokepoints Data" (ArcGIS table `Daily_Chokepoints_Data`; Hormuz = `chokepoint6`, Bab el-Mandeb = `chokepoint4`). Each JSON also lists `missing_dates` (days absent from the source; never filled in). `ma7` is `null` when any of the 7 days is missing.
