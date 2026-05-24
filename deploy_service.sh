#!/bin/bash
set -euo pipefail

SRC="/Users/aidima/Documents/finuslugabot/"
DST="/Users/aidima/finuslugabot_service/"
PLIST="/Users/aidima/Library/LaunchAgents/com.aidima.finuslugabot.plist"
RATE_PLIST="/Users/aidima/Library/LaunchAgents/com.aidima.finuslugabot.rapira-rate.plist"

mkdir -p "$DST"
mkdir -p "/Users/aidima/finuslugabot_data/chats"
rsync -a --delete \
  --exclude 'bot.log' \
  --exclude 'data' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  "$SRC" "$DST"

if [ ! -x "$DST/.venv/bin/python" ]; then
  python3 -m venv "$DST/.venv"
  "$DST/.venv/bin/python" -m pip install --upgrade pip
  "$DST/.venv/bin/pip" install -r "$DST/requirements.txt"
fi

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

if [ -f "$RATE_PLIST" ]; then
  launchctl bootout "gui/$(id -u)" "$RATE_PLIST" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$RATE_PLIST"
fi
