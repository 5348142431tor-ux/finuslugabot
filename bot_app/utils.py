from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional


def to_decimal(value: Optional[str]) -> Decimal:
    if value is None:
        return Decimal("0")
    cleaned = str(value).strip().replace("\u00A0", "").replace("\u202F", "").replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def round_to_hundred(value: Decimal) -> Decimal:
    return (value / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal("100")


def round_exchange_amount(value: Decimal, currency_code: str) -> Decimal:
    step = Decimal("5") if currency_code.upper() in {"USD", "EUR"} else Decimal("100")
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def looks_like_expression(text: str) -> bool:
    return bool(re.search(r"\d", text))


def normalize_chat_text(text: str) -> str:
    normalized = re.sub(r"[^\w\sа-яА-ЯёЁ]", " ", text.casefold())
    return re.sub(r"\s+", " ", normalized).strip()


def looks_like_greeting(text: str) -> bool:
    normalized = normalize_chat_text(text)
    return normalized in {
        "привет",
        "здравствуйте",
        "здравствуй",
        "добрый день",
        "добрый вечер",
        "доброе утро",
        "доброго дня",
        "hello",
        "hi",
    }


def looks_like_thanks(text: str) -> bool:
    normalized = normalize_chat_text(text)
    return normalized in {
        "спасибо",
        "спасибо большое",
        "благодарю",
        "благодарю вас",
        "спс",
        "thanks",
        "thank you",
        "thx",
    }
