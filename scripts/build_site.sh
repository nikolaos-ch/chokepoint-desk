#!/usr/bin/env bash
# Allowlist copy of the public site into public/ (nothing else is ever published).
set -euo pipefail
rm -rf public && mkdir -p public/data
cp index.html styles.css app.js public/
cp -r data/transits data/forecast public/data/
cp data/rules.json data/ledger.json public/data/
echo "built public/: $(find public -type f | wc -l) files"
