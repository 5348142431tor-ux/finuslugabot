#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
set -a
source .env
set +a
export PYTHONDONTWRITEBYTECODE=1
exec .venv/bin/python -m bot_app.rate_updater >> rate_updater.log 2>&1
