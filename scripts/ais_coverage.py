#!/usr/bin/env python3
"""Phase 0 coverage test (ticket #6): AIS gate crossings vs PortWatch, per day.

    python scripts/ais_coverage.py                 # both straits, prints a table
    python scripts/ais_coverage.py --json var/ais/coverage.json

Only full UTC days (at least 23 h connected) that PortWatch has already
settled (it lags ~2 days) are scored. Gate: under ~50% coverage is flagged.
Output is aggregate counts only, with no ship identities, and it is not published.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ais_lib as L  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=L.DB_PATH)
    ap.add_argument("--transits-dir", type=Path, default=L.ROOT / "data" / "transits")
    ap.add_argument("--json", type=Path, default=L.ROOT / "var" / "ais" / "coverage.json")
    ap.add_argument("--min-uptime", type=float, default=23.0, help="hours connected for a day to count")
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"no recorder database at {args.db}; run scripts/ais_recorder.py first", file=sys.stderr)
        return 2
    con = L.connect(args.db)
    reports = {}
    for strait in L.GATES:
        pw = L.load_portwatch(args.transits_dir / f"{strait}.json")
        rep = L.coverage_report(con, strait, pw, min_uptime_h=args.min_uptime)
        reports[strait] = rep
        print(f"\n== {strait}  (gate {rep['gate'][0]} -> {rep['gate'][1]})")
        print(f"{'date':<11}{'up_h':>6}{'ais':>6}{'unk':>5}{'pw':>6}{'cov':>7}{'tank':>7}{'cargo':>7}  status")
        for d in rep["days"]:
            a = d["ais"]
            if d["status"] == "scored":
                print(f"{d['date']:<11}{d['uptime_h']:>6.1f}{a['total_pw_comparable']:>6}{a['unknown']:>5}"
                      f"{d['portwatch']['total']:>6}{d['coverage_total']:>7.0%}"
                      f"{(d['coverage_tanker'] or 0):>7.0%}{(d['coverage_cargo'] or 0):>7.0%}  scored")
            else:
                print(f"{d['date']:<11}{d['uptime_h']:>6.1f}{a['total_pw_comparable']:>6}{a['unknown']:>5}"
                      f"{'-':>6}{'-':>7}{'-':>7}{'-':>7}  {d['status']}")
        print("summary:", rep["summary"]["verdict"] if rep["summary"] else "no scored days yet")
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(reports, indent=1), encoding="utf-8")
    print(f"\nwritten {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
