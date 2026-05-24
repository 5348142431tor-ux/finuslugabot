from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Optional

import gspread
import requests
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from .constants import SUPPORTED_CURRENCY_ORDER
from .models import ExchangeRequestRow, SheetRow
from .utils import format_decimal, normalize_chat_text, to_decimal

LOGGER = logging.getLogger("sheet_math_bot")

VERIFICATION_STATUS_COLUMN = "verification_status"
VERIFICATION_STATUS_OPTIONS = ("не верифицирован", "верифицирован")
COMMUNICATION_STYLE_COLUMN = "communication_style"
COMMUNICATION_STYLE_OPTIONS = ("Деловой", "Дружеский")
DIALOG_EXAMPLES_COUNT_COLUMN = "dialog_examples_count"
SETTINGS_WORKSHEET = "Настройка"
LEGACY_SETTINGS_WORKSHEET = "Настройки"
REQUESTS_WORKSHEET = "Заявки"
MARKET_RATES_WORKSHEET = "курсы"
REQUEST_STATUS_COLORS = {
    "готовы": {"red": 0.72, "green": 0.9, "blue": 0.72},
    "ожидание": {"red": 0.98, "green": 0.91, "blue": 0.6},
    "не готовы": {"red": 0.95, "green": 0.72, "blue": 0.72},
}
REQUEST_STATUS_OPTIONS = ("ожидание", "готовы", "не готовы")
REQUESTS_CURRENCY_COLUMNS = {
    "RUB": "рубль",
    "USDT": "юсдт",
    "USD": "доллар",
    "EUR": "евро",
    "TRY": "лира",
    "KZT": "тенге",
}
DEFAULT_GREETINGS = {
    "Деловой": [
        "Здравствуйте! Чем могу помочь?",
        "Здравствуйте. Подскажите, пожалуйста, чем могу быть полезен?",
        "Добрый день! Готов помочь вам.",
        "Здравствуйте! Слушаю вас.",
        "Добрый день. Чем могу помочь сегодня?",
    ],
    "Дружеский": [
        "Привет! Чем помочь?",
        "Здравствуйте! С радостью помогу.",
        "Добрый день! Чем могу быть полезен?",
        "Привет! Подскажите, что нужно сделать.",
        "Здравствуйте! Давайте помогу.",
    ],
}
DEFAULT_THANKS_RULES = [
    ("спасибо", "Пожалуйста"),
    ("спасибо большое", "Пожалуйста"),
    ("благодарю", "Пожалуйста"),
    ("благодарю вас", "Пожалуйста"),
    ("спс", "Пожалуйста"),
    ("thanks", "Пожалуйста"),
    ("thank you", "Пожалуйста"),
    ("thx", "Пожалуйста"),
]


class SheetRepository:
    def __init__(self, allowed_currencies: set[str]):
        self.allowed_currencies = allowed_currencies
        creds_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        worksheet = os.environ.get("WORKSHEET_NAME", "Calculations")
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        gc = gspread.authorize(creds)

        spreadsheet_key = os.environ.get("SPREADSHEET_KEY")
        if spreadsheet_key:
            spreadsheet = gc.open_by_key(spreadsheet_key)
        else:
            spreadsheet_name = os.environ.get("SPREADSHEET_NAME", "TelegramMath")
            spreadsheet = gc.open(spreadsheet_name)
        self.spreadsheet = spreadsheet

        try:
            self.sheet = spreadsheet.worksheet(worksheet)
        except gspread.WorksheetNotFound:
            LOGGER.info("Worksheet '%s' not found, creating", worksheet)
            self.sheet = spreadsheet.add_worksheet(title=worksheet, rows=100, cols=6)
        try:
            self.settings_sheet = spreadsheet.worksheet(SETTINGS_WORKSHEET)
        except gspread.WorksheetNotFound:
            try:
                legacy_sheet = spreadsheet.worksheet(LEGACY_SETTINGS_WORKSHEET)
                legacy_sheet.update_title(SETTINGS_WORKSHEET)
                self.settings_sheet = legacy_sheet
            except gspread.WorksheetNotFound:
                LOGGER.info("Worksheet '%s' not found, creating", SETTINGS_WORKSHEET)
                self.settings_sheet = spreadsheet.add_worksheet(title=SETTINGS_WORKSHEET, rows=100, cols=3)
        try:
            self.requests_sheet = spreadsheet.worksheet(REQUESTS_WORKSHEET)
        except gspread.WorksheetNotFound:
            LOGGER.info("Worksheet '%s' not found, creating", REQUESTS_WORKSHEET)
            self.requests_sheet = spreadsheet.add_worksheet(title=REQUESTS_WORKSHEET, rows=200, cols=11)
        try:
            self.market_rates_sheet = spreadsheet.worksheet(MARKET_RATES_WORKSHEET)
        except gspread.WorksheetNotFound:
            self.market_rates_sheet = None
        self.lock = asyncio.Lock()

        base_header = [
            VERIFICATION_STATUS_COLUMN,
            COMMUNICATION_STYLE_COLUMN,
            DIALOG_EXAMPLES_COUNT_COLUMN,
            "chat_id",
            "chat_name",
            "user",
            "expression",
            "result",
            "timestamp",
        ]
        header_row = self._ensure_communication_style_layout(base_header)
        total_cols = max(self.sheet.col_count, len(header_row))
        if len(header_row) < total_cols:
            header_row += [""] * (total_cols - len(header_row))

        self.header_titles = header_row.copy()
        self.column_map: dict[str, int] = {
            title: idx for idx, title in enumerate(self.header_titles, start=1) if title
        }
        self.verification_column = self.column_map.get(VERIFICATION_STATUS_COLUMN, 1)
        self.style_column = self.column_map.get(COMMUNICATION_STYLE_COLUMN, 2)
        self.dialog_examples_column = self.column_map.get(DIALOG_EXAMPLES_COUNT_COLUMN, 3)
        self.chat_id_column = self.column_map.get("chat_id", 4)
        self.chat_name_column = self.column_map.get("chat_name", 5)
        self.user_column = self.column_map.get("user", 6)
        self.expression_column = self.column_map.get("expression", 7)
        self.result_column = self.column_map.get("result", 8)
        self.timestamp_column = self.column_map.get("timestamp", 9)
        self._apply_communication_style_validation()
        self._ensure_settings_layout()
        self._ensure_requests_layout()
        self.currency_columns: dict[str, int] = {}
        for title, idx in self.column_map.items():
            if title.startswith("result_") and title != "result":
                self.currency_columns[title.split("_", 1)[1].upper()] = idx

        for currency_code in SUPPORTED_CURRENCY_ORDER:
            if currency_code in self.allowed_currencies and currency_code not in self.currency_columns:
                self._ensure_currency_column(currency_code)

        self.chat_row_cache: dict[str, int] = {}
        for idx, value in enumerate(self.sheet.col_values(self.chat_id_column), start=1):
            if value and value != "chat_id":
                self.chat_row_cache[value] = idx

    def _rewrite_sheet(self, rows: list[list[str]]) -> list[str]:
        self.sheet.clear()
        end_cell = rowcol_to_a1(len(rows), max(len(row) for row in rows))
        self.sheet.update(f"A1:{end_cell}", rows)
        return rows[0]

    def _ensure_communication_style_layout(self, base_header: list[str]) -> list[str]:
        values = self.sheet.get_all_values()
        if not values:
            self.sheet.update("A1:I1", [base_header])
            return base_header.copy()

        header_row = values[0]
        if "chat_id" in header_row:
            chat_idx = header_row.index("chat_id")
            verification_indices = [idx for idx, title in enumerate(header_row[:chat_idx]) if title == VERIFICATION_STATUS_COLUMN]
            style_indices = [idx for idx, title in enumerate(header_row[:chat_idx]) if title == COMMUNICATION_STYLE_COLUMN]
            dialog_indices = [idx for idx, title in enumerate(header_row[:chat_idx]) if title == DIALOG_EXAMPLES_COUNT_COLUMN]
            canonical_prefix = [VERIFICATION_STATUS_COLUMN, COMMUNICATION_STYLE_COLUMN, DIALOG_EXAMPLES_COUNT_COLUMN, "chat_id"]
            if (
                header_row[:4] != canonical_prefix
                or len(verification_indices) != 1
                or len(style_indices) != 1
                or len(dialog_indices) != 1
            ):
                verification_idx = verification_indices[-1] if verification_indices else None
                style_idx = style_indices[-1] if style_indices else None
                dialog_idx = dialog_indices[-1] if dialog_indices else None
                source_header = header_row[chat_idx:]
                rows = [[VERIFICATION_STATUS_COLUMN, COMMUNICATION_STYLE_COLUMN, DIALOG_EXAMPLES_COUNT_COLUMN] + source_header]
                for row in values[1:]:
                    payload = row[chat_idx:]
                    if not any(payload):
                        continue
                    verification_value = (
                        row[verification_idx].strip()
                        if verification_idx is not None and verification_idx < len(row) and row[verification_idx].strip() in VERIFICATION_STATUS_OPTIONS
                        else VERIFICATION_STATUS_OPTIONS[0]
                    )
                    style_value = (
                        row[style_idx].strip()
                        if style_idx is not None and style_idx < len(row) and row[style_idx].strip() in COMMUNICATION_STYLE_OPTIONS
                        else COMMUNICATION_STYLE_OPTIONS[0]
                    )
                    dialog_value = (
                        row[dialog_idx].strip()
                        if dialog_idx is not None and dialog_idx < len(row) and row[dialog_idx].strip()
                        else "0"
                    )
                    rows.append([verification_value, style_value, dialog_value] + payload)
                header_row = self._rewrite_sheet(rows)
                values = self.sheet.get_all_values()
                return header_row
            return header_row
        if header_row.count("chat_id") > 1:
            old_start = max(idx for idx, title in enumerate(header_row, start=1) if title == "chat_id")
            source_header = header_row[old_start - 1 :]
            rows = [[COMMUNICATION_STYLE_COLUMN] + source_header]
            for row in values[1:]:
                payload = row[old_start - 1 :]
                if any(payload):
                    style_value = row[0] if row and row[0] in COMMUNICATION_STYLE_OPTIONS else COMMUNICATION_STYLE_OPTIONS[0]
                    rows.append([style_value] + payload)
            header_row = self._rewrite_sheet(rows)
            values = self.sheet.get_all_values()

        if not header_row or header_row[0] != COMMUNICATION_STYLE_COLUMN:
            rows = [base_header[:1] + header_row]
            for row in values[1:]:
                if any(row):
                    rows.append([COMMUNICATION_STYLE_OPTIONS[0]] + row)
            header_row = self._rewrite_sheet(rows)
            values = self.sheet.get_all_values()

        header_row = values[0]
        if VERIFICATION_STATUS_COLUMN not in header_row:
            rows = [[VERIFICATION_STATUS_COLUMN] + header_row]
            for row in values[1:]:
                if any(row):
                    rows.append([VERIFICATION_STATUS_OPTIONS[0]] + row)
            header_row = self._rewrite_sheet(rows)
            values = self.sheet.get_all_values()

        header_row = values[0]
        if DIALOG_EXAMPLES_COUNT_COLUMN not in header_row:
            rows = []
            for row_index, row in enumerate(values):
                if row_index == 0:
                    rows.append(row[:2] + [DIALOG_EXAMPLES_COUNT_COLUMN] + row[2:])
                else:
                    rows.append((row[:2] + ["0"] + row[2:]) if any(row) else row[:2] + ["0"] + row[2:])
            return self._rewrite_sheet(rows)

        return header_row

    def _apply_communication_style_validation(self) -> None:
        self.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": self.sheet.id,
                                "startRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": 1,
                            },
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": [
                                        {"userEnteredValue": option}
                                        for option in VERIFICATION_STATUS_OPTIONS
                                    ],
                                },
                                "showCustomUi": True,
                                "strict": True,
                            },
                        }
                    },
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": self.sheet.id,
                                "startRowIndex": 1,
                                "startColumnIndex": 1,
                                "endColumnIndex": 2,
                            },
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": [
                                        {"userEnteredValue": option}
                                        for option in COMMUNICATION_STYLE_OPTIONS
                                    ],
                                },
                                "showCustomUi": True,
                                "strict": True,
                            },
                        }
                    }
                ]
            }
        )
        self._apply_verification_conditional_formatting()
        self._apply_style_conditional_formatting()
        self._apply_dialog_examples_conditional_formatting()

    def _apply_verification_conditional_formatting(self) -> None:
        requests = [
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": self.sheet.id,
                            "startRowIndex": 1,
                            "startColumnIndex": self.verification_column - 1,
                            "endColumnIndex": self.verification_column,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": "верифицирован"}],
                            },
                            "format": {"backgroundColor": {"red": 0.72, "green": 0.9, "blue": 0.72}},
                        },
                    },
                    "index": 0,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": self.sheet.id,
                            "startRowIndex": 1,
                            "startColumnIndex": self.verification_column - 1,
                            "endColumnIndex": self.verification_column,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": "не верифицирован"}],
                            },
                            "format": {"backgroundColor": {"red": 0.95, "green": 0.72, "blue": 0.72}},
                        },
                    },
                    "index": 1,
                }
            },
        ]
        metadata = self.spreadsheet.fetch_sheet_metadata()
        target_sheet = None
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") == self.sheet.id:
                target_sheet = sheet
                break
        existing_rules = target_sheet.get("conditionalFormats", []) if target_sheet else []
        delete_requests = []
        for index in range(len(existing_rules) - 1, -1, -1):
            rule = existing_rules[index]
            ranges = rule.get("ranges", [])
            if any(r.get("sheetId") == self.sheet.id and r.get("startColumnIndex") == self.verification_column - 1 for r in ranges):
                delete_requests.append({
                    "deleteConditionalFormatRule": {
                        "sheetId": self.sheet.id,
                        "index": index,
                    }
                })
        self.spreadsheet.batch_update({"requests": delete_requests + requests})

    def _apply_style_conditional_formatting(self) -> None:
        requests = [
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": self.sheet.id,
                            "startRowIndex": 1,
                            "startColumnIndex": self.style_column - 1,
                            "endColumnIndex": self.style_column,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": "Деловой"}],
                            },
                            "format": {"backgroundColor": {"red": 0.98, "green": 0.91, "blue": 0.6}},
                        },
                    },
                    "index": 0,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": self.sheet.id,
                            "startRowIndex": 1,
                            "startColumnIndex": self.style_column - 1,
                            "endColumnIndex": self.style_column,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "TEXT_EQ",
                                "values": [{"userEnteredValue": "Дружеский"}],
                            },
                            "format": {"backgroundColor": {"red": 0.72, "green": 0.9, "blue": 0.72}},
                        },
                    },
                    "index": 1,
                }
            },
        ]
        metadata = self.spreadsheet.fetch_sheet_metadata()
        target_sheet = None
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") == self.sheet.id:
                target_sheet = sheet
                break
        existing_rules = target_sheet.get("conditionalFormats", []) if target_sheet else []
        delete_requests = []
        for index in range(len(existing_rules) - 1, -1, -1):
            rule = existing_rules[index]
            ranges = rule.get("ranges", [])
            if any(r.get("sheetId") == self.sheet.id and r.get("startColumnIndex") == self.style_column - 1 for r in ranges):
                delete_requests.append({
                    "deleteConditionalFormatRule": {
                        "sheetId": self.sheet.id,
                        "index": index,
                    }
                })
        self.spreadsheet.batch_update({"requests": delete_requests + requests})

    def _apply_dialog_examples_conditional_formatting(self) -> None:
        requests = [
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": self.sheet.id,
                            "startRowIndex": 1,
                            "startColumnIndex": self.dialog_examples_column - 1,
                            "endColumnIndex": self.dialog_examples_column,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "NUMBER_LESS",
                                "values": [{"userEnteredValue": "50"}],
                            },
                            "format": {"backgroundColor": {"red": 0.95, "green": 0.72, "blue": 0.72}},
                        },
                    },
                    "index": 0,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": self.sheet.id,
                            "startRowIndex": 1,
                            "startColumnIndex": self.dialog_examples_column - 1,
                            "endColumnIndex": self.dialog_examples_column,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "NUMBER_GREATER_THAN_EQ",
                                "values": [{"userEnteredValue": "50"}],
                            },
                            "format": {"backgroundColor": {"red": 0.98, "green": 0.91, "blue": 0.6}},
                        },
                    },
                    "index": 1,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": self.sheet.id,
                            "startRowIndex": 1,
                            "startColumnIndex": self.dialog_examples_column - 1,
                            "endColumnIndex": self.dialog_examples_column,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "NUMBER_GREATER_THAN_EQ",
                                "values": [{"userEnteredValue": "100"}],
                            },
                            "format": {"backgroundColor": {"red": 0.72, "green": 0.9, "blue": 0.72}},
                        },
                    },
                    "index": 2,
                }
            },
        ]
        metadata = self.spreadsheet.fetch_sheet_metadata()
        target_sheet = None
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") == self.sheet.id:
                target_sheet = sheet
                break
        existing_rules = target_sheet.get("conditionalFormats", []) if target_sheet else []
        delete_requests = []
        for index in range(len(existing_rules) - 1, -1, -1):
            rule = existing_rules[index]
            ranges = rule.get("ranges", [])
            if any(r.get("sheetId") == self.sheet.id and r.get("startColumnIndex") == self.dialog_examples_column - 1 for r in ranges):
                delete_requests.append({
                    "deleteConditionalFormatRule": {
                        "sheetId": self.sheet.id,
                        "index": index,
                    }
                })
        self.spreadsheet.batch_update({"requests": delete_requests + requests})

    def _ensure_settings_layout(self) -> None:
        values = self.settings_sheet.get_all_values()
        header = ["key", "value", "", "incoming_text", "reply_text"]
        def build_greeting_rows() -> list[list[str]]:
            rows: list[list[str]] = []
            for style, greetings in DEFAULT_GREETINGS.items():
                group = f"greeting_{style}"
                for text in greetings:
                    rows.append([group, text, "", "", ""])
            return rows
        def build_thanks_rows() -> list[list[str]]:
            return [["", "", "", incoming, reply] for incoming, reply in DEFAULT_THANKS_RULES]
        if not values:
            rows = [header] + build_greeting_rows() + build_thanks_rows()
            self.settings_sheet.update(f"A1:E{len(rows)}", rows)
            return
        current_header = values[0] + [""] * (len(header) - len(values[0]))
        if current_header[: len(header)] != header:
            self.settings_sheet.update("A1:E1", [header])
        populated_groups = {row[0].strip() for row in values[1:] if len(row) >= 2 and row[0].strip() and row[1].strip()}
        expected_groups = {f"greeting_{style}" for style in DEFAULT_GREETINGS}
        if not populated_groups.intersection(expected_groups):
            start_row = len(values) + 1
            rows = build_greeting_rows()
            self.settings_sheet.update(f"A{start_row}:E{start_row + len(rows) - 1}", rows)
            values = self.settings_sheet.get_all_values()
        has_thanks_rules = any(
            len(row) >= 5 and row[3].strip() and row[4].strip()
            for row in values[1:]
        )
        if not has_thanks_rules:
            start_row = len(values) + 1
            rows = build_thanks_rows()
            self.settings_sheet.update(f"A{start_row}:E{start_row + len(rows) - 1}", rows)

    def _ensure_currency_column(self, currency: str) -> int:
        currency = currency.upper()
        if currency in self.currency_columns:
            return self.currency_columns[currency]
        header_name = f"result_{currency}"
        current_cols = self.sheet.col_count
        self.sheet.add_cols(1)
        new_index = current_cols + 1
        self.sheet.update(values=[[header_name]], range_name=rowcol_to_a1(1, new_index))
        self.currency_columns[currency] = new_index
        self.column_map[header_name] = new_index
        self.header_titles.append(header_name)
        return new_index

    def _ensure_requests_layout(self) -> None:
        header = ["статус", "дата", "номер"] + list(REQUESTS_CURRENCY_COLUMNS.values()) + ["chat_id", "chat_name"]
        values = self.requests_sheet.get_all_values()
        if not values:
            self.requests_sheet.update("A1:L1", [header])
            self._apply_requests_status_validation(2)
            return
        header_row = self._find_requests_header_row(values)
        if header_row is not None:
            self._apply_requests_status_validation(header_row + 1)
            return
        current = values[0]
        rows = [header]
        if current[:11] == ["дата", "номер"] + list(REQUESTS_CURRENCY_COLUMNS.values()) + ["chat_id", "chat_name", "status"]:
            for row in values[1:]:
                padded = row + [""] * (11 - len(row))
                status = (padded[10] or "ожидание").strip() or "ожидание"
                rows.append([status] + padded[:10])
            self.requests_sheet.clear()
            self.requests_sheet.update(f"A1:L{len(rows)}", rows)
            for idx in range(2, len(rows) + 1):
                self._set_request_status_color(idx, rows[idx - 1][0])
            self._apply_requests_status_validation(2)
            return
        self.requests_sheet.update("A1:L1", [header])
        self._apply_requests_status_validation(2)

    def _find_requests_header_row(self, values: list[list[str]]) -> int | None:
        target = ["статус", "дата", "номер"]
        for idx, row in enumerate(values[:10], start=1):
            padded = row + [""] * max(0, 3 - len(row))
            if padded[:3] == target:
                return idx
        return None

    def _requests_data_start_row(self) -> int:
        values = self.requests_sheet.get_all_values()
        header_row = self._find_requests_header_row(values)
        return (header_row + 1) if header_row is not None else 2

    def _apply_requests_status_validation(self, start_row: int) -> None:
        self.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": self.requests_sheet.id,
                                "startRowIndex": max(start_row - 1, 1),
                                "startColumnIndex": 0,
                                "endColumnIndex": 1,
                            },
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": [
                                        {"userEnteredValue": option}
                                        for option in REQUEST_STATUS_OPTIONS
                                    ],
                                },
                                "showCustomUi": True,
                                "strict": True,
                            },
                        }
                    }
                ]
            }
        )
        self._apply_requests_status_conditional_formatting(start_row)

    def _apply_requests_status_conditional_formatting(self, start_row: int) -> None:
        metadata = self.spreadsheet.fetch_sheet_metadata()
        target_sheet = None
        for sheet in metadata.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("sheetId") == self.requests_sheet.id:
                target_sheet = sheet
                break
        existing_rules = target_sheet.get("conditionalFormats", []) if target_sheet else []
        requests = []
        for index in range(len(existing_rules) - 1, -1, -1):
            requests.append({
                "deleteConditionalFormatRule": {
                    "sheetId": self.requests_sheet.id,
                    "index": index,
                }
            })
        for status, color in reversed(list(REQUEST_STATUS_COLORS.items())):
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": self.requests_sheet.id,
                            "startRowIndex": max(start_row - 1, 1),
                            "startColumnIndex": 0,
                            "endColumnIndex": 11,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{
                                    "userEnteredValue": f'=$A{start_row}="{status}"',
                                }],
                            },
                            "format": {
                                "backgroundColor": color,
                            },
                        },
                    },
                    "index": 0,
                }
            })
        if requests:
            self.spreadsheet.batch_update({"requests": requests})

    def _set_request_status_color(self, row_idx: int, status: str) -> None:
        color = REQUEST_STATUS_COLORS.get(status.strip().lower())
        if not color:
            return
        self.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": self.requests_sheet.id,
                                "startRowIndex": row_idx - 1,
                                "endRowIndex": row_idx,
                                "startColumnIndex": 0,
                                "endColumnIndex": 12,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": color,
                                }
                            },
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    }
                ]
            }
        )

    async def _get_cell_value(self, row_idx: int, col_idx: int) -> Optional[str]:
        for attempt in range(3):
            try:
                return self.sheet.cell(row_idx, col_idx).value
            except requests.exceptions.RequestException:
                if attempt == 2:
                    raise
                await asyncio.sleep(1)
        return None

    async def ensure_row(self, chat_id: str, chat_name: str, user_name: str) -> None:
        async with self.lock:
            if chat_id in self.chat_row_cache:
                return
            self._insert_row(chat_id, chat_name, user_name)

    def _insert_row(self, chat_id: str, chat_name: str, user_name: str) -> int:
        next_row = len(self.sheet.col_values(self.chat_id_column)) + 1
        self.sheet.update(
            range_name=f"A{next_row}:H{next_row}",
            values=[[
                VERIFICATION_STATUS_OPTIONS[0],
                COMMUNICATION_STYLE_OPTIONS[0],
                chat_id,
                chat_name,
                user_name,
                "",
                "0",
                "",
            ]],
        )
        updates = []
        for col_idx in self.currency_columns.values():
            updates.append({"range": rowcol_to_a1(next_row, col_idx), "values": [["0"]]})
        if updates:
            self.sheet.batch_update(updates)
        self.chat_row_cache[chat_id] = next_row
        return next_row

    async def upsert(self, row: SheetRow) -> Decimal:
        async with self.lock:
            currency_col = self._ensure_currency_column(row.currency)
            row_idx = self.chat_row_cache.get(row.chat_id)
            if row_idx is None:
                row_idx = self._insert_row(row.chat_id, row.chat_name, row.user)

            current_total = to_decimal(await self._get_cell_value(row_idx, currency_col))
            new_total = current_total + row.delta
            self.sheet.batch_update(
                [
                    {
                        "range": f"{rowcol_to_a1(row_idx, self.chat_id_column)}:{rowcol_to_a1(row_idx, self.expression_column)}",
                        "values": [[row.chat_id, row.chat_name, row.user, row.expression]],
                    },
                    {
                        "range": rowcol_to_a1(row_idx, self.timestamp_column),
                        "values": [[row.timestamp]],
                    },
                    {
                        "range": rowcol_to_a1(row_idx, currency_col),
                        "values": [[format_decimal(new_total)]],
                    },
                ]
            )
            return new_total

    async def get_currency_totals(self, chat_id: str) -> dict[str, Decimal]:
        async with self.lock:
            row_idx = self.chat_row_cache.get(chat_id)
            result: dict[str, Decimal] = {}
            for code in SUPPORTED_CURRENCY_ORDER:
                col_idx = self.currency_columns.get(code)
                if not row_idx or not col_idx:
                    result[code] = Decimal("0")
                else:
                    result[code] = to_decimal(self.sheet.cell(row_idx, col_idx).value)
            return result

    async def get_communication_style(self, chat_id: str) -> str:
        async with self.lock:
            row_idx = self.chat_row_cache.get(chat_id)
            if not row_idx:
                return COMMUNICATION_STYLE_OPTIONS[0]
            value = self.sheet.cell(row_idx, self.style_column).value
            if value in COMMUNICATION_STYLE_OPTIONS:
                return value
            return COMMUNICATION_STYLE_OPTIONS[0]

    async def is_verified_chat(self, chat_id: str) -> bool:
        async with self.lock:
            row_idx = self.chat_row_cache.get(chat_id)
            if not row_idx:
                return False
            value = (self.sheet.cell(row_idx, self.verification_column).value or "").strip().lower()
            return value == "верифицирован"

    async def get_greeting_variants(self, style: str) -> list[str]:
        async with self.lock:
            group = f"greeting_{style}"
            rows = self.settings_sheet.get_all_values()
            variants = [row[1].strip() for row in rows[1:] if len(row) >= 2 and row[0].strip() == group and row[1].strip()]
            if variants:
                return variants
            return DEFAULT_GREETINGS.get(style, DEFAULT_GREETINGS["Деловой"])

    async def get_thanks_reply(self, text: str) -> str | None:
        async with self.lock:
            normalized = normalize_chat_text(text)
            rows = self.settings_sheet.get_all_values()
            for row in rows[1:]:
                if len(row) < 5:
                    continue
                incoming = normalize_chat_text(row[3])
                reply = row[4].strip()
                if incoming and reply and incoming == normalized:
                    return reply
            for incoming, reply in DEFAULT_THANKS_RULES:
                if normalize_chat_text(incoming) == normalized:
                    return reply
            return None

    async def get_rate_values(self, chat_id: str) -> list[tuple[str, str]]:
        async with self.lock:
            row_idx = self.chat_row_cache.get(chat_id)
            if not row_idx:
                return []
            header_row = self.sheet.row_values(1)
            row_values = self.sheet.row_values(row_idx)
            result: list[tuple[str, str]] = []
            eur_usd_titles = {"rate_eur/usd", "rate_eur_usd", "rate_eurusd"}
            for idx, title in enumerate(header_row, start=1):
                if not title or "rate" not in title.lower():
                    continue
                normalized_title = title.strip().lower().replace(" ", "")
                value = row_values[idx - 1] if idx - 1 < len(row_values) else ""
                value = value.strip() if isinstance(value, str) else value
                if not value:
                    continue
                if normalized_title in eur_usd_titles:
                    result.append(("евро/доллар", str(value)))
                    continue
                result.append((title, str(value)))
            return result

    async def get_market_quote(self, give_currency: str, receive_currency: str) -> tuple[Decimal, str] | None:
        async with self.lock:
            if not self.market_rates_sheet:
                return None
            direct_pair = f"{give_currency.lower()}/{receive_currency.lower()}"
            reverse_pair = f"{receive_currency.lower()}/{give_currency.lower()}"
            rows = self.market_rates_sheet.get_all_values()
            for row in rows[1:]:
                if len(row) < 4:
                    continue
                pair_name = (row[1] or "").strip().lower()
                if pair_name == direct_pair or direct_pair in pair_name:
                    raw_value = (row[2] or row[0] or "").strip()
                    if not raw_value:
                        return None
                    return to_decimal(raw_value), "give_times_rate"
                if pair_name == reverse_pair or reverse_pair in pair_name:
                    raw_value = (row[3] or "").strip()
                    if not raw_value:
                        return None
                    return to_decimal(raw_value), "receive_times_rate"
            if give_currency != "RUB" and receive_currency != "RUB":
                give_to_rub = self._get_base_to_rub_rate(rows, give_currency)
                receive_to_rub = self._get_base_to_rub_rate(rows, receive_currency)
                if give_to_rub and receive_to_rub and receive_to_rub != Decimal("0"):
                    return give_to_rub / receive_to_rub, "give_times_rate"
            return None

    def _get_base_to_rub_rate(self, rows: list[list[str]], currency: str) -> Decimal | None:
        target = currency.lower()
        for row in rows[1:]:
            if len(row) < 2:
                continue
            pair_name = (row[1] or "").strip().lower()
            if f"{target}/rub" not in pair_name:
                continue
            raw_value = (row[0] or "").strip()
            if not raw_value:
                continue
            value = to_decimal(raw_value)
            if value != Decimal("0"):
                return value
        return None

    async def append_exchange_request(self, row: ExchangeRequestRow) -> int:
        async with self.lock:
            next_row = len(self.requests_sheet.get_all_values()) + 1
            request_number = next_row - self._requests_data_start_row() + 1
            currency_values = {code: "" for code in REQUESTS_CURRENCY_COLUMNS}
            if row.give_amount is not None:
                currency_values[row.give_currency] = format_decimal(row.give_amount)
            if row.receive_amount is not None:
                currency_values[row.receive_currency] = format_decimal(-row.receive_amount)
            values = [[
                row.status,
                row.created_at,
                str(request_number),
                currency_values["RUB"],
                currency_values["USDT"],
                currency_values["USD"],
                currency_values["EUR"],
                currency_values["TRY"],
                currency_values["KZT"],
                row.chat_id,
                row.chat_name,
            ]]
            self.requests_sheet.update(f"A{next_row}:L{next_row}", values)
            self._set_request_status_color(next_row, row.status)
            return request_number

    async def update_exchange_request_status(self, request_number: int, status: str) -> None:
        async with self.lock:
            row_idx = self.find_request_row_by_number(request_number)
            if row_idx is None:
                return
            self.requests_sheet.update(f"A{row_idx}:A{row_idx}", [[status]])
            self._set_request_status_color(row_idx, status)

    async def update_dialog_examples_count(self, chat_id: str, count: int) -> None:
        async with self.lock:
            row_idx = self.chat_row_cache.get(chat_id)
            if not row_idx:
                return
            self.sheet.update(
                rowcol_to_a1(row_idx, self.dialog_examples_column),
                [[str(count)]],
            )

    def find_request_row_by_number(self, request_number: int) -> int | None:
        values = self.requests_sheet.get_all_values()
        start_row = self._requests_data_start_row()
        for idx, row in enumerate(values[start_row - 1 :], start=start_row):
            if len(row) >= 3 and str(row[2]).strip() == str(request_number):
                return idx
        return None

    async def sync_request_status_colors(self) -> None:
        async with self.lock:
            values = self.requests_sheet.get_all_values()
            start_row = self._requests_data_start_row()
            self._apply_requests_status_validation(start_row)
            for idx, row in enumerate(values[start_row - 1 :], start=start_row):
                if not row or not any(cell.strip() for cell in row if isinstance(cell, str)):
                    continue
                status = (row[0] if len(row) >= 1 else "").strip().lower()
                if status in REQUEST_STATUS_COLORS:
                    self._set_request_status_color(idx, status)
