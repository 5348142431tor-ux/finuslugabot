from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

import easyocr

from .constants import OCR_ALIAS_TO_CODE
from .models import ReceiptResult


class ReceiptProcessor:
    KEYWORDS = ("ИТОГО", "СУММА", "СУММА ПЕРЕВОДА", "СУММА ОПЛАТЫ", "СУММА ОПЕРАЦИИ")

    def __init__(self):
        self.reader = easyocr.Reader(["ru", "en"], gpu=False)
        currency_tokens = sorted(OCR_ALIAS_TO_CODE.keys(), key=len, reverse=True)
        escaped = [re.escape(token) for token in currency_tokens]
        currency_regex = "|".join(escaped)
        amount_re = r"\d[\d\s\u00A0]*(?:[.,]\d+)?"
        self.amount_currency = re.compile(
            rf"(?P<amount>{amount_re})\s*(?P<currency>{currency_regex})",
            re.IGNORECASE,
        )
        self.currency_amount = re.compile(
            rf"(?P<currency>{currency_regex})\s*(?P<amount>{amount_re})",
            re.IGNORECASE,
        )

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
        for idx, line in enumerate(lines):
            upper_line = line.upper()
            if any(keyword in upper_line for keyword in self.KEYWORDS):
                window = " ".join(lines[idx : idx + 3])
                amount = self._extract_number(window)
                if amount is not None:
                    return amount, "RUB"
        raise ValueError("не нашёл сумму и валюту на чеке")

    def _match_line(self, line: str) -> Optional[Tuple[Decimal, str]]:
        for pattern in (self.amount_currency, self.currency_amount):
            match = pattern.search(line)
            if not match:
                continue
            currency = self._map_currency(match.group("currency"))
            amount = self._extract_number(match.group("amount"))
            if currency and amount is not None:
                return amount, currency
        return None

    def _extract_number(self, fragment: str) -> Optional[Decimal]:
        normalized = fragment.replace("\u00A0", " ")
        candidates = re.findall(r"\d[0-9OОoо\s.,+-]*", normalized)
        cleaned = max(candidates, key=len) if candidates else normalized
        cleaned = cleaned.replace("О", "0").replace("о", "0").replace("O", "0").replace("o", "0")
        cleaned = cleaned.replace(" ", "").replace(",", ".")
        cleaned = re.sub(r"[^0-9.+-]", "", cleaned)
        if cleaned.count(".") > 1:
            parts = cleaned.split(".")
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    def _map_currency(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        upper = token.upper().strip().replace(".", "")
        return OCR_ALIAS_TO_CODE.get(upper) or OCR_ALIAS_TO_CODE.get(token)
