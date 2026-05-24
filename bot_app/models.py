from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class SheetRow:
    chat_id: str
    chat_name: str
    user: str
    expression: str
    delta: Decimal
    timestamp: str
    currency: str


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


@dataclass
class ExchangeRequestRow:
    status: str
    created_at: str
    request_id: str
    chat_id: str
    chat_name: str
    give_currency: str
    give_amount: Decimal | None
    receive_currency: str
    receive_amount: Decimal | None
