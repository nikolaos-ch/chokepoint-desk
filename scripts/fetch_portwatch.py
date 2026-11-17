#!/usr/bin/env python3
"""Fetch IMF PortWatch daily chokepoint transits for Hormuz and Bab el-Mandeb.

Source: IMF PortWatch "Daily Chokepoints Data" (ArcGIS feature table,
item 3da2b9ca97684916b75c4013f95d18ab), the same table behind
https://portwatch.imf.org/pages/chokepoints.

Writes, per the shared data contract (ticket #1):
  data/transits/hormuz.json
  data/transits/bab_el_mandeb.json
  data/raw/portwatch_chokepoints.csv   (raw copy of every source field)
  data/raw/portwatch_meta.json         (real fetch time of that raw copy)

Usage:
  python scripts/fetch_portwatch.py            # fetch live, rewrite everything
  python scripts/fetch_portwatch.py --offline  # rebuild JSON from the raw CSV only

Standard library only, so it runs unchanged in CI.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANSITS_DIR = ROOT / "data" / "transits"
RAW_CSV = ROOT / "data" / "raw" / "portwatch_chokepoints.csv"
# Sidecar holding the real fetch time of RAW_CSV (file mtime is reset by git checkout).
RAW_META = ROOT / "data" / "raw" / "portwatch_meta.json"

QUERY_URL = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
    "Daily_Chokepoints_Data/FeatureServer/0/query"
)
SOURCE_LABEL = "IMF PortWatch, Daily Chokepoints Data (portwatch.imf.org)"
PAGE_SIZE = 2000

# Contract slug -> PortWatch portid. IDs are stable; names are checked too.
STRAITS = {
    "hormuz": {"portid": "chokepoint6", "name": "Strait of Hormuz"},
    "bab_el_mandeb": {"portid": "chokepoint4", "name": "Bab el-Mandeb Strait"},
}
SHIP_TYPES = ("container", "dry_bulk", "general_cargo", "roro", "tanker")

RAW_FIELDS = [
    "date", "year", "month", "day", "portid", "portname",
    "n_container", "n_dry_bulk", "n_general_cargo", "n_roro", "n_tanker",
    "n_cargo", "n_total",
    "capacity_container", "capacity_dry_bulk", "capacity_general_cargo",
    "capacity_roro", "capacity_tanker", "capacity_cargo", "capacity",
]


# ---------------------------------------------------------------- fetching

def _get_json(params: dict, retries: int = 4) -> dict:
    url = QUERY_URL + "?" + urllib.parse.urlencode(params)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "chokepoint-desk/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.load(resp)
            if "error" in payload:
                raise RuntimeError(f"ArcGIS error: {payload['error']}")
            return payload
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as err:
            last_err = err
            time.sleep(2 ** attempt)
    raise RuntimeError(f"PortWatch query failed after {retries} attempts: {last_err}")


def fetch_rows() -> list[dict]:
    """All rows for the configured straits, every source field, paginated."""
    ids = ",".join(f"'{s['portid']}'" for s in STRAITS.values())
    rows: list[dict] = []
    offset = 0
    while True:
        payload = _get_json({
            "where": f"portid IN ({ids})",
            "outFields": ",".join(RAW_FIELDS),
            "orderByFields": "date ASC, portid ASC",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "returnGeometry": "false",
            "f": "json",
        })
        page = [f["attributes"] for f in payload.get("features", [])]
        rows.extend(page)
        if not page or not payload.get("exceededTransferLimit"):
            break
        offset += len(page)
    if not rows:
        raise RuntimeError("PortWatch returned no rows for the configured straits")
    return rows


def write_raw_csv(rows: list[dict]) -> None:
    RAW_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = RAW_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(RAW_CSV)


def write_raw_meta(fetched_at: str, row_count: int) -> None:
    write_json(RAW_META, {"fetched_at": fetched_at, "rows": row_count, "source": SOURCE_LABEL})


def read_raw_fetched_at() -> str:
    """Original UTC fetch time of the raw CSV; never guessed from file times."""
    try:
        meta = json.loads(RAW_META.read_text(encoding="utf-8"))
        return str(meta["fetched_at"])
    except (OSError, ValueError, KeyError) as err:
        raise RuntimeError(f"Cannot read the original fetch time from {RAW_META} ({err}); "
                           "refusing to date offline data") from err


def read_raw_csv() -> list[dict]:
    if not RAW_CSV.exists():
        raise RuntimeError(f"No raw copy at {RAW_CSV}; run once without --offline")
    with RAW_CSV.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- building

def _int(value) -> int:
    return int(value) if value not in (None, "") else 0


def build_strait(slug: str, rows: list[dict], fetched_at: str, offline: bool) -> dict:
    cfg = STRAITS[slug]
    mine = [r for r in rows if r["portid"] == cfg["portid"]]
    if not mine:
        raise RuntimeError(f"No rows for {slug} ({cfg['portid']})")
    names = {r["portname"] for r in mine}
    if names != {cfg["name"]}:
        raise RuntimeError(f"{slug}: unexpected portname(s) {names}; PortWatch IDs may have changed")

    by_date: dict[str, dict] = {}
    for r in mine:
        d = str(r["date"])[:10]
        if d in by_date:
            raise RuntimeError(f"{slug}: duplicate row for {d}")
        by_type = {t: _int(r[f"n_{t}"]) for t in SHIP_TYPES}
        total = _int(r["n_total"])
        if sum(by_type.values()) != total:
            raise RuntimeError(f"{slug} {d}: types sum {sum(by_type.values())} != n_total {total}")
        by_date[d] = {"date": d, "total": total, "by_type": by_type}

    dates = sorted(by_date)
    first, last = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    missing = []
    day = first
    while day <= last:
        if day.isoformat() not in by_date:
            missing.append(day.isoformat())
        day += timedelta(days=1)

    # ma7: trailing 7-calendar-day mean of total, only when all 7 days exist.
    daily = []
    for d in dates:
        window = [(date.fromisoformat(d) - timedelta(days=k)).isoformat() for k in range(7)]
        if all(w in by_date for w in window):
            ma7 = round(sum(by_date[w]["total"] for w in window) / 7, 2)
        else:
            ma7 = None
        daily.append({**by_date[d], "ma7": ma7})

    return {
        "strait": slug,
        "name": cfg["name"],
        "portwatch_id": cfg["portid"],
        "source": SOURCE_LABEL,
        "source_url": QUERY_URL.rsplit("/query", 1)[0],
        "fetched_at": fetched_at,
        # True when rebuilt from data/raw because PortWatch was not queried;
        # fetched_at is then the time the raw copy was originally fetched.
        "offline": offline,
        "last_data_date": dates[-1],
        "first_data_date": dates[0],
        "missing_dates": missing,
        "daily": daily,
    }


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="rebuild JSON from data/raw only")
    args = parser.parse_args()

    if args.offline:
        rows = read_raw_csv()
        fetched_iso = read_raw_fetched_at()
    else:
        rows = fetch_rows()
        if RAW_CSV.exists():
            previous = len(read_raw_csv())
            if len(rows) < previous:
                raise RuntimeError(f"PortWatch returned {len(rows)} rows, fewer than the {previous} "
                                   "already saved; refusing to shrink history")
        fetched_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # Build and validate everything before writing anything.
    outputs = {slug: build_strait(slug, rows, fetched_iso, args.offline) for slug in STRAITS}
    if not args.offline:
        write_raw_csv(rows)
        write_raw_meta(fetched_iso, len(rows))
    for slug, obj in outputs.items():
        write_json(TRANSITS_DIR / f"{slug}.json", obj)
        print(f"{slug}: {len(obj['daily'])} days {obj['first_data_date']}..{obj['last_data_date']}, "
              f"{len(obj['missing_dates'])} missing")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # noqa: BLE001 - any failure must exit non-zero for CI
        print(f"fetch_portwatch: FAILED: {err}", file=sys.stderr)
        sys.exit(1)
