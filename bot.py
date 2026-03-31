import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import ast
import operator as op
import re
import certifi
import easyocr
import gspread
import requests
from types import SimpleNamespace

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("sheet_math_bot")

ALLOWED_BIN = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
    ast.FloorDiv: op.floordiv,
}
ALLOWED_UNARY = {ast.UAdd: op.pos, ast.USub: op.neg}

SUPPORTED_CURRENCY_ALIASES = {
    "USD": {"usd", "дол", "долл", "доллар", "dollar"},
    "USDT": {"usdt", "юсдт"},
    "RUB": {"rub", "руб", "рубль", "рублей"},
    "EUR": {"eur", "евро"},
    "TRY": {"try", "lira", "лира", "tl", "lir", "лир"},
}
ALIAS_TO_CURRENCY = {
    alias: code
    for code, aliases in SUPPORTED_CURRENCY_ALIASES.items()
    for alias in aliases
}
SUPPORTED_CURRENCY_ORDER = ["USD", "USDT", "RUB", "EUR", "TRY"]

OCR_ALIAS_TO_CODE = {
    "₽": "RUB",
    "РУБ": "RUB",
    "РУБЛЕЙ": "RUB",
    "РУБ": "RUB",
    "RUB": "RUB",
    "RUR": "RUB",
    "USD": "USD",
    "$": "USD",
    "USDT": "USDT",
    "EUR": "EUR",
    "€": "EUR",
    "TRY": "TRY",
    "₺": "TRY",
    "TL": "TRY",
    "ЛИРА": "TRY",
    "ЛИР": "TRY",
}
OCR_ALIAS_TO_CODE.update({alias.upper(): code for alias, code in ALIAS_TO_CURRENCY.items()})


def _to_decimal(value: str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _normalize_chat_id(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    if not text:
        return ""
    if text.lstrip("-").isdigit():
        return text
    try:
        return str(int(Decimal(text)))
    except (InvalidOperation, ValueError):
        return text


class ExpressionEvaluator:
    def __init__(self, max_length: int = 128, max_depth: int = 10):
        self.max_length = max_length
        self.max_depth = max_depth

    def evaluate(self, text: str) -> float:
        expr = text.strip()
        if not expr or len(expr) > self.max_length:
            raise ValueError("Пустое или слишком длинное выражение")
        tree = ast.parse(expr, mode="eval")
        return self._eval(tree.body, depth=0)

    def _eval(self, node: ast.AST, depth: int) -> Any:
        if depth > self.max_depth:
            raise ValueError("Слишком глубокое выражение")
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_BIN:
            left = self._eval(node.left, depth + 1)
            right = self._eval(node.right, depth + 1)
            return ALLOWED_BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in ALLOWED_UNARY:
            return ALLOWED_UNARY[type(node.op)](self._eval(node.operand, depth + 1))
        raise ValueError("Недопустимое выражение")


@dataclass
class SheetRow:
    chat_id: str
    chat_name: str
    user: str
    expression: str
    delta: Decimal
    timestamp: str
    currency: str | None


@dataclass
class ReceiptResult:
    amount: Decimal
    currency: str
    text: str


@dataclass
class PendingReceiptEntry:
    token: str
    target_chat_id: str
    target_chat_name: str
    user_name: str
    currency: str
    amount: Decimal
    filename: str
    prompt_chat_id: int | None = None
    prompt_message_id: int | None = None


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

        self.lock = asyncio.Lock()
        base_header = ["chat_id", "chat_name", "user", "expression", "result", "timestamp"]
        header_row = self.sheet.row_values(1)
        total_cols = max(self.sheet.col_count, len(base_header))
        if not header_row:
            self.sheet.update("A1:F1", [base_header])
            header_row = base_header + [""] * (total_cols - len(base_header))
        else:
            if len(header_row) < total_cols:
                header_row += [""] * (total_cols - len(header_row))
            if header_row[: len(base_header)] != base_header:
                self.sheet.update("A1:F1", [base_header])
                header_row[: len(base_header)] = base_header

        self.header_titles = header_row.copy()
        self.column_map: dict[str, int] = {
            title: idx for idx, title in enumerate(self.header_titles, start=1) if title
        }
        self.result_column = self.column_map.get("result", 5)
        self.timestamp_column = self.column_map.get("timestamp", 6)
        self.currency_columns: dict[str, int] = {}
        for title, idx in self.column_map.items():
            if title.startswith("result_") and title != "result":
                currency = title.split("_", 1)[1].upper()
                self.currency_columns[currency] = idx

        for currency_code in SUPPORTED_CURRENCY_ORDER:
            if currency_code in self.allowed_currencies and currency_code not in self.currency_columns:
                self._ensure_currency_column(currency_code)

        self.chat_row_cache: dict[str, int] = {}
        self.settings_title = os.environ.get("SETTINGS_WORKSHEET", "Настройка")
        self.settings_sheet = None
        self.log_title = os.environ.get("LOG_WORKSHEET", "Лог")
        self.log_sheet = None
        self.requisites_title = os.environ.get("REQUISITES_WORKSHEET", "Реквизиты")
        self.requisites_sheet = None
        self.rates_title = os.environ.get("RATES_WORKSHEET", "курсы")
        self.rates_sheet = None
        self.ruble_debts_title = os.environ.get("RUBLE_DEBTS_SHEET", "Долги рубль")
        self.ruble_debts_sheet = None
        self.log_row_cache: dict[str, int] = {}
        for idx, value in enumerate(self.sheet.col_values(1), start=1):
            if not value or value == "chat_id":
                continue
            self.chat_row_cache[value] = idx
        self._repair_chat_ids()
        self._ensure_requisites_rows()

    def _ensure_requisites_rows(self) -> None:
        sheet = self._get_requisites_sheet()
        if not sheet:
            return
        try:
            rows = sheet.get_all_values()
        except Exception:
            return
        existing = {_normalize_chat_id(row[0]) for row in rows[1:] if row and row[0]}
        additions: list[list[str]] = []
        chat_name_col = self.column_map.get("chat_name")
        for chat_id, row_idx in self.chat_row_cache.items():
            if chat_id in existing:
                continue
            if chat_name_col:
                try:
                    name = self.sheet.cell(row_idx, chat_name_col).value or chat_id
                except Exception:
                    name = chat_id
            else:
                name = chat_id
            additions.append([chat_id, name])
        if additions:
            sheet.append_rows(additions, value_input_option="USER_ENTERED")

    def _repair_chat_ids(self) -> None:
        try:
            all_rows = self.sheet.get_all_values()
        except Exception:
            return
        updates = []
        for offset, row in enumerate(all_rows[1:], start=2):
            chat_id_value = row[0].strip() if len(row) > 0 else ""
            if chat_id_value:
                continue
            candidate = row[8].strip() if len(row) > 8 else ""
            if not candidate:
                continue
            updates.append({"range": f"A{offset}", "values": [[candidate]]})
            updates.append({"range": f"I{offset}", "values": [[""]]})
            self.chat_row_cache[candidate] = offset
        if updates:
            self.sheet.batch_update(updates)

    def _ensure_currency_column(self, currency: str | None) -> int:
        if currency is None:
            return self.result_column
        currency = currency.upper()
        if currency not in self.allowed_currencies:
            raise ValueError(f"Currency {currency} is not supported")
        if currency in self.currency_columns:
            return self.currency_columns[currency]
        header_name = f"result_{currency}"
        current_cols = self.sheet.col_count
        self.sheet.add_cols(1)
        new_index = current_cols + 1
        self.sheet.update(values=[[header_name]], range_name=rowcol_to_a1(1, new_index))
        self.header_titles.append(header_name)
        self.column_map[header_name] = new_index
        self.currency_columns[currency] = new_index
        return new_index

    async def _get_cell_value(self, row_idx: int, col_idx: int) -> str | None:
        for attempt in range(3):
            try:
                return self.sheet.cell(row_idx, col_idx).value
            except requests.exceptions.RequestException:
                if attempt == 2:
                    raise
                await asyncio.sleep(1)
        return None

    async def upsert(self, row: SheetRow) -> Decimal:
        async with self.lock:
            currency_col = self._ensure_currency_column(row.currency)
            if row.chat_id in self.chat_row_cache:
                row_idx = self.chat_row_cache[row.chat_id]
                cell_value = await self._get_cell_value(row_idx, currency_col)
                current_total = _to_decimal(cell_value)
                new_total = current_total + row.delta
                chat_name_col = self.column_map.get("chat_name")
                if chat_name_col:
                    existing_chat_name = await self._get_cell_value(row_idx, chat_name_col)
                else:
                    existing_chat_name = None
                chat_name_value = existing_chat_name or row.chat_name
                updates = [
                    {
                        "range": f"A{row_idx}:D{row_idx}",
                        "values": [[row.chat_id, chat_name_value, row.user, row.expression]],
                    },
                    {
                        "range": rowcol_to_a1(row_idx, self.timestamp_column),
                        "values": [[row.timestamp]],
                    },
                    {
                        "range": rowcol_to_a1(row_idx, currency_col),
                        "values": [[_format_decimal(new_total)]],
                    },
                ]
                self.sheet.batch_update(updates)
                return new_total

            new_total = row.delta
            col_values = self.sheet.col_values(1)
            row_idx = len(col_values) + 1
            updates = [
                {"range": f"A{row_idx}:D{row_idx}", "values": [[row.chat_id, row.chat_name, row.user, row.expression]]},
                {"range": rowcol_to_a1(row_idx, self.timestamp_column), "values": [[row.timestamp]]},
                {"range": rowcol_to_a1(row_idx, currency_col), "values": [[_format_decimal(new_total)]]},
            ]
            self.sheet.batch_update(updates)
            self.chat_row_cache[row.chat_id] = row_idx
            return new_total


    async def ensure_row(self, chat_id: str, chat_name: str, user_name: str) -> None:
        async with self.lock:
            if chat_id in self.chat_row_cache:
                return
            base_payload = [chat_id, chat_name, user_name, "", "0", ""]
            col_values = self.sheet.col_values(1)
            next_row = len(col_values) + 1
            range_name = f"A{next_row}:F{next_row}"
            self.sheet.update(range_name=range_name, values=[base_payload])
            zero_updates = []
            for curr, col_idx in self.currency_columns.items():
                if col_idx == self.result_column:
                    continue
                zero_updates.append({"range": rowcol_to_a1(next_row, col_idx), "values": [["0"]]})
            if zero_updates:
                self.sheet.batch_update(zero_updates)
            self.chat_row_cache[chat_id] = next_row

    def _get_settings_sheet(self):
        if self.settings_sheet is not None:
            return self.settings_sheet
        spreadsheet = self.sheet.spreadsheet
        try:
            ws = spreadsheet.worksheet(self.settings_title)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=self.settings_title, rows=50, cols=2)
            ws.update("A1:B1", [["key", "value"]])
        self.settings_sheet = ws
        return ws

    def _get_requisites_sheet(self):
        if self.requisites_sheet is not None:
            return self.requisites_sheet
        spreadsheet = self.sheet.spreadsheet
        try:
            ws = spreadsheet.worksheet(self.requisites_title)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=self.requisites_title, rows=200, cols=10)
            ws.update("A1:B1", [["chat_id", "client_name"]])
        else:
            header = ws.row_values(1)
            if not header or len(header) < 2 or header[0].strip().lower() != "chat_id":
                ws.update("A1:B1", [["chat_id", "client_name"]])
        self.requisites_sheet = ws
        return ws

    def _get_rates_sheet(self):
        if self.rates_sheet is not None:
            return self.rates_sheet
        spreadsheet = self.sheet.spreadsheet
        try:
            ws = spreadsheet.worksheet(self.rates_title)
        except gspread.WorksheetNotFound:
            for sheet in spreadsheet.worksheets():
                if sheet.title.lower() == self.rates_title.lower():
                    self.rates_sheet = sheet
                    return sheet
            return None
        self.rates_sheet = ws
        return ws

    def _get_ruble_debts_sheet(self):
        if self.ruble_debts_sheet is not None:
            return self.ruble_debts_sheet
        spreadsheet = self.sheet.spreadsheet
        try:
            ws = spreadsheet.worksheet(self.ruble_debts_title)
        except gspread.WorksheetNotFound:
            return None
        self.ruble_debts_sheet = ws
        return ws

    def _get_log_sheet(self):
        if self.log_sheet is not None:
            return self.log_sheet
        spreadsheet = self.sheet.spreadsheet
        try:
            ws = spreadsheet.worksheet(self.log_title)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=self.log_title, rows=200, cols=5)
            ws.update("A1:D1", [["chat_id", "client_name", "topic", "log"]])
        self.log_sheet = ws
        self.log_row_cache = {}
        for idx, row in enumerate(ws.get_all_values()[1:], start=2):
            if not row or not row[0]:
                continue
            self.log_row_cache[row[0]] = idx
        return ws

    async def append_log(self, timestamp: str, chat_id: str, client_name: str, topic: str, event: str) -> None:
        async with self.lock:
            sheet = self._get_log_sheet()
            entry = f"{timestamp} — {event}"
            row_idx = self.log_row_cache.get(chat_id)
            if row_idx:
                current = sheet.cell(row_idx, 4).value or ""
                new_value = entry if not current else entry + "\n" + current
                sheet.update(rowcol_to_a1(row_idx, 4), [[new_value]])
            else:
                row_idx = len(sheet.col_values(1)) + 1
                sheet.append_row([chat_id, client_name, topic, entry], value_input_option="USER_ENTERED")
                self.log_row_cache[chat_id] = row_idx

    async def list_ruble_debts(self, chat_id: str) -> list[tuple[str, str]]:
        async with self.lock:
            sheet = self._get_ruble_debts_sheet()
            if not sheet:
                return []
            rows = sheet.get_all_values()
        result: list[tuple[str, str]] = []
        normalized = _normalize_chat_id(chat_id)
        for row in rows[1:]:
            if not row or not row[0]:
                continue
            cell_id = _normalize_chat_id(row[0])
            if cell_id != normalized:
                continue
            remaining = row[2].strip() if len(row) > 2 else ""
            in_work = row[3].strip() if len(row) > 3 else ""
            result.append((in_work, remaining))
        return result

    async def get_setting(self, key: str) -> tuple[str | None, str | None]:
        async with self.lock:
            sheet = self._get_settings_sheet()
            rows = sheet.get_all_values()
            lookup = key.strip().lower()
            for row in rows[1:]:
                if not row or not row[0]:
                    continue
                if row[0].strip().lower() == lookup:
                    value = row[1].strip() if len(row) > 1 else ""
                    note = row[2].strip() if len(row) > 2 else ""
                    return (value or None, note or None)
            return (None, None)

    async def list_requisites(self, chat_id: str) -> list[tuple[int, str, str]]:
        async with self.lock:
            sheet = self._get_requisites_sheet()
            if not sheet:
                return []
            rows = sheet.get_all_values()
            if not rows:
                return []
            headers = rows[0]
            for row in rows[1:]:
                if not row or not row[0]:
                    continue
                cell_id = _normalize_chat_id(row[0])
                if cell_id != chat_id:
                    continue
                result: list[tuple[int, str, str]] = []
                for col_idx, title in enumerate(headers[2:], start=3):
                    raw_title = title.strip() if title else ""
                    value = row[col_idx - 1].strip() if len(row) >= col_idx else ""
                    if not value:
                        continue
                    if raw_title:
                        display_title = raw_title
                    else:
                        fallback = value.splitlines()[0].strip() if value else ""
                        if not fallback:
                            fallback = f"Реквизит {rowcol_to_a1(1, col_idx)[:-1]}"
                        display_title = fallback
                    result.append((col_idx, display_title, value))
                return result
            return []

    async def get_chat_name(self, chat_id: str) -> str | None:
        async with self.lock:
            row_idx = self.chat_row_cache.get(chat_id)
            if not row_idx:
                return None
            col_idx = self.column_map.get("chat_name")
            if not col_idx:
                return None
            return await self._get_cell_value(row_idx, col_idx)

    async def get_total(self, chat_id: str) -> Decimal:
        async with self.lock:
            row_idx = self.chat_row_cache.get(chat_id)
            if not row_idx:
                return Decimal("0")
            return _to_decimal(self.sheet.cell(row_idx, self.result_column).value)

    async def get_currency_totals(self, chat_id: str) -> dict[str, Decimal]:
        async with self.lock:
            row_idx = self.chat_row_cache.get(chat_id)
            result: dict[str, Decimal] = {}
            for code in SUPPORTED_CURRENCY_ORDER:
                if code not in self.allowed_currencies:
                    continue
                col_idx = self.currency_columns.get(code)
                if not col_idx or not row_idx:
                    result[code] = Decimal("0")
                else:
                    result[code] = _to_decimal(self.sheet.cell(row_idx, col_idx).value)
            return result

    async def get_rate_values(self, chat_id: str) -> list[tuple[str, str]]:
        async with self.lock:
            header_row = self.sheet.row_values(1)
            rate_columns: list[tuple[str, int]] = []
            for idx, title in enumerate(header_row, start=1):
                if title and "rate" in title.lower():
                    rate_columns.append((title, idx))
            if not rate_columns:
                return []

            row_idx = self.chat_row_cache.get(chat_id)
            if not row_idx:
                try:
                    cell = self.sheet.find(chat_id, in_column=self.column_map.get("chat_id", 1))
                except gspread.exceptions.CellNotFound:
                    return []
                row_idx = cell.row
                self.chat_row_cache[chat_id] = row_idx

            row_values = self.sheet.row_values(row_idx)
            result: list[tuple[str, str]] = []
            for title, col_idx in rate_columns:
                value = row_values[col_idx - 1] if col_idx - 1 < len(row_values) else ""
                value = value.strip() if isinstance(value, str) else value
                if value:
                    result.append((title, str(value)))
            return result

    async def get_grinex_values(self) -> tuple[str | None, str | None]:
        async with self.lock:
            sheet = self._get_rates_sheet()
            if not sheet:
                return None, None
            try:
                buy = sheet.acell("B11").value
                sell = sheet.acell("B12").value
            except Exception:
                return None, None
            return buy, sell

    async def get_eurusd_rate(self) -> str | None:
        async with self.lock:
            sheet = self._get_rates_sheet()
            if not sheet:
                return None
            try:
                return sheet.acell("A4").value
            except Exception:
                return None


class ReceiptProcessor:
    KEYWORDS = ("ИТОГО", "СУММА", "СУММА ПЕРЕВОДА", "СУММА ОПЛАТЫ")

    def __init__(self):
        self.reader = easyocr.Reader(["ru", "en"], gpu=False)
        currency_tokens = sorted(OCR_ALIAS_TO_CODE.keys(), key=len, reverse=True)
        escaped = [re.escape(token) for token in currency_tokens]
        self.currency_regex = "|".join(escaped)
        self.amount_currency = re.compile(
            rf"(?P<amount>{self._amount_re()})\s*(?P<currency>{self.currency_regex})",
            re.IGNORECASE,
        )
        self.currency_amount = re.compile(
            rf"(?P<currency>{self.currency_regex})\s*(?P<amount>{self._amount_re()})",
            re.IGNORECASE,
        )

    def _amount_re(self) -> str:
        return r"\d[\d\s\u00A0]*(?:[.,]\d+)?"

    def parse(self, image_path: str) -> ReceiptResult:
        lines = self.reader.readtext(image_path, detail=0, paragraph=False)
        if not lines:
            raise ValueError("текст на чеке не распознан")
        amount, currency = self._extract_amount(lines)
        return ReceiptResult(amount=amount, currency=currency, text="\n".join(lines))

    def _extract_amount(self, lines: list[str]) -> tuple[Decimal, str]:
        for line in lines:
            match = self._match_line(line)
            if match:
                return match
        combined = "\n".join(lines)
        match = self._match_line(combined)
        if match:
            return match
        for line in lines:
            upper_line = line.upper()
            if any(keyword in upper_line for keyword in self.KEYWORDS):
                amount = self._extract_number(line)
                if amount is not None:
                    return amount, "RUB"
        raise ValueError("не нашёл сумму и валюту на чеке")

    def _match_line(self, line: str) -> tuple[Decimal, str] | None:
        for pattern in (self.amount_currency, self.currency_amount):
            match = pattern.search(line)
            if match:
                currency_token = match.group("currency")
                currency = self._map_currency(currency_token)
                if not currency:
                    continue
                amount_text = match.group("amount")
                amount = self._extract_number(amount_text)
                if amount is None:
                    continue
                return amount, currency
        return None

    def _extract_number(self, fragment: str) -> Decimal | None:
        cleaned = (
            fragment.replace("\u00A0", "")
            .replace(" ", "")
            .replace(",", ".")
        )
        cleaned = re.sub(r"[^0-9.+-]", "", cleaned)
        if cleaned.count(".") > 1:
            parts = cleaned.split(".")
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        try:
            value = Decimal(cleaned)
        except Exception:
            return None
        return value

    def _map_currency(self, token: str | None) -> str | None:
        if not token:
            return None
        upper = token.upper().strip().replace(".", "")
        return OCR_ALIAS_TO_CODE.get(token) or OCR_ALIAS_TO_CODE.get(upper)


class MathBot:
    def __init__(self):
        self.token = os.environ["TELEGRAM_TOKEN"]
        self.evaluator = ExpressionEvaluator()
        self.repo = SheetRepository(set(SUPPORTED_CURRENCY_ALIASES.keys()))
        self.receipt_processor = ReceiptProcessor()
        self.receipts_dir = Path(os.environ.get("RECEIPTS_DIR", "receipts"))
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.support_chat_id = int(os.environ.get("SUPPORT_CHAT_ID", "0") or 0)
        self.support_topics_file = Path(os.environ.get("SUPPORT_TOPICS_FILE", "support_topics.json"))
        self.support_topics_file.parent.mkdir(parents=True, exist_ok=True)
        self.exchange_state: dict[str, dict] = {}
        self.pending_receipts: dict[str, PendingReceiptEntry] = {}
        self.manual_receipt_inputs: dict[int, str] = {}

    def _load_support_topics(self) -> dict[str, int]:
        if not self.support_topics_file.exists():
            return {}
        try:
            return json.loads(self.support_topics_file.read_text())
        except Exception:
            return {}

    def _save_support_topics(self, mapping: dict[str, int]) -> None:
        self.support_topics_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))

    async def _ensure_support_thread(self, context: ContextTypes.DEFAULT_TYPE, user) -> int | None:
        if not self.support_chat_id:
            return None
        mapping = self._load_support_topics()
        if str(user.id) in mapping:
            return mapping[str(user.id)]
        title = (user.full_name or user.username or str(user.id))[:64]
        topic = await context.bot.create_forum_topic(self.support_chat_id, name=title)
        mapping[str(user.id)] = topic.message_thread_id
        self._save_support_topics(mapping)
        return topic.message_thread_id

    async def _route_to_support(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> None:
        if not self.support_chat_id:
            await update.message.reply_text("Напиши, пожалуйста, сумму или воспользуйся командой /сумма.")
            return
        user = update.effective_user
        await self.repo.ensure_row(str(user.id), user.full_name or user.username or str(user.id), user.full_name or user.username or str(user.id))
        thread_id = await self._ensure_support_thread(context, user)
        payload = f"Сообщение от {user.full_name or user.username or user.id}:\n{raw_text}"
        await context.bot.send_message(
            chat_id=self.support_chat_id,
            text=payload,
            message_thread_id=thread_id
        )

    async def _get_client_name(self, chat, user) -> str:
        chat_id = str(chat.id)
        stored = await self.repo.get_chat_name(chat_id)
        if stored:
            return stored
        return chat.title or chat.username or user.full_name or user.username or chat_id

    def _should_log_chat(self, chat_type: str) -> bool:
        return chat_type in (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)

    async def _log_support_event(self, context: ContextTypes.DEFAULT_TYPE, user, text: str) -> None:
        if not self.support_chat_id:
            return
        thread_id = await self._ensure_support_thread(context, user)
        if thread_id is None:
            return
        await context.bot.send_message(
            chat_id=self.support_chat_id,
            text=text,
            message_thread_id=thread_id,
        )
        chat_id = str(user.id)
        chat_name = await self.repo.get_chat_name(chat_id) or (user.full_name or user.username or chat_id)
        timestamp = datetime.now(timezone.utc).isoformat()
        topic_label = chat_name
        full_text = f"{chat_name} {text}"
        await self.repo.append_log(timestamp, chat_id, chat_name, topic_label, full_text)

    def _find_user_by_thread(self, thread_id: int) -> int | None:
        mapping = self._load_support_topics()
        for user_id, t_id in mapping.items():
            if t_id == thread_id:
                return int(user_id)
        return None

    async def _get_bot_username(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        if context.bot.username:
            return context.bot.username
        me = await context.bot.get_me()
        return me.username or ""

    def _should_process(self, chat_type: str, text: str, bot_username: str) -> bool:
        if not text:
            return False
        if chat_type == ChatType.PRIVATE:
            return True
        if not bot_username:
            return False
        mention = f"@{bot_username}".lower()
        return mention in text.lower()

    def _evaluate_expression(self, stripped_text: str) -> tuple[str, Decimal, str, Decimal | None]:
        expression_display = stripped_text.strip()
        try:
            text, currency = self._extract_currency(stripped_text)
        except ValueError as exc:
            raise
        if not text:
            raise ValueError("EMPTY_EXPRESSION")
        if currency is None:
            raise ValueError("NO_CURRENCY")
        manual_value = None
        if "=" in text:
            _, right = text.split("=", 1)
            manual_value = self._parse_manual_value(right)
            if manual_value is None:
                raise ValueError("BAD_MANUAL")
        if manual_value is not None:
            result_decimal = manual_value
        else:
            result = self.evaluator.evaluate(text)
            result_decimal = Decimal(str(result))
        expression_to_store = expression_display or text
        return expression_to_store, result_decimal, currency, manual_value

    def _strip_mention(self, text: str, bot_username: str) -> str:
        if not bot_username:
            return text.strip()
        pattern = re.compile(rf"@{re.escape(bot_username)}\b", re.IGNORECASE)
        cleaned = pattern.sub("", text, count=1)
        return cleaned.strip()

    def _extract_currency(self, text: str) -> tuple[str, str | None]:
        trimmed = text.strip()
        if not trimmed:
            return "", None
        match = re.search(r"([A-Za-zА-Яа-я]+)\s*$", trimmed)
        if not match:
            return trimmed, None
        token = match.group(1).lower()
        if token in ALIAS_TO_CURRENCY:
            currency = ALIAS_TO_CURRENCY[token]
            cleaned = trimmed[: match.start()].strip()
            return cleaned, currency
        # если слово есть, но мы его не знаем — сообщаем допустимые варианты
        allowed = ", ".join(sorted({alias for aliases in SUPPORTED_CURRENCY_ALIASES.values() for alias in aliases}))
        raise ValueError(
            f"Неизвестная валюта '{match.group(1)}'. Используй одно из: {allowed}"
        )

    def _parse_manual_value(self, fragment: str) -> Decimal | None:
        cleaned = fragment.strip()
        cleaned = cleaned.replace("\u00A0", "").replace("\u202F", "").replace(" ", "").replace("'", "")
        cleaned = cleaned.replace(",", ".")
        cleaned = re.sub(r"[^0-9+\-.]", "", cleaned)
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    async def start(self, update: Update, _: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        user = update.effective_user
        await self.repo.ensure_row(str(chat.id), chat.title or chat.username or str(chat.id), user.full_name or user.username or str(user.id))
        await update.message.reply_text(
            "Добрый день. Чем могу помочь?"
        )

    async def handle_expression(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message or update.effective_message
        if not message:
            return
        if await self._handle_manual_receipt_amount(update, context):
            return
        raw_text = (message.text or "").strip()
        if not raw_text:
            return
        LOGGER.info("expr chat=%s text=%s", update.effective_chat.id, raw_text)
        chat_id = str(update.effective_chat.id)
        state = self.exchange_state.get(chat_id)
        if state:
            await self._process_exchange_message(update, context, raw_text, state)
            return
        if re.search(r"\d", raw_text) and not raw_text.startswith("/"):
            chat = update.effective_chat
            if chat.type == ChatType.PRIVATE:
                await self._route_to_support(update, context, raw_text)
                return
            # в группах продолжаем обработку (при условии упоминания бота)
        if raw_text.startswith("/"):
            return
        await self._process_expression(update, context, raw_text)

    async def start_exchange(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        await self._begin_exchange(chat, message, user, context)

    async def cancel_exchange(self, update: Update, _: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        chat_id = str(chat.id)
        if chat_id in self.exchange_state:
            del self.exchange_state[chat_id]
            await update.effective_message.reply_text("Обмен отменён.")
        else:
            await update.effective_message.reply_text("Нет активного обмена.")

    async def _begin_exchange(self, chat, message, user, context):
        chat_id = str(chat.id)
        self.exchange_state[chat_id] = {"stage": "give"}
        name = await self._get_client_name(chat, user)
        await message.reply_text(
            f"{name}: Шаг 1. Введи сумму и валюту, которую клиент отдаёт (пример: 10000 rub).\n/cancel — отменить."
        )

    async def _process_exchange_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str, state: dict) -> None:
        chat = update.effective_chat
        chat_id = str(chat.id)
        message = update.effective_message
        user = update.effective_user
        bot_username = await self._get_bot_username(context)
        stripped_text = self._strip_mention(raw_text, bot_username)
        expression_display = stripped_text.strip()
        if not expression_display:
            await message.reply_text("Нужен текст с суммой.")
            return
        try:
            text, currency = self._extract_currency(stripped_text)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        if not text:
            await message.reply_text("Добавь выражение после обращения.")
            return
        manual_value = None
        if "=" in text:
            _, right = text.split("=", 1)
            manual_value = self._parse_manual_value(right)
            if manual_value is None:
                await message.reply_text("Не могу прочитать число после '=' — напиши его цифрами.")
                return
        if manual_value is not None:
            result_decimal = manual_value
        else:
            try:
                result = self.evaluator.evaluate(text)
            except Exception as exc:
                await message.reply_text(f"Ошибка: {exc}")
                return
            result_decimal = Decimal(str(result))
        name = await self._get_client_name(chat, user)
        if state["stage"] == "give":
            amount = abs(result_decimal)
            delta = -amount
            label = f"[Обмен отдаёт] {expression_display}"
            await self._write_exchange_entry(chat, user, label, delta, currency)
            await self._log_support_event(context, user, f"отдаёт {amount} {currency}")
            self.exchange_state[chat_id] = {"stage": "receive"}
            await message.reply_text(
                f"{name}: зафиксировал отдачу {amount} {currency}. Теперь напиши, что клиент получает."
            )
        elif state["stage"] == "receive":
            amount = abs(result_decimal)
            delta = amount
            label = f"[Обмен получает] {expression_display}"
            await self._write_exchange_entry(chat, user, label, delta, currency)
            await self._log_support_event(context, user, f"получает {amount} {currency}")
            del self.exchange_state[chat_id]
            await message.reply_text("Заявка принята.")
            fake_update = SimpleNamespace(
                effective_chat=chat,
                effective_message=message,
                message=message,
                effective_user=user,
            )
            await self.send_total(fake_update, context)
            should_delete = True
        else:
            await message.reply_text("Неизвестный статус обмена, сбрасываю.")
            del self.exchange_state[chat_id]

    async def _write_exchange_entry(self, chat, user, label: str, delta: Decimal, currency: str):
        row = SheetRow(
            chat_id=str(chat.id),
            chat_name=chat.title or chat.username or str(chat.id),
            user=user.full_name or user.username or str(user.id),
            expression=label,
            delta=delta,
            timestamp=datetime.now(timezone.utc).isoformat(),
            currency=currency,
        )
        await self.repo.upsert(row)
    async def handle_slash_expression(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message
        if not message:
            return
        text = (message.text or "").strip()
        if not text.startswith("/"):
            return
        command_token = text.split()[0].lower()
        base_token = command_token.split('@')[0]
        known = {"/start", "/help", "/summa", "/total", "/kurs", "/курс", "/сумма"}
        if base_token in known:
            return
        sanitized = text[1:].strip()
        if not sanitized:
            return
        chat_key = str(message.chat.id)
        state = self.exchange_state.get(chat_key)
        if state:
            await self._process_exchange_message(update, context, sanitized, state)
            return
        forced_chat_id = None
        forced_chat_name = None
        force_process = False
        if (
            self.support_chat_id
            and message.chat
            and message.chat.id == self.support_chat_id
            and message.message_thread_id
        ):
            user_id = self._find_user_by_thread(message.message_thread_id)
            if not user_id:
                await message.reply_text("Не нашёл, к какому клиенту относится эта тема.")
                return
            forced_chat_id = str(user_id)
            forced_chat_name = await self.repo.get_chat_name(forced_chat_id)
            force_process = True
        await self._process_expression(
            update,
            context,
            sanitized,
            forced_chat_id=forced_chat_id,
            forced_chat_name=forced_chat_name,
            force_process=force_process,
        )

    async def _process_expression(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        raw_text: str,
        *,
        forced_chat_id: str | None = None,
        forced_chat_name: str | None = None,
        force_process: bool = False,
    ):
        message = update.message or update.effective_message
        if not message:
            return
        chat = update.effective_chat
        user = message.from_user

        if (
            not forced_chat_id
            and chat.type == ChatType.PRIVATE
            and not re.search(r"\d", raw_text)
        ):
            await self._route_to_support(update, context, raw_text)
            return

        bot_username = await self._get_bot_username(context)
        if not force_process and not self._should_process(chat.type, raw_text, bot_username):
            return

        stripped_text = self._strip_mention(raw_text, bot_username)
        expression_display = stripped_text.strip()
        target_chat_id = forced_chat_id or str(chat.id)
        chat_display = forced_chat_name or chat.title or chat.username or str(chat.id)
        try:
            text, currency = self._extract_currency(stripped_text)
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        if not text:
            await message.reply_text("Добавь выражение после обращения к боту.")
            return
        if currency is None:
            allowed = ", ".join(SUPPORTED_CURRENCY_ORDER)
            await message.reply_text(f"Укажи валюту в конце выражения (доступно: {allowed}).")
            return

        manual_value = None
        expression_to_store = expression_display or text

        if "=" in text:
            _, right = text.split("=", 1)
            manual_value = self._parse_manual_value(right)
            if manual_value is None:
                await message.reply_text("Не могу прочитать число после '=' — напиши его цифрами.")
                return

        if manual_value is not None:
            result_decimal = manual_value
        else:
            try:
                result = self.evaluator.evaluate(text)
            except Exception as exc:
                await message.reply_text(f"Ошибка: {exc}")
                return
            result_decimal = Decimal(str(result))

        row = SheetRow(
            chat_id=target_chat_id,
            chat_name=chat_display,
            user=user.full_name or user.username or str(user.id),
            expression=expression_to_store,
            delta=result_decimal,
            timestamp=datetime.now(timezone.utc).isoformat(),
            currency=currency,
        )

        try:
            total = await self.repo.upsert(row)
        except Exception as exc:
            LOGGER.exception("Sheet write error")
            await message.reply_text(f"Не смог записать в таблицу: {exc}")
            return

        symbol = "⇒" if manual_value is not None else "="
        await message.reply_text(
            f"{expression_to_store} {symbol} {_format_decimal(result_decimal)} (сумма: {_format_decimal(total)})"
        )
        if forced_chat_id is None and self._should_log_chat(chat.type):
            await self._log_support_event(
                context,
                user,
                f"записал: {expression_to_store} {symbol} {_format_decimal(result_decimal)} ({currency or "?"})",
            )
        await self.send_total(update, context)

    async def handle_receipt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message
        if not message:
            return
        chat = update.effective_chat
        bot_username = await self._get_bot_username(context)
        if chat.type != ChatType.PRIVATE:
            caption = (message.caption or "").lower()
            if not bot_username or f"@{bot_username.lower()}" not in caption:
                return
        force_chat_id = None
        force_chat_name = None
        if (
            self.support_chat_id
            and chat.id == self.support_chat_id
            and message.message_thread_id
        ):
            user_id = self._find_user_by_thread(message.message_thread_id)
            if not user_id:
                await message.reply_text("Не нашёл, к какому клиенту относится эта тема.")
                return
            force_chat_id = str(user_id)
            force_chat_name = await self.repo.get_chat_name(force_chat_id) or force_chat_id

        file = None
        filename = None
        if message.photo:
            file = await message.photo[-1].get_file()
            filename = f"{file.file_unique_id}.jpg"
        elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
            file = await message.document.get_file()
            filename = message.document.file_name or f"{file.file_unique_id}"
        else:
            return

        local_path = self.receipts_dir / filename
        await file.download_to_drive(str(local_path))
        try:
            receipt = self.receipt_processor.parse(str(local_path))
        except ValueError as exc:
            await message.reply_text(f"Не смог прочитать чек: {exc}")
            return

        amount = receipt.amount.copy_abs()
        currency = receipt.currency
        target_chat_id = force_chat_id or str(chat.id)
        target_chat_name = force_chat_name or chat.title or chat.username or str(chat.id)
        if message.from_user:
            user_name = message.from_user.full_name or message.from_user.username or str(message.from_user.id)
        else:
            user_name = str(chat.id)

        token = uuid4().hex
        entry = PendingReceiptEntry(
            token=token,
            target_chat_id=target_chat_id,
            target_chat_name=target_chat_name,
            user_name=user_name,
            currency=currency,
            amount=amount,
            filename=filename,
        )
        self.pending_receipts[token] = entry
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Верно", callback_data=f"receipt|{token}|ok"),
                    InlineKeyboardButton("Исправить", callback_data=f"receipt|{token}|edit"),
                ]
            ]
        )
        prompt = await message.reply_text(
            f"Распознано {_format_decimal(amount)} {currency}. Всё верно?",
            reply_markup=keyboard,
        )
        entry.prompt_chat_id = prompt.chat_id
        entry.prompt_message_id = prompt.message_id

    def _clear_manual_receipt_refs(self, token: str) -> None:
        if not self.manual_receipt_inputs:
            return
        self.manual_receipt_inputs = {
            user_id: pending_token
            for user_id, pending_token in self.manual_receipt_inputs.items()
            if pending_token != token
        }

    async def _finalize_receipt_entry(self, entry: PendingReceiptEntry, message, user, context: ContextTypes.DEFAULT_TYPE) -> bool:
        row = SheetRow(
            chat_id=entry.target_chat_id,
            chat_name=entry.target_chat_name,
            user=entry.user_name,
            expression=f"Чек {entry.currency} -{_format_decimal(entry.amount)} ({entry.filename})",
            delta=-entry.amount,
            timestamp=datetime.now(timezone.utc).isoformat(),
            currency=entry.currency,
        )
        try:
            total = await self.repo.upsert(row)
        except Exception as exc:
            LOGGER.exception("Receipt write error")
            await message.reply_text(f"Не смог обновить таблицу: {exc}")
            return False

        await message.reply_text(
            f"Чек сохранён: -{_format_decimal(entry.amount)} {entry.currency}. Текущий итог по {entry.currency}: {_format_decimal(total)}"
        )
        fake_update = SimpleNamespace(
            effective_chat=message.chat,
            effective_message=message,
            message=message,
            effective_user=user,
        )
        await self.send_total(fake_update, context)
        return True

    async def handle_receipt_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query or not query.data:
            return
        parts = query.data.split("|", 2)
        if len(parts) != 3:
            await query.answer("Некорректный ответ", show_alert=True)
            return
        _, token, action = parts
        entry = self.pending_receipts.get(token)
        if not entry:
            await query.answer("Эта проверка уже завершена", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            self._clear_manual_receipt_refs(token)
            return

        if action == "ok":
            await query.answer("Сохраняю чек…")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            success = await self._finalize_receipt_entry(entry, query.message, query.from_user, context)
            if success:
                self.pending_receipts.pop(token, None)
                self._clear_manual_receipt_refs(token)
        elif action == "edit":
            await query.answer("Укажи сумму вручную")
            self.manual_receipt_inputs[query.from_user.id] = token
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text("Напиши сумму чека цифрами (пример: 12345.67)")
        else:
            await query.answer("Неизвестное действие", show_alert=True)

    async def _handle_manual_receipt_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return False
        token = self.manual_receipt_inputs.get(user.id)
        if not token:
            return False
        entry = self.pending_receipts.get(token)
        if not entry:
            self.manual_receipt_inputs.pop(user.id, None)
            await message.reply_text("Эта проверка уже завершена")
            self._clear_manual_receipt_refs(token)
            return True
        manual_value = self._parse_manual_value(message.text or "")
        if manual_value is None:
            await message.reply_text("Не могу прочитать сумму. Напиши цифрами, например 12345.67")
            return True
        entry.amount = manual_value.copy_abs()
        await message.reply_text(
            f"Принял сумму {_format_decimal(entry.amount)} {entry.currency}. Сохраняю чек…"
        )
        success = await self._finalize_receipt_entry(entry, message, user, context)
        if success:
            self.pending_receipts.pop(token, None)
            self.manual_receipt_inputs.pop(user.id, None)
            self._clear_manual_receipt_refs(token)
        return True

    async def handle_support_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.support_chat_id:
            return
        message = update.effective_message
        if not message or message.chat_id != self.support_chat_id:
            return
        thread_id = message.message_thread_id
        if not thread_id:
            return
        user_id = self._find_user_by_thread(thread_id)
        if not user_id:
            return
        text_body = message.text or message.caption
        if not text_body:
            return
        LOGGER.info("support reply chat=%s text=%s", message.chat_id, text_body)
        await context.bot.send_message(chat_id=user_id, text=text_body)

    async def send_total(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        message = update.message
        target_chat_id = str(chat.id)
        prefix = "Остатки:"
        if (
            self.support_chat_id
            and chat.id == self.support_chat_id
            and message
            and message.message_thread_id
        ):
            user_id = self._find_user_by_thread(message.message_thread_id)
            if user_id:
                target_chat_id = str(user_id)
                chat_name = await self.repo.get_chat_name(target_chat_id)
                label = chat_name or user_id
                prefix = f"Остатки для {label}:"
        totals = await self.repo.get_currency_totals(target_chat_id)
        lines: list[str] = []
        for code in SUPPORTED_CURRENCY_ORDER:
            value = totals.get(code, Decimal('0'))
            if value == Decimal('0'):
                continue
            lines.append(f"- {code}: {_format_decimal(value)}")
        note = "\n\nБаланс:\n«-» клиент должен\n«+» фин услуга должна."
        if not lines:
            await update.message.reply_text(f"{prefix} все валюты = 0{note}")
            if self._should_log_chat(chat.type):
                await self._log_support_event(context, update.effective_user, f"запросил /summa: {prefix} все валюты = 0")
            return
        text_block = prefix + "\n" + "\n".join(lines) + note
        await update.message.reply_text(text_block)
        if self._should_log_chat(chat.type):
            await self._log_support_event(context, update.effective_user, f"Клиент запросил /summa: \n{text_block}")

    async def send_menu(self, update: Update, _: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("курсы", callback_data="menu_kurs")],
            [InlineKeyboardButton("баланс", callback_data="menu_balance")],
            [InlineKeyboardButton("обмен", callback_data="menu_exchange")],
            [InlineKeyboardButton("реквизиты", callback_data="menu_requisites")],
        ]
        await update.message.reply_text("Меню:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def _delete_message_safe(self, message) -> None:
        if not message:
            return
        try:
            await message.delete()
        except Exception:
            pass


    async def show_requisites(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        message = update.effective_message
        entries = await self.repo.list_requisites(str(chat.id))
        if not entries:
            await message.reply_text("Для этого клиента нет сохранённых реквизитов.")
            return
        keyboard = [
            [InlineKeyboardButton(title, callback_data=f"req|{col_idx}")]
            for col_idx, title, _ in entries
        ]
        await message.reply_text("Выбери реквизит:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def _handle_requisite_selection(self, query, context: ContextTypes.DEFAULT_TYPE, payload: str):
        try:
            col_idx = int(payload)
        except ValueError:
            await query.answer("Некорректный реквизит", show_alert=True)
            return
        chat = query.message.chat
        entries = await self.repo.list_requisites(str(chat.id))
        match = next((item for item in entries if item[0] == col_idx), None)
        if not match:
            await query.answer("Реквизит не найден", show_alert=True)
            return
        _, title, value = match
        await query.message.reply_text(f"{title}:\n{value}")
        if self._should_log_chat(chat.type):
            await self._log_support_event(context, query.from_user, f"запросил реквизит {title}")
        await query.answer()

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if not query:
            return
        await query.answer()
        should_delete = False
        if query.data == "menu_kurs":
            keyboard = [
                [InlineKeyboardButton("Курс чата", callback_data="menu_kurs_chat")],
                [InlineKeyboardButton("Курс Grinex", callback_data="menu_grinex")],
                [InlineKeyboardButton("xe.com", callback_data="menu_kurs_xe")],
            ]
            await query.message.reply_text("Выбери курс:", reply_markup=InlineKeyboardMarkup(keyboard))
            should_delete = True
        elif query.data == "menu_kurs_chat":
            fake_update = SimpleNamespace(
                effective_chat=query.message.chat,
                effective_message=query.message,
                message=query.message,
                effective_user=query.from_user,
            )
            await self.send_rates(fake_update, context)
            should_delete = True
        elif query.data == "menu_grinex":
            buy, sell = await self.repo.get_grinex_values()
            text = "Курс Grinex:\nкупить USDT — {buy}\nпродать USDT — {sell}".format(
                buy=buy or "—",
                sell=sell or "—",
            )
            await query.message.reply_text(text)
            should_delete = True
            if self._should_log_chat(query.message.chat.type):
                await self._log_support_event(context, query.from_user, f"запросил Курс Grinex:\n{text}")
        elif query.data == "menu_kurs_xe":
            value = await self.repo.get_eurusd_rate()
            if not value:
                await query.answer("Нет данных в курсы!A4", show_alert=True)
            else:
                await query.message.reply_text(f"Курс Евро/Доллар: {value}")
            should_delete = True
        elif query.data == "menu_balance":
            fake_update = SimpleNamespace(
                effective_chat=query.message.chat,
                effective_message=query.message,
                message=query.message,
                effective_user=query.from_user,
            )
            await self.send_total(fake_update, context)
        elif query.data == "menu_exchange":
            await self._begin_exchange(query.message.chat, query.message, query.from_user, context)
            should_delete = True
        elif query.data == "menu_requisites":
            fake_update = SimpleNamespace(
                effective_chat=query.message.chat,
                effective_message=query.message,
                message=query.message,
                effective_user=query.from_user,
            )
            await self.show_requisites(fake_update, context)
            should_delete = True
        elif query.data.startswith("req|"):
            await self._handle_requisite_selection(query, context, query.data.split("|", 1)[1])
            should_delete = True
        elif query.data == "menu_exchange":
            fake_update = SimpleNamespace(
                effective_chat=query.message.chat,
                effective_message=query.message,
                message=query.message,
                effective_user=query.from_user,
            )
            await self.start_exchange(fake_update, context)
            should_delete = True
        if should_delete:
            await self._delete_message_safe(query.message)
            should_delete = True

    async def send_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        value, note = await self.repo.get_setting("wallet")
        if not value:
            await update.message.reply_text("Кошелёк не задан на листе 'Настройка'.")
            return
        await update.message.reply_text(value)
        if note:
            await update.message.reply_text(f"Сеть: {note}")
        chat = update.effective_chat
        if self._should_log_chat(chat.type):
            await self._log_support_event(
                context, update.effective_user, f"запросил кошелёк (сеть: {note or 'не указана'})"
            )

    async def send_rates(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        message = update.effective_message
        target_chat_id = str(chat.id)
        prefix = "Курсы:"
        if (
            self.support_chat_id
            and chat.id == self.support_chat_id
            and message
            and getattr(message, "message_thread_id", None)
        ):
            user_id = self._find_user_by_thread(message.message_thread_id)
            if user_id:
                target_chat_id = str(user_id)
                chat_name = await self.repo.get_chat_name(target_chat_id)
                label = chat_name or user_id
                prefix = f"Курсы для {label}:"
        entries = await self.repo.get_rate_values(target_chat_id)
        if not entries:
            await message.reply_text("Для этого чата нет столбцов с курсом (rate).")
            return
        lines = [f"- {title}: {value}" for title, value in entries]
        text_block = prefix + "\n" + "\n".join(lines)
        await message.reply_text(text_block)
        if self._should_log_chat(chat.type):
            await self._log_support_event(
                context, update.effective_user, f"запросил /kurs: \n{text_block}"
            )

    async def send_ruble_debts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        message = update.effective_message
        entries = await self.repo.list_ruble_debts(str(chat.id))
        if not entries:
            await message.reply_text("Нет активных долгов в листе ‘Долги рубль’.")
            return
        for in_work, remaining in entries:
            line = (
                f"В работе: {in_work or '—'}\nОсталось обработать: {remaining or '—'}"
            )
            await message.reply_text(line)


    def run(self):
        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler(["summa", "total"], self.send_total))
        app.add_handler(MessageHandler(filters.Regex(r"^/сумма(?:@[\w_]+)?\b"), self.send_total))
        app.add_handler(CommandHandler(["kurs"], self.send_rates))
        app.add_handler(CommandHandler(["kosh"], self.send_wallet))
        app.add_handler(CommandHandler(["req", "requisites"], self.show_requisites))
        app.add_handler(CommandHandler(["дай"], self.send_ruble_debts))
        app.add_handler(CommandHandler(["menu"], self.send_menu))
        app.add_handler(CallbackQueryHandler(self.handle_receipt_callback, pattern=r"^receipt\|"))
        app.add_handler(CallbackQueryHandler(self.handle_menu_callback, pattern=r"^(menu_|req\|)"))
        app.add_handler(CommandHandler(["exchange"], self.start_exchange))
        app.add_handler(CommandHandler(["cancel"], self.cancel_exchange))
        app.add_handler(MessageHandler(filters.COMMAND, self.handle_slash_expression))
        if self.support_chat_id:
            app.add_handler(MessageHandler(filters.Chat(self.support_chat_id) & filters.TEXT, self.handle_support_reply, block=False))
            app.add_handler(MessageHandler(filters.Chat(self.support_chat_id) & filters.TEXT & ~filters.COMMAND, self.handle_expression, block=False))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.handle_receipt))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_expression))
        app.run_polling()


if __name__ == "__main__":
    MathBot().run()
