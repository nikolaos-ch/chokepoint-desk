# Chokepoint Desk

Independent project tracking ship transits through the Strait of Hormuz and Bab el-Mandeb. Not affiliated with Aureum. Not financial advice.

## Data

Regenerate all transit data with one command (Python 3.9+, standard library only):

```
python scripts/fetch_portwatch.py            # fetch live from IMF PortWatch
python scripts/fetch_portwatch.py --offline  # rebuild the JSON from data/raw if PortWatch is down
```

On Windows, where `python` is often not on PATH, use the launcher instead: `py scripts/fetch_portwatch.py` (add `--offline` the same way).

| File | What it is |
|---|---|
| `data/transits/hormuz.json` | Daily ship transits through the Strait of Hormuz since 2019-01-01: `total`, `by_type` (container, dry_bulk, general_cargo, roro, tanker) and `ma7`, the trailing 7-day average. <br>Header fields `source`, `fetched_at` (UTC) and `last_data_date` say where the numbers come from and how fresh they are. |
| `data/transits/bab_el_mandeb.json` | The same daily series for the Bab el-Mandeb Strait, with the same fields. <br>PortWatch publishes with a lag of a few days, so `last_data_date` is usually 2 to 4 days before today. |
| `data/raw/portwatch_chokepoints.csv` | An untouched copy of every PortWatch field (counts and capacity) for both straits, one row per strait per day. <br>It is the offline backup: `--offline` rebuilds both JSON files from it alone. |
| `data/raw/portwatch_meta.json` | The UTC time the raw CSV was actually fetched (`fetched_at`) and its row count. <br>`--offline` reuses this time and sets `"offline": true` in both transit files, so stale data is never shown as fresh. |

Source: IMF PortWatch "Daily Chokepoints Data" (ArcGIS table `Daily_Chokepoints_Data`; Hormuz = `chokepoint6`, Bab el-Mandeb = `chokepoint4`). Each JSON also lists `missing_dates` (days absent from the source; never filled in). `ma7` is `null` when any of the 7 days is missing.

## Sanctions lists (private)

`scripts/sanctions.py` downloads the official free sanctions lists, extracts every vessel entry with its IMO number, and matches them against the AIS recorder's `ship_static.imo`. Everything it writes stays in `var/sanctions/` (gitignored): nothing goes to `data/`, the site or the repository, and no ship names or matched IMOs are ever published.

```
py scripts/sanctions.py              # download all lists (~220 MB), rebuild the table, match
py scripts/sanctions.py --offline    # re-parse the cached downloads in var/sanctions/raw/
py scripts/sanctions.py --no-match   # skip the AIS match
py -m unittest tests.test_sanctions -v
```

- **Output.** `var/sanctions/vessels.sqlite` has three tables:
  - `lists`: one row per list per snapshot date, with the source URL, download time, the list's own publish date, size and SHA-256.
  - `vessels`: IMO, IMO as written, names, flag, owner or linked entity, programme or regime, and listing date.
  - `matches`: the private AIS hits.
  
  Earlier snapshot dates are kept, so later runs can show what was newly listed.
- **IMO handling.** It accepts `IMO 1234567`, `IMO1234567`, `IMO: …`, `IMO No. …` and a bare number. It checks the IMO check digit, where the first six digits are weighted 7 to 2 and the last digit of the sum must equal the seventh digit. A number with a wrong or missing digit is stored as written, never matched and never "repaired".
- **AIS.** The recorder's database is opened read-only (`mode=ro`, `query_only`). SQLite readers in WAL mode never block the recorder's writes.
  - IMO is the primary key.
  - OFAC also lists MMSIs, so the script reports MMSI-only hits as leads. MMSIs are reassigned when a ship changes flag, so an MMSI-only hit needs checking before it is used.
- **Downloads.** These servers sometimes close long downloads early without any error. The script resumes with HTTP Range, checks Content-Length and the closing XML tag, and never replaces a cached file with a cut-off one.

| List (`list_id`) | Source | What it gives for vessels | Terms of use |
|---|---|---|---|
| OFAC SDN List (`ofac_sdn`) | [SDN.XML](https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML). Listing dates come from [SDN_ADVANCED.XML](https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML). | ~1,570 vessels: IMO, MMSI, flag, owner or "Linked To" entity, programmes, and listing date. | US federal government work, which has no US copyright under [17 U.S.C. § 105(a)](https://www.copyright.gov/title17/92chap1.html#105) ("Copyright protection under this title is not available for any work of the United States Government"), checked 18 Nov 2026. **To confirm:** neither OFAC's [Sanctions List Service page](https://ofac.treasury.gov/sanctions-list-service) nor Treasury's site-policies page stated its own reuse notice when fetched from here on 18 Nov. The legally binding version is the one in the Federal Register. |
| OFAC Consolidated (non-SDN) List (`ofac_cons`) | [CONSOLIDATED.XML](https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONSOLIDATED.XML) | None at present: it holds only individuals and entities. It is parsed anyway, so vessels added later get picked up. | Same as the OFAC SDN List. |
| UK Sanctions List, FCDO (`uk`) | [UK-Sanctions-List.xml](https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml) | ~690 ships: IMO, current believed flag, owner or operator, regime, designation date and last-updated date. Most designations only bar port entry and do not freeze assets. | Crown copyright under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/), which allows reuse with attribution (shown on the GOV.UK publication page, checked 18 Nov 2026). |
| EU Consolidated Financial Sanctions List, FSF (`eu_fsf`) | [xmlFullSanctionsList_1_1](https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw) (public token) | **No vessel subjects.** The IMO numbers in it belong to shipping companies (IMO owner numbers), so they are stored with `kind='company'` and not matched. The EU's ship bans are in the next row. | European Commission reuse policy (Decision 2011/833/EU), which allows reuse with acknowledgement of the source. The Official Journal is the legally binding version. **To check:** the dataset page could not be loaded from this machine on 18 Nov, so these terms are not confirmed on the page itself. |
| EU vessel list: Annex XLII to Council Regulation (EU) No 833/2014 (`eu_vessels`) | The newest consolidated text on EUR-Lex, `TXT/HTML/?uri=CELEX:02014R0833-YYYYMMDD`. Its date comes from the regulation's [EUR-Lex page](https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:32014R0833). | ~670 ships banned under Article 3s from EU ports and services (the Russian "shadow fleet"). For each: IMO, name and former names, the Article 3s(2) ground (point a to h), and the date of application. Flag and owner are not given. Deleted entries are left out. | EUR-Lex [legal notice](https://eur-lex.europa.eu/content/legal-notice/legal-notice.html), checked 18 Nov 2026: reuse is allowed under Decision 2011/833/EU, and consolidated texts are licensed under CC BY 4.0, so cite the source and state any changes. Only the Official Journal is authentic. |

**EU vessel list lag.** EUR-Lex publishes a consolidated text some time after each amending act. The consolidated version read is the `publish_date` in `lists`. `var/sanctions/raw/meta.json` also keeps the newest amending act the EUR-Lex page lists. If that act is newer than the consolidated text, the run warns that recent EU listings may be missing. On 18 Nov 2026 both dates were 24 Jul 2026. A Council act published after that date but not yet listed on the EUR-Lex page would not show up either way. EUR-Lex pages are downloaded gzip-compressed: plain downloads broke off partway through without any error.

The table is a screening aid, not a legal determination. A matching IMO says that a ship with that number appears on a list on the snapshot date, nothing more.
