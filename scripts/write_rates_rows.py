#!/usr/bin/env python3
import argparse
import json
import os
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

def load_sheet() -> gspread.Worksheet:
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    worksheet_name = os.environ.get("RATES_WORKSHEET", "курсы")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    gc = gspread.authorize(creds)

    spreadsheet_key = os.environ.get("SPREADSHEET_KEY")
    if spreadsheet_key:
        spreadsheet = gc.open_by_key(spreadsheet_key)
    else:
        spreadsheet_name = os.environ.get("SPREADSHEET_NAME", "TelegramMath")
        spreadsheet = gc.open(spreadsheet_name)
    try:
        return spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        for ws in spreadsheet.worksheets():
            if ws.title.lower() == worksheet_name.lower():
                return ws
        raise

def build_rows(data: dict[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = []
    for pair, payload in data.items():
        label_base = pair.upper()
        if isinstance(payload, dict):
            for side in ("sell", "buy"):
                value = payload.get(side)
                if value is None:
                    continue
                side_label = "продажа" if side == "sell" else "покупка"
                rows.append([f"{label_base} {side_label}", str(value)])
        else:
            rows.append([label_base, str(payload)])
    return rows

def main():
    parser = argparse.ArgumentParser(description="Записать курсы из JSON в лист начиная с заданной строки")
    parser.add_argument("json_path", help="Путь к rates.json")
    args = parser.parse_args()
    with open(args.json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit("JSON должен быть объектом")
    worksheet = load_sheet()
    rows = build_rows(data)
    start_row = int(os.environ.get("RATES_START_ROW", "5"))
    worksheet.batch_clear([f"A{start_row}:B1000"])
    worksheet.update(f"A{start_row}", rows)
    print(f"Записано {len(rows)} строк, начиная с A{start_row}")

if __name__ == "__main__":
    main()
