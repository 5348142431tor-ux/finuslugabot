#!/bin/bash
set -euo pipefail
cd /Users/dmitriytormosov/FinuslugaBot
set -a
source .env
set +a
source .venv/bin/activate
TMP_JSON=$(mktemp /tmp/grinex-rates.XXXXXX.json)
trap 'rm -f "$TMP_JSON"' EXIT
curl -sSf https://grinex.io/rates?offset=0 -o "$TMP_JSON"
python scripts/write_rates_rows.py "$TMP_JSON"
