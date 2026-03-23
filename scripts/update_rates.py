#!/usr/bin/env python3
import argparse
import json
import os
from typing import Any

import gspread
from google.oauth2.service_account import Credentials


def load_sheet() -> gspread.Worksheet:
    creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    worksheet_name = os.environ.get("RATES_WORKSHEET", "Курсы")
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
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        for ws in spreadsheet.worksheets():
            if ws.title.lower() == worksheet_name.lower():
                return ws
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=100, cols=3)
    return worksheet


def build_rows(data: dict[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = [["курс", "значение"]]
    for pair, payload in data.items():
        label_base = pair.upper()
        if isinstance(payload, dict):
            for side in ("buy", "sell"):
                value = payload.get(side)
                if value is None:
                    continue
                side_label = "покупка" if side == "buy" else "продажа"
                rows.append([f"{label_base} {side_label}", str(value)])
        else:
            rows.append([label_base, str(payload)])
    return rows


def main():
    parser = argparse.ArgumentParser(description="Обновить вкладку 'Курсы' из JSON с котировками")
    parser.add_argument("json_path", help="Путь к rates.json")
    args = parser.parse_args()

    with open(args.json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise SystemExit("JSON должен быть объектом с парами")

    worksheet = load_sheet()
    rows = build_rows(data)
    worksheet.clear()
    worksheet.update(range_name="A1", values=rows)
    print(f"Обновлено строк: {len(rows) - 1}")


if __name__ == "__main__":
    main()
