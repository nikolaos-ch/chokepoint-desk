#!/usr/bin/env bash
# Daily refresh: pipeline (#2) then forecast (#3).
# Contract: on ANY failure, data/ is restored to the last good state and exit != 0.
# Override commands via env if script names differ.
set -euo pipefail
PIPELINE_CMD="${PIPELINE_CMD:-python scripts/fetch_portwatch.py}"
FORECAST_CMD="${FORECAST_CMD:-python scripts/forecast.py}"

backup="$(mktemp -d)"
[ -d data ] && cp -a data/. "$backup"/
restore() {
  echo "::error::Refresh failed, restoring last good data/" >&2
  rm -rf data && mkdir -p data && cp -a "$backup"/. data/
}
trap 'rc=$?; if [ $rc -ne 0 ]; then restore; fi; rm -rf "$backup"; exit $rc' EXIT

echo "== pipeline: $PIPELINE_CMD"; eval "$PIPELINE_CMD"
echo "== forecast: $FORECAST_CMD"; eval "$FORECAST_CMD"

# Sanity: outputs must be valid JSON
for f in data/transits/*.json data/forecast/*.json; do
  [ -e "$f" ] || { echo "missing outputs under data/" >&2; exit 1; }
  python -c "import json,sys; json.load(open(sys.argv[1]))" "$f"
done
echo "== refresh ok"
