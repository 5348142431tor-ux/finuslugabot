from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials


RAPIRA_RATES_URL = "https://api.rapira.net/open/market/rates"
RAPIRA_SYMBOL = "USDT/RUB"
SHEET_NAME = "курсы"
SHEET_RANGE = "A5:B5"
STATE_FILE = "data/rapira_rate_state.json"
MSK = ZoneInfo("Europe/Moscow")


@dataclass
class RateState:
    last_slot: str = ""


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def current_interval_minutes(now: datetime) -> int:
    return 5 if 8 <= now.hour < 20 else 20


def current_slot(now: datetime, interval: int) -> str:
    minute = (now.minute // interval) * interval
    slot = now.replace(minute=minute, second=0, microsecond=0)
    return slot.isoformat()


def should_run(now: datetime, state: RateState) -> bool:
    interval = current_interval_minutes(now)
    if now.minute % interval != 0:
        return False
    return current_slot(now, interval) != state.last_slot


def load_state(path: Path) -> RateState:
    if not path.exists():
        return RateState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return RateState()
    return RateState(last_slot=str(raw.get("last_slot", "")))


def save_state(path: Path, now: datetime) -> None:
    interval = current_interval_minutes(now)
    payload = {"last_slot": current_slot(now, interval)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_rapira_rate() -> str:
    with urllib.request.urlopen(RAPIRA_RATES_URL, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    for item in payload.get("data", []):
        if item.get("symbol") == RAPIRA_SYMBOL:
            rate = item.get("close") or item.get("askPrice") or item.get("bidPrice")
            if rate is None:
                break
            return str(rate).replace(".", ",")
    raise RuntimeError("Rapira USDT/RUB rate not found")


def update_sheet(rate_value: str) -> None:
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    spreadsheet_key = os.environ["SPREADSHEET_KEY"]
    creds = Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(spreadsheet_key)
    worksheet = spreadsheet.worksheet(SHEET_NAME)
    worksheet.update(values=[[rate_value, "rapira usdt/rub"]], range_name=SHEET_RANGE)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    load_env(root / ".env")
    now = datetime.now(MSK)
    state_path = root / STATE_FILE
    state = load_state(state_path)
    if not should_run(now, state):
        return 0
    rate_value = fetch_rapira_rate()
    update_sheet(rate_value)
    save_state(state_path, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
